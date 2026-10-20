"""BACKGROUND_JOBS decides whether a process runs the idle-thread memory loop and the store's TTL
sweeper: the worker does, API pods don't (Phase 9)."""

from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from chatbot.api import dependencies
from chatbot.api.dependencies import Resources
from chatbot.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def settings_for(monkeypatch: pytest.MonkeyPatch, database_url: str) -> Iterator[object]:
    def use(background_jobs: bool) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=database_url,
            openai_api_key=SecretStr("sk-test"),
            reranker_model="",
            rate_limits_enabled=False,
            background_jobs=background_jobs,
        )
        monkeypatch.setattr(dependencies, "get_settings", lambda: settings)

    dependencies.get_limits.cache_clear()
    yield use
    dependencies.get_limits.cache_clear()


@pytest.mark.parametrize("background_jobs", [True, False])
async def test_only_a_background_process_runs_the_loops(
    settings_for: object, background_jobs: bool
) -> None:
    settings_for(background_jobs)  # type: ignore[operator]
    resources = Resources()
    await resources.ensure()
    try:
        assert resources.store is not None and resources.writer is not None
        assert (resources.store._ttl_sweeper_task is not None) is background_jobs
        assert (resources.writer._loop is not None) is background_jobs
    finally:
        await resources.close()
