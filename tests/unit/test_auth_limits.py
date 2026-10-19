"""Accounts and JWT authentication (api/auth.py), and rate limits and the token budget
(accounts/limits.py), over the API. Limits use the `limits` library's in-memory store here;
Redis in the app. Every test client has the accounts alice, bob and carol (apiclient.ACCOUNTS)."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import openai
import pytest
from apiclient import ALICE, AUTH, BOB, auth, client_for, parse_sse, scripted
from fakes import ScriptedChatModel
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from chatbot.accounts.limits import UsageLimits
from chatbot.agent import builder
from chatbot.agent.builder import check_credentials
from chatbot.agent.middleware import UserModel
from chatbot.api import auth as auth_module
from chatbot.api.dependencies import get_users
from chatbot.api.main import app
from chatbot.config import ModelCredentials, Settings, get_settings

SECRET = AUTH.jwt_secret.get_secret_value()  # type: ignore[union-attr]


def token(key: str = SECRET, **claims: Any) -> dict[str, str]:
    """A bearer header for carol, with `claims` changed (None removes one)."""
    body = {"sub": "carol", "exp": datetime.now(UTC) + timedelta(minutes=5), **claims}
    body = {k: v for k, v in body.items() if v is not None}
    return {"Authorization": f"Bearer {jwt.encode(body, key)}"}


def limits(**settings: Any) -> UsageLimits:
    return UsageLimits(Settings(**settings), storage_uri="async+memory://")


def with_usage(text: str, tokens: int) -> AIMessage:
    usage = {"input_tokens": tokens - 10, "output_tokens": 10, "total_tokens": tokens}
    return AIMessage(text, usage_metadata=usage)  # type: ignore[arg-type]


# --- authentication ----------------------------------------------------------------------


def test_the_user_is_the_tokens_subject() -> None:
    with client_for(scripted("hi carol")) as client:
        created = client.post("/chat", json={"message": "hello"}, headers=auth("carol"))
        assert created.status_code == 200
        assert len(client.get("/threads", headers=auth("carol")).json()) == 1
        assert client.get("/threads", headers=ALICE).json() == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"exp": datetime.now(UTC) - timedelta(minutes=1)}, "ExpiredSignatureError"),
        ({"sub": None}, "MissingRequiredClaimError"),
        ({"exp": None}, "MissingRequiredClaimError"),
        ({"key": "a-different-secret-that-is-long-enough"}, "InvalidSignatureError"),
    ],
)
def test_invalid_tokens_are_refused(
    change: dict[str, Any], reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO), client_for(scripted()) as client:
        r = client.get("/threads", headers=token(**change))
    assert r.status_code == 401
    # One answer for every bad token; the reason is only in the log.
    assert r.json() == {"detail": "Invalid or expired token"}
    assert r.headers["www-authenticate"] == "Bearer"
    assert f"token refused: {reason}" in caplog.text


def test_unsigned_tokens_are_refused() -> None:
    unsigned = jwt.encode({"sub": "carol"}, None, algorithm="none")
    with client_for(scripted()) as client:
        r = client.get("/threads", headers={"Authorization": f"Bearer {unsigned}"})
    assert r.status_code == 401


def test_a_token_for_an_account_that_doesnt_exist_is_refused() -> None:
    with client_for(scripted()) as client:
        assert client.get("/threads", headers=auth("zoe")).status_code == 401
        # Deleted after logging in: the token stops working at once.
        assert client.get("/threads", headers=BOB).status_code == 200
        users = app.dependency_overrides[get_users]()
        del users.rows["bob@example.com"]
        assert client.get("/threads", headers=BOB).status_code == 401


def test_requests_without_a_token_or_a_secret_are_refused() -> None:
    with client_for(scripted()) as client:
        r = client.get("/threads", headers={"Authorization": ""})
        assert (r.status_code, r.json()["detail"]) == (401, "Not signed in")
        unconfigured = Settings(_env_file=None, jwt_secret=None)  # type: ignore[call-arg]
        app.dependency_overrides[get_settings] = lambda: unconfigured
        r = client.get("/threads")
    assert r.status_code == 401 and "isn't configured" in r.json()["detail"]


def test_the_request_log_names_the_authenticated_user(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="chatbot.api"), client_for(scripted()) as client:
        client.get("/threads", headers=BOB)
    assert "GET /threads -> 200" in caplog.text and "(user=bob)" in caplog.text


# --- accounts: register, log in, me -------------------------------------------------------

NO_TOKEN = {"Authorization": ""}


def register(
    client: TestClient, email: str = "Maya@Example.com", password: str = "s3cret-pass"
) -> Any:
    return client.post(
        "/auth/register", json={"email": email, "password": password}, headers=NO_TOKEN
    )


def log_in(
    client: TestClient, email: str = "maya@example.com", password: str = "s3cret-pass"
) -> Any:
    return client.post(
        "/auth/login", data={"username": email, "password": password}, headers=NO_TOKEN
    )


def test_register_log_in_and_use_the_token() -> None:
    with client_for(scripted("hello maya")) as client:
        created = register(client)
        assert created.status_code == 201
        user = created.json()
        assert user["email"] == "maya@example.com"  # stored lower-cased

        token = log_in(client, email="MAYA@example.com")  # any case logs in
        assert token.status_code == 200
        body = token.json()
        assert body["token_type"] == "bearer" and body["expires_in"] == 3600
        headers = {"Authorization": f"Bearer {body['access_token']}"}

        assert client.get("/auth/me", headers=headers).json() == user
        # The account's id is the user: its threads are its own.
        assert client.post("/chat", json={"message": "hi"}, headers=headers).status_code == 200
        assert len(client.get("/threads", headers=headers).json()) == 1
        assert client.get("/threads", headers=ALICE).json() == []


def test_a_wrong_password_and_an_unknown_email_get_the_same_answer() -> None:
    with client_for(scripted()) as client:
        register(client)
        wrong = log_in(client, password="not-the-password")
        unknown = log_in(client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Incorrect email or password"}


def test_an_email_registers_once_and_bad_input_is_refused() -> None:
    with client_for(scripted()) as client:
        assert register(client).status_code == 201
        assert register(client, email="maya@EXAMPLE.com").status_code == 409
        assert register(client, email="not-an-email").status_code == 422
        assert register(client, email="ok@example.com", password="short").status_code == 422


def test_passwords_are_stored_hashed() -> None:
    with client_for(scripted()) as client:
        register(client)
        users = app.dependency_overrides[get_users]()
    _, stored = users.rows["maya@example.com"]
    assert "s3cret-pass" not in stored and stored.startswith("$argon2id$")


def test_logging_in_is_limited_per_ip() -> None:
    with client_for(scripted(), usage=limits(rate_limit_login="3/minute")) as client:
        register(client)
        assert [log_in(client, password="guess").status_code for _ in range(2)] == [401, 401]
        guessed = log_in(client)  # even the right password, once over the limit
        assert guessed.status_code == 429 and "retry-after" in guessed.headers
        assert client.get("/threads").status_code == 200  # other routes aren't affected


def test_me_is_the_signed_in_account() -> None:
    with client_for(scripted()) as client:
        assert client.get("/auth/me").json() == {"id": "alice", "email": "alice@example.com"}


# --- the user's own OpenAI credentials -----------------------------------------------------


def models_and_ping(models: dict[str, Any] | list[Any], status: int = 200, error: str = "") -> Any:
    """A fake OpenAI-compatible endpoint: GET /models, and POST /chat/completions."""

    def handle(request: httpx.Request) -> httpx.Response:
        # The key both ways: OpenAI reads Bearer, Azure-style gateways `api-key`.
        assert request.headers["authorization"] == "Bearer sk-test"
        assert request.headers["api-key"] == "sk-test"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=models)
        if status == 200:
            return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
        return httpx.Response(status, json={"error": {"message": error}})

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("transport", "problem"),
    [
        (models_and_ping({"data": [{"id": "gpt-5.4"}]}), None),
        (models_and_ping([{"value": "gpt-5.4"}]), None),  # the gateway's shape
        (models_and_ping({"data": [{"id": "gpt-4o"}]}), "The model gpt-5.4 isn't available there"),
        (
            models_and_ping({"data": [{"id": "gpt-5.4"}]}, 401, "Incorrect API key"),
            "refused (Incorrect API key)",
        ),
        # Listing models doesn't prove the key works: some gateways don't check it there.
        (
            models_and_ping({"data": [{"id": "gpt-5.4"}]}, 400, "Unreadable token"),
            "refused (Unreadable token)",
        ),
        (httpx.MockTransport(lambda r: httpx.Response(404)), "isn't an OpenAI-compatible API"),
    ],
)
async def test_checking_a_users_credentials(
    transport: httpx.MockTransport, problem: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(builder.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    credentials = ModelCredentials("https://llm.example/v1", SecretStr("sk-test"))
    result = await check_credentials(Settings(model="gpt-5.4"), credentials)
    assert result == problem if problem is None else problem in (result or "")


async def test_an_unreachable_endpoint() -> None:
    credentials = ModelCredentials("https://no-such-host.invalid/v1", SecretStr("sk-test"))
    assert "Can't reach" in (await check_credentials(Settings(), credentials) or "")


def test_the_users_model_sends_the_key_both_ways() -> None:
    credentials = ModelCredentials("https://llm.example/v1", SecretStr("sk-mine"))
    llm = builder.build_llm(Settings(), credentials=credentials)
    assert llm.openai_api_base == "https://llm.example/v1"
    assert llm.openai_api_key.get_secret_value() == "sk-mine"  # sent as Bearer
    assert llm.default_headers == {"api-key": "sk-mine"}
    assert builder.build_llm(Settings()).default_headers is None  # the server's model: unchanged


def test_login_fails_when_the_users_credentials_dont_work(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[ModelCredentials] = []

    async def refuse(settings: Settings, credentials: ModelCredentials) -> str:
        checked.append(credentials)
        return "The API key was refused"

    monkeypatch.setattr(auth_module, "check_credentials", refuse)
    with client_for(scripted()) as client:
        register(client)
        form = {
            "username": "maya@example.com",
            "password": "s3cret-pass",
            "openai_api_key": "sk-bad",
        }
        r = client.post("/auth/login", data=form, headers=NO_TOKEN)
    assert r.status_code == 400
    assert r.json()["detail"] == "Incorrect OpenAI base URL or API key: The API key was refused"
    assert checked[0].base_url == "https://api.openai.com/v1"  # the default when only a key


def test_a_users_runs_use_their_own_model(monkeypatch: pytest.MonkeyPatch) -> None:
    used: list[ModelCredentials] = []

    def theirs(self: UserModel, credentials: ModelCredentials) -> Any:
        used.append(credentials)
        return scripted("from your model")

    monkeypatch.setattr(UserModel, "model_for", theirs)
    with client_for(scripted("from the server's model")) as client:
        own = {"X-OpenAI-Key": "sk-mine", "X-OpenAI-Base-URL": "https://llm.example/v1"}
        assert (
            client.post("/chat", json={"message": "hi"}, headers=own).json()["reply"]
            == "from your model"
        )
        assert (
            client.post("/chat", json={"message": "hi"}).json()["reply"]
            == "from the server's model"
        )
    assert used[0].base_url == "https://llm.example/v1"
    assert used[0].api_key.get_secret_value() == "sk-mine"


def test_a_users_key_that_stops_working_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        response = httpx.Response(401, request=httpx.Request("POST", "https://llm.example"))
        raise openai.AuthenticationError("Incorrect API key", response=response, body=None)

    class Refusing(ScriptedChatModel):  # refuses streamed calls too
        _generate = _stream = refuse  # type: ignore[assignment]

        async def _astream(self, *args: Any, **kwargs: Any) -> Any:
            refuse()
            yield

    monkeypatch.setattr(UserModel, "model_for", lambda self, c: Refusing(responses=[]))
    own = {"X-OpenAI-Key": "sk-revoked"}
    with client_for(scripted()) as client:
        r = client.post("/chat", json={"message": "hi"}, headers=own)
        assert r.status_code == 400 and "credentials were refused (401)" in r.json()["detail"]
        streamed = parse_sse(client.post("/chat/stream", json={"message": "hi"}, headers=own).text)
    assert streamed[-1][0] == "error" and "credentials were refused" in streamed[-1][1]["message"]


# --- rate limits -------------------------------------------------------------------------


def test_a_user_over_their_rate_limit_gets_429_and_others_dont() -> None:
    with client_for(scripted(), usage=limits(rate_limit_per_user="2/minute")) as client:
        assert [client.get("/threads").status_code for _ in range(2)] == [200, 200]
        limited = client.get("/threads")
        assert limited.status_code == 429
        assert 0 < int(limited.headers["retry-after"]) <= 60
        assert client.get("/threads", headers=BOB).status_code == 200


def test_the_ip_limit_counts_every_user_and_comes_before_authentication() -> None:
    with client_for(scripted(), usage=limits(rate_limit_per_ip="3/minute")) as client:
        assert client.get("/threads", headers=ALICE).status_code == 200
        assert client.get("/threads", headers=BOB).status_code == 200
        assert client.get("/threads", headers={"Authorization": ""}).status_code == 401
        # Over the limit: refused before the token is even looked at.
        assert client.get("/threads", headers={"Authorization": ""}).status_code == 429
        assert client.get("/healthz").status_code == 200  # probes aren't limited


def test_the_daily_token_budget_stops_new_runs_once_spent() -> None:
    llm = scripted(with_usage("one", 60), with_usage("two", 60), with_usage("three", 60))
    usage = limits(daily_token_budget=100)
    with client_for(llm, usage=usage) as client:
        assert client.post("/chat", json={"message": "a"}).status_code == 200  # 60 used
        # Still under the budget when it starts, so it runs (and goes over).
        assert client.post("/chat", json={"message": "b"}).status_code == 200  # 120 used
        spent = client.post("/chat", json={"message": "c"})
        assert spent.status_code == 429
        assert "budget" in spent.json()["detail"] and "retry-after" in spent.headers
        # Only runs need budget: reading threads still works, and other users aren't affected.
        assert client.get("/threads").status_code == 200
        assert client.post("/chat", json={"message": "d"}, headers=BOB).status_code == 200
        assert client.portal.call(usage.used_today, "alice") == 120  # type: ignore[union-attr]


def test_if_redis_is_down_requests_go_through(caplog: pytest.LogCaptureFixture) -> None:
    down = UsageLimits(Settings(redis_url="redis://localhost:1/0"))
    with caplog.at_level(logging.WARNING), client_for(scripted("hi"), usage=down) as client:
        assert client.post("/chat", json={"message": "hello"}).status_code == 200
    assert "is Redis up?" in caplog.text


def test_the_budget_can_be_turned_off() -> None:
    llm = scripted(with_usage("one", 500), with_usage("two", 500))
    with client_for(llm, usage=limits(daily_token_budget=0)) as client:
        assert [client.post("/chat", json={"message": m}).status_code for m in "ab"] == [200, 200]


def test_the_test_client_is_alice() -> None:
    # The suite's default user, so the other API tests run as an authenticated user.
    with client_for(scripted()) as client:
        assert isinstance(client, TestClient)
        assert client.headers["authorization"].startswith("Bearer ")
