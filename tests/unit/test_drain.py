"""The API pod's preStop drain (api/drain.py): wait for the open streams to finish before
uvicorn gets SIGTERM, which ends every open SSE stream at once (Phase 9)."""

from chatbot.api import drain


def test_drain_waits_until_no_stream_is_open() -> None:
    counts = iter([2.0, 1.0, 0.0])
    waited = drain.wait_for_streams(lambda: next(counts), timeout=10, every=0, settle=0)
    assert waited is True


def test_drain_gives_up_at_the_timeout() -> None:
    assert drain.wait_for_streams(lambda: 1.0, timeout=0.05, every=0.01, settle=0) is False


def test_the_gauge_is_read_from_the_metrics_text() -> None:
    text = (
        "# HELP chatbot_active_streams x\n"
        "# TYPE chatbot_active_streams gauge\n"
        "chatbot_active_streams 3.0\n"
    )
    assert drain.active_streams(text) == 3.0
    assert drain.active_streams("other_metric 1\n") == 0.0
