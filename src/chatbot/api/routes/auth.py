"""Accounts and logging in: POST /auth/register, POST /auth/login (the OAuth2 password flow,
returning a JWT) and GET /auth/me. Register and log in are limited per IP (RATE_LIMIT_LOGIN)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import SecretStr

from chatbot.accounts.users import EmailTaken
from chatbot.agent.builder import OPENAI_BASE_URL, check_credentials
from chatbot.api.dependencies import Users, limit_login
from chatbot.api.schemas import RegisterRequest, TokenResponse, UserResponse
from chatbot.api.security import CurrentUser, create_access_token
from chatbot.config import ModelCredentials, Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


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
