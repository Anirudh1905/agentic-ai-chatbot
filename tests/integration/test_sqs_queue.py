"""SqsJobQueue against ElasticMQ, which stands in for SQS (Phase 9). Each test gets its own FIFO
queue and dead-letter queue. Skipped when ElasticMQ isn't reachable, unless REQUIRE_SQS=1 (CI)."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator

import aioboto3
import pytest

from chatbot.config import Settings
from chatbot.jobs import MemoryPush, SqsJobQueue

pytestmark = pytest.mark.integration

ENDPOINT = os.environ.get("TEST_SQS_ENDPOINT", "http://localhost:9324")
CREDENTIALS = {"aws_access_key_id": "local", "aws_secret_access_key": "local"}


@pytest.fixture
async def queue() -> AsyncIterator[SqsJobQueue]:
    name = f"test-{uuid.uuid4().hex[:8]}"
    session = aioboto3.Session()
    try:
        async with session.client(
            "sqs", endpoint_url=ENDPOINT, region_name="us-east-1", **CREDENTIALS
        ) as sqs:
            dlq = await sqs.create_queue(
                QueueName=f"{name}-dlq.fifo", Attributes={"FifoQueue": "true"}
            )
            dlq_arn = (
                await sqs.get_queue_attributes(
                    QueueUrl=dlq["QueueUrl"], AttributeNames=["QueueArn"]
                )
            )["Attributes"]["QueueArn"]
            main = await sqs.create_queue(
                QueueName=f"{name}.fifo",
                Attributes={
                    "FifoQueue": "true",
                    "VisibilityTimeout": "1",
                    "RedrivePolicy": json.dumps(
                        {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "2"}
                    ),
                },
            )
    except Exception as e:
        if os.environ.get("REQUIRE_SQS") == "1":
            raise
        pytest.skip(f"ElasticMQ not reachable at {ENDPOINT}: {e}")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sqs_endpoint_url=ENDPOINT,
        sqs_queue_url=main["QueueUrl"],
        sqs_dead_letter_queue_url=dlq["QueueUrl"],
        sqs_access_key_id="local",
        sqs_secret_access_key="local",
    )
    q = SqsJobQueue(settings)
    await q.open()
    try:
        yield q
    finally:
        await q.close()


async def test_a_push_goes_through_once_and_is_acked(queue: SqsJobQueue) -> None:
    push = MemoryPush.remember("alice", "I'm vegetarian", None)
    await queue.enqueue(push)
    await queue.enqueue(push)  # deduped by SQS

    got = await queue.receive(wait_seconds=1)
    assert [r.push for r in got] == [push] and got[0].receives == 1
    await queue.ack(got[0])
    await asyncio.sleep(1.5)
    assert await queue.receive(wait_seconds=1) == []


async def test_an_unacked_push_comes_back_then_goes_to_the_dead_letter_queue(
    queue: SqsJobQueue,
) -> None:
    await queue.enqueue(MemoryPush.remember("bob", "my city is Pune", None))
    for receive in (1, 2):
        got = await queue.receive(wait_seconds=2)
        assert got and got[0].receives == receive  # not acked: back after the timeout
        await asyncio.sleep(1.5)
    assert await queue.receive(wait_seconds=1) == []  # moved on after 2 receives
    assert (await queue.depth()).dead == 1
