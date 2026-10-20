"""`make load`: a load test of the deployed app, through the gateway (Phase 9).

USERS virtual users, each with its own account, start RAMP_SECONDS apart: registering and
logging in are two of the 10 attempts a minute allowed per IP, and every user here comes from
one IP. Each repeats a
conversation until DURATION seconds have passed: a knowledge base question on a new thread, a
follow-up on the same thread (the turn the prompt cache can serve), and a pause. Every
WEB_EVERY-th conversation asks one of a few web questions instead, so the web search cache
sees repeats. The questions avoid HITL_SENSITIVE_TERMS, so no run waits for an approval.

A turn is timed from the request to its first token and to its `done` event. It fails on a
status other than 200, an `error` event, or no `done`. The run fails on any failed turn or
when the p95 time to first token is over P95_TTFT_SECONDS. A summary is printed and written
to tests/load/report.json. Each user deletes its account at the end.

    make load USERS=10 DURATION=300
"""

import asyncio
import json
import os
import random
import secrets
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("BASE", "http://localhost:8088")
USERS = int(os.environ.get("USERS", "10"))
DURATION = float(os.environ.get("DURATION", "300"))
RAMP_SECONDS = float(os.environ.get("RAMP_SECONDS", "13"))
P95_TTFT_SECONDS = float(os.environ.get("P95_TTFT_SECONDS", "10"))
THINK_SECONDS = (2.0, 5.0)
WEB_EVERY = 4
REPORT = Path(__file__).with_name("report.json")
GOLDEN = Path(__file__).parents[1] / "evals" / "golden.jsonl"

FOLLOW_UPS = [
    "Can you sum that up in one sentence?",
    "What should I do first?",
    "Is there any exception to that?",
]
WEB_QUESTIONS = [
    "What is REI's return policy?",
    "What is Patagonia's warranty policy?",
    "What's the weather in Denver today?",
]


def kb_questions() -> list[str]:
    cases = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    return [c["question"] for c in cases if c.get("tools") == ["search_knowledge_base"]]


@dataclass
class Turn:
    kind: str  # "first", "follow_up" or "web"
    status: int = 0
    first_token: float | None = None
    total: float | None = None
    error: str | None = None
    events: dict[str, int] = field(default_factory=dict)


async def login(client: httpx.AsyncClient, n: int) -> str:
    email = f"load-{int(time.time())}-{n}@example.com"
    password = secrets.token_urlsafe(16)
    # Retried on the login limit (429) and on a dropped connection, so a busy start or a pod
    # restarting doesn't end the run before it begins.
    for attempt in range(5):
        try:
            registered = await client.post(
                "/api/auth/register", json={"email": email, "password": password}
            )
            if registered.status_code != 429:
                registered.raise_for_status()
                token = await client.post(
                    "/api/auth/login", data={"username": email, "password": password}
                )
                token.raise_for_status()
                return str(token.json()["access_token"])
        except httpx.TransportError:
            pass
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:  # the login itself hit the limit
                raise
        # A fresh address: the last one may have registered.
        email = f"load-{int(time.time())}-{n}-{attempt}@example.com"
        await asyncio.sleep(15 * (attempt + 1))
    raise RuntimeError(f"user {n} couldn't register and log in")


async def turn(
    client: httpx.AsyncClient, token: str, kind: str, message: str, thread_id: str | None
) -> tuple[Turn, str | None]:
    result = Turn(kind)
    start = time.monotonic()
    body: dict[str, Any] = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    try:
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(120, connect=10),
        ) as response:
            result.status = response.status_code
            if response.status_code != 200:
                result.error = f"status {response.status_code}: {(await response.aread())[:200]!r}"
                return result, thread_id
            event = ""
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line.removeprefix("event:").strip()
                    result.events[event] = result.events.get(event, 0) + 1
                    if event == "token" and result.first_token is None:
                        result.first_token = time.monotonic() - start
                elif line.startswith("data:") and event in ("done", "error"):
                    data = json.loads(line.removeprefix("data:").strip())
                    if event == "error":
                        result.error = f"error event: {data}"
                        return result, thread_id
                    result.total = time.monotonic() - start
                    return result, str(data["thread_id"])
            result.error = "the stream ended without done"
    except httpx.HTTPError as e:
        result.error = repr(e)
    return result, thread_id


async def user(n: int, deadline: float, turns: list[Turn]) -> None:
    await asyncio.sleep(n * RAMP_SECONDS)
    questions = kb_questions()
    rng = random.Random(n)
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        token = await login(client, n)
        conversation = 0
        while time.monotonic() < deadline:
            conversation += 1
            if conversation % WEB_EVERY == 0:
                first = ("web", rng.choice(WEB_QUESTIONS))
            else:
                first = ("first", rng.choice(questions))
            result, thread_id = await turn(client, token, *first, None)
            turns.append(result)
            if result.error is None and time.monotonic() < deadline:
                follow_up, _ = await turn(
                    client, token, "follow_up", rng.choice(FOLLOW_UPS), thread_id
                )
                turns.append(follow_up)
            await asyncio.sleep(rng.uniform(*THINK_SECONDS))
        # The account, its threads and memories go when the run ends (DELETE /me), so runs
        # don't pile up test users.
        await client.delete("/api/me", headers={"Authorization": f"Bearer {token}"})


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1]


def summarize(turns: list[Turn], seconds: float) -> dict[str, Any]:
    done = [t for t in turns if t.error is None]
    ttft = [t.first_token for t in done if t.first_token is not None]
    totals = [t.total for t in done if t.total is not None]
    return {
        "users": USERS,
        "seconds": round(seconds),
        "turns": len(turns),
        "failed": len(turns) - len(done),
        "turns_per_minute": round(len(turns) / seconds * 60, 1),
        "first_token_p50": percentile(ttft, 50),
        "first_token_p95": percentile(ttft, 95),
        "total_p50": percentile(totals, 50),
        "total_p95": percentile(totals, 95),
        "resets": sum(t.events.get("reset", 0) for t in turns),
        "by_kind": {
            kind: {
                "turns": len(of_kind := [t for t in done if t.kind == kind]),
                "first_token_p50": percentile(
                    [t.first_token for t in of_kind if t.first_token is not None], 50
                ),
            }
            for kind in ("first", "follow_up", "web")
        },
        "errors": sorted({t.error for t in turns if t.error})[:10],
        "p95_target": P95_TTFT_SECONDS,
    }


async def main() -> int:
    turns: list[Turn] = []
    start = time.monotonic()
    deadline = start + DURATION
    print(f"{USERS} users against {BASE} for {DURATION:.0f}s, starting {RAMP_SECONDS:.0f}s apart")

    async def progress() -> None:
        while True:
            await asyncio.sleep(30)
            failed = sum(t.error is not None for t in turns)
            print(f"  {time.monotonic() - start:4.0f}s: {len(turns)} turns, {failed} failed")

    reporter = asyncio.create_task(progress())
    await asyncio.gather(*(user(n, deadline, turns) for n in range(USERS)))
    reporter.cancel()
    summary = summarize(turns, time.monotonic() - start)
    REPORT.write_text(
        json.dumps({"summary": summary, "turns": [asdict(t) for t in turns]}, indent=2) + "\n"
    )

    def s(value: float | None) -> str:
        return f"{value:.1f}s" if value is not None else "-"

    print(
        f"{summary['turns']} turns ({summary['turns_per_minute']}/min), "
        f"{summary['failed']} failed, {summary['resets']} resets\n"
        f"time to first token: p50 {s(summary['first_token_p50'])}, "
        f"p95 {s(summary['first_token_p95'])} (target {P95_TTFT_SECONDS:.0f}s)\n"
        f"whole turn: p50 {s(summary['total_p50'])}, p95 {s(summary['total_p95'])}"
    )
    for error in summary["errors"]:
        print(f"  error: {error}")
    p95 = summary["first_token_p95"]
    ok = summary["failed"] == 0 and p95 is not None and p95 <= P95_TTFT_SECONDS
    print("ok" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
