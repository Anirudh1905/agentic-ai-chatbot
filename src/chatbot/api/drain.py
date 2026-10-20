"""The API pod's preStop hook (Phase 9): `python -m chatbot.api.drain`.

Kubernetes stops sending a terminating pod new requests, but uvicorn, once it gets SIGTERM,
ends every open SSE stream at once (sse-starlette closes them on shutdown). So before SIGTERM,
this waits a moment for the pod to leave the Service, then until no stream is open
(chatbot_active_streams, on METRICS_PORT), up to a timeout inside the pod's grace period. A
stream still cut off can be finished with POST /threads/{id}/resume.
"""

import logging
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable

logger = logging.getLogger("chatbot.api.drain")

GAUGE = re.compile(r"^chatbot_active_streams\s+([0-9.eE+-]+)$", re.MULTILINE)


def active_streams(metrics_text: str) -> float:
    match = GAUGE.search(metrics_text)
    return float(match.group(1)) if match else 0.0


def wait_for_streams(
    read: Callable[[], float], timeout: float, every: float = 1.0, settle: float = 5.0
) -> bool:
    """True once no stream is open, False if `timeout` ran out first."""
    time.sleep(settle)  # the Service drops the pod; no new streams start
    deadline = time.monotonic() + timeout
    while True:
        try:
            if read() == 0:
                return True
        except OSError:
            return True  # metrics unreachable: nothing to wait for
        if time.monotonic() >= deadline:
            return False
        time.sleep(every)


def main() -> int:
    port = os.environ.get("METRICS_PORT", "9100")
    timeout = float(os.environ.get("DRAIN_TIMEOUT_SECONDS", "55"))

    def read() -> float:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
            return active_streams(resp.read().decode())

    drained = wait_for_streams(read, timeout)
    print("drained" if drained else f"streams still open after {timeout:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
