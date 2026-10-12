from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from chatbot.api.dependencies import resources
from chatbot.api.routes import chat, health
from chatbot.config import get_settings
from chatbot.observability import configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    configure_logging(get_settings())
    yield
    await resources.close()


app = FastAPI(title="Agentic AI Chatbot", lifespan=lifespan)
app.include_router(health.router)
app.include_router(chat.router)
