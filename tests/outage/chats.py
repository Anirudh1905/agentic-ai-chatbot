"""Chats for the outage test (deploy/outage-test.sh): N knowledge base questions through the
gateway, each on a new thread, with the load test's client (tests/load/run.py). Prints one line
per turn and exits 1 if any failed. The account is deleted at the end.

    PYTHONPATH=tests uv run python tests/outage/chats.py N
"""

import asyncio
import sys

import httpx
from load.run import BASE, kb_questions, login, turn


async def main(n: int) -> int:
    questions = kb_questions()
    failed = 0
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        token = await login(client, 0)
        try:
            for i in range(n):
                result, _ = await turn(client, token, "first", questions[i % len(questions)], None)
                failed += result.error is not None
                ttft = f"{result.first_token:.1f}s" if result.first_token else "-"
                print(f"  turn {i + 1}: {result.error or 'ok'}, first token {ttft}", flush=True)
        finally:
            await client.delete("/api/me", headers={"Authorization": f"Bearer {token}"})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]))))
