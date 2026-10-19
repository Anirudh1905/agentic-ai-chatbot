"""Who's calling: accounts, logging in, and the authenticated user of every request.

- **Routes:** POST /auth/register, POST /auth/login (the OAuth2 password flow, returning a
  JWT) and GET /auth/me. Register and log in are limited per IP (RATE_LIMIT_LOGIN).
- **Every other route** gets its user from `get_current_user`: the bearer token, signed with
  JWT_SECRET (HS256), whose `sub` must be an account that still exists. A deleted account's
  token stops working at once. Routes take `UserId` (the account's id) within the user's rate
  limit, or `BudgetedUserId` when they start a model run and need token budget left today.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import SecretStr

from chatbot.accounts.users import EmailTaken, User
from chatbot.agent.builder import OPENAI_BASE_URL, check_credentials
from chatbot.api.dependencies import Limits, Users, limit_login
from chatbot.api.schemas import RegisterRequest, TokenResponse, UserResponse
from chatbot.config import ModelCredentials, Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
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


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_login)],
)
async def register(body: RegisterRequest, users: Users) -> UserResponse:
    try:
        user = await users.create(body.email, body.password)
    except EmailTaken:
        logger.info("register refused: email already registered")
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    logger.info("user registered: %s", user.id)
    return UserResponse(id=user.id, email=user.email)


@router.post("/login", dependencies=[Depends(limit_login)])
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    users: Users,
    settings: Annotated[Settings, Depends(get_settings)],
    request: Request,
    openai_base_url: Annotated[str | None, Form()] = None,
    openai_api_key: Annotated[str | None, Form()] = None,
) -> TokenResponse:
    """The OAuth2 password flow: form fields `username` (the email) and `password`.

    Optionally `openai_api_key` (and `openai_base_url`, default OpenAI's) for the user to chat
    with their own endpoint and key. They're checked here, and the login fails if they don't
    work; the server doesn't keep them: the client sends them with each run request
    (X-OpenAI-Key, X-OpenAI-Base-URL).
    """
    if openai_api_key:
        credentials = ModelCredentials(
            openai_base_url or OPENAI_BASE_URL, SecretStr(openai_api_key)
        )
        if problem := await check_credentials(settings, credentials):
            logger.info("login refused: model credentials don't work (%s)", problem)
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Incorrect OpenAI base URL or API key: {problem}"
            )
    user = await users.authenticate(form.username, form.password)
    if user is None:
        # The same answer for an unknown email and a wrong password; no email in the log
        # (failed logins are often typos of real addresses).
        logger.warning("login failed (client %s)", request.client.host if request.client else "?")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(settings, user.id)
    logger.info("logged in: %s", user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me")
async def me(user: CurrentUser) -> UserResponse:
    return UserResponse(id=user.id, email=user.email)
