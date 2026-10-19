from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse

from chatbot.agent.middleware import UserModelError
from chatbot.api import auth
from chatbot.api.dependencies import limit_ip, resources
from chatbot.api.middleware import RequestMiddleware
from chatbot.api.routes import chat, health, me, threads
from chatbot.config import get_settings
from chatbot.observability.logs import configure_logging
from chatbot.observability.metrics import RUN_ERRORS


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    configure_logging(get_settings())
    yield
    await resources.close()


app = FastAPI(title="Agentic AI Chatbot", lifespan=lifespan)
app.add_middleware(RequestMiddleware)


@app.exception_handler(UserModelError)
async def user_model_refused(_: Request, e: UserModelError) -> JSONResponse:
    """The user's own key stopped working mid-session (agent/middleware.py)."""
    RUN_ERRORS.labels("user_model").inc()
    return JSONResponse({"detail": str(e)}, status_code=status.HTTP_400_BAD_REQUEST)


app.include_router(health.router)
# Health probes and metrics aren't limited; everything else is, per client IP (api/dependencies.py).
for router in (auth.router, chat.router, threads.router, me.router):
    app.include_router(router, dependencies=[Depends(limit_ip)])
