"""Who's calling: the authenticated user of every request, and the token that says so.

Every route gets its user from `get_current_user`: the bearer token from POST /auth/login
(routes/auth.py), signed with JWT_SECRET (HS256), whose `sub` must be an account that still
exists, so a deleted account's token stops working at once. Routes take `UserId` (the account's
id) within the user's rate limit, or `BudgetedUserId` when they start a model run and need token
budget left today; `UserCredentials` is the user's own model endpoint and key, if they sent one.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import SecretStr

from chatbot.accounts.users import User
from chatbot.agent.builder import OPENAI_BASE_URL
from chatbot.api.dependencies import Limits, Users
from chatbot.config import ModelCredentials, Settings, get_settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
# Points the OpenAPI docs' "Authorize" button at /auth/login.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def unauthorized(detail: str = "Invalid or expired token") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def create_access_token(settings: Settings, user_id: str) -> tuple[str, int]:
    """A token for `user_id`, and its lifetime in seconds (JWT_TTL_MINUTES)."""
    if settings.jwt_secret is None:
        raise ValueError("JWT_SECRET isn't set")
    now = datetime.now(UTC)
    lifetime = timedelta(minutes=settings.jwt_ttl_minutes)
    claims = {"sub": user_id, "iat": now, "exp": now + lifetime}
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    return token, int(lifetime.total_seconds())


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    users: Users,
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    if settings.jwt_secret is None:
        raise unauthorized("Authentication isn't configured (JWT_SECRET)")
    if token is None:
        raise unauthorized("Not signed in")
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except jwt.InvalidTokenError as e:
        logger.info("token refused: %s", type(e).__name__)  # e.g. ExpiredSignatureError
        raise unauthorized() from e
    user = await users.get(claims["sub"])
    if user is None:  # a token for an account that no longer exists
        logger.info("token refused: unknown user %s", claims["sub"])
        raise unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def limited_user(request: Request, user: CurrentUser, limits: Limits) -> str:
    """The signed-in user's id, within their rate limit."""
    request.state.user_id = user.id  # for the request's log line (api/middleware.py)
    if limits:
        await limits.limit_user(user.id)
    return user.id


async def budgeted_user(user_id: Annotated[str, Depends(limited_user)], limits: Limits) -> str:
    """The user, with token budget left today: for the routes that start a model run."""
    if limits:
        await limits.check_budget(user_id)
    return user_id


async def model_credentials(
    x_openai_key: Annotated[str | None, Header()] = None,
    x_openai_base_url: Annotated[str | None, Header()] = None,
) -> ModelCredentials | None:
    """The user's own endpoint and key, if the client sent them (checked at login)."""
    if not x_openai_key:
        return None
    return ModelCredentials(x_openai_base_url or OPENAI_BASE_URL, SecretStr(x_openai_key))


UserId = Annotated[str, Depends(limited_user)]
BudgetedUserId = Annotated[str, Depends(budgeted_user)]
UserCredentials = Annotated[ModelCredentials | None, Depends(model_credentials)]
