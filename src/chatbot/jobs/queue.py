"""Where memory pushes wait: SQS in production, ElasticMQ (the same API) in the cluster, CI and
dev, and an in-memory queue in tests.

The worker receives a batch, runs each push and acks (deletes) it only once it succeeded; an
unacked message comes back after the queue's visibility timeout, and after its maximum
receives the queue moves it to the dead-letter queue (the queue's redrive policy).
"""

import logging
import time
from collections import deque
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Protocol

import aioboto3

from chatbot.config import Settings
from chatbot.jobs.push import MemoryPush

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Received:
    push: MemoryPush
    receipt: str  # what ack() needs
    receives: int  # 1 the first time it's delivered
    sent_at: float | None = None  # epoch seconds, when it was enqueued


@dataclass(frozen=True)
class QueueDepth:
    visible: int  # waiting
    in_flight: int  # received, not acked yet
    dead: int  # in the dead-letter queue


class JobQueue(Protocol):
    async def enqueue(self, push: MemoryPush) -> None: ...

    async def receive(self, wait_seconds: int = 20, max_jobs: int = 10) -> list[Received]:
        """Up to `max_jobs`, long-polling up to `wait_seconds`; [] if none came."""
        ...

    async def ack(self, received: Received) -> None:
        """The push succeeded: delete it."""
        ...

    async def depth(self) -> QueueDepth: ...


class SqsJobQueue:
    """SQS through aioboto3. `open()` before use, `close()` after."""

    def __init__(self, settings: Settings) -> None:
        self.queue_url = settings.require("sqs_queue_url")
        self.dead_letter_url = settings.sqs_dead_letter_queue_url
        self._client_args: dict[str, Any] = {"region_name": settings.sqs_region}
        if settings.sqs_endpoint_url:  # ElasticMQ; unset on AWS
            self._client_args["endpoint_url"] = settings.sqs_endpoint_url
        if settings.sqs_access_key_id and settings.sqs_secret_access_key:
            # Otherwise boto's default chain: the environment, or the pod's IAM role on AWS.
            self._client_args["aws_access_key_id"] = settings.sqs_access_key_id
            self._client_args["aws_secret_access_key"] = (
                settings.sqs_secret_access_key.get_secret_value()
            )
        self._stack = AsyncExitStack()
        self._sqs: Any = None

    async def open(self) -> None:
        session = aioboto3.Session()
        self._sqs = await self._stack.enter_async_context(
            session.client("sqs", **self._client_args)
        )

    async def close(self) -> None:
        await self._stack.aclose()

    async def enqueue(self, push: MemoryPush) -> None:
        await self._sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=push.to_json(),
            MessageGroupId=push.group_id,
            MessageDeduplicationId=push.dedupe_id,
        )

    async def receive(self, wait_seconds: int = 20, max_jobs: int = 10) -> list[Received]:
        resp = await self._sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_jobs,
            WaitTimeSeconds=wait_seconds,
            MessageSystemAttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        )
        out = []
        for message in resp.get("Messages", []):
            attributes = message.get("Attributes", {})
            try:
                push = MemoryPush.from_json(message["Body"])
            except (ValueError, TypeError):
                # Not ours, or corrupt: left unacked, so it ends in the dead-letter queue.
                logger.error("unreadable queue message %s", message.get("MessageId"))
                continue
            sent = attributes.get("SentTimestamp")
            out.append(
                Received(
                    push,
                    message["ReceiptHandle"],
                    int(attributes.get("ApproximateReceiveCount", 1)),
                    int(sent) / 1000 if sent else None,
                )
            )
        return out

    async def ack(self, received: Received) -> None:
        await self._sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=received.receipt)

    async def depth(self) -> QueueDepth:
        names = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
        main = (
            await self._sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=names)
        )["Attributes"]
        dead = 0
        if self.dead_letter_url:
            dlq = await self._sqs.get_queue_attributes(
                QueueUrl=self.dead_letter_url, AttributeNames=names[:1]
            )
            dead = int(dlq["Attributes"].get("ApproximateNumberOfMessages", 0))
        return QueueDepth(
            int(main.get("ApproximateNumberOfMessages", 0)),
            int(main.get("ApproximateNumberOfMessagesNotVisible", 0)),
            dead,
        )


class InMemoryJobQueue:
    """The JobQueue for tests: dedupes, and redelivers what isn't acked once
    `expire_in_flight()` (the visibility timeout) runs out."""

    def __init__(self) -> None:
        self.visible: deque[tuple[MemoryPush, int]] = deque()
        self.in_flight: dict[str, tuple[MemoryPush, int]] = {}
        self.seen: set[str] = set()
        self._next = 0

    async def enqueue(self, push: MemoryPush) -> None:
        if push.dedupe_id not in self.seen:
            self.seen.add(push.dedupe_id)
            self.visible.append((push, 0))

    async def receive(self, wait_seconds: int = 20, max_jobs: int = 10) -> list[Received]:
        out: list[Received] = []
        while self.visible and len(out) < max_jobs:
            push, receives = self.visible.popleft()
            self._next += 1
            receipt = str(self._next)
            self.in_flight[receipt] = (push, receives + 1)
            out.append(Received(push, receipt, receives + 1, time.time()))
        return out

    async def ack(self, received: Received) -> None:
        self.in_flight.pop(received.receipt, None)

    async def depth(self) -> QueueDepth:
        return QueueDepth(len(self.visible), len(self.in_flight), 0)

    def expire_in_flight(self) -> None:
        self.visible.extend(self.in_flight.values())
        self.in_flight.clear()
