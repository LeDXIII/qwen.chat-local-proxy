"""
FastAPI application factory.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
from src.config import API_HOST, API_PORT, LOG_LEVEL, DEBUG_API


def _setup_logging():
    level = logging.DEBUG if DEBUG_API else getattr(logging, LOG_LEVEL, logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s" if DEBUG_API else "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=fmt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger(__name__).info(f"Starting Qwen Local Proxy on {API_HOST}:{API_PORT}")
    if DEBUG_API:
        logging.getLogger(__name__).debug("DEBUG_API is enabled — verbose logging active")
    yield
    logging.getLogger(__name__).info("Shutting down Qwen Local Proxy")


def create_app() -> FastAPI:
    _setup_logging()

    app = FastAPI(
        title="Qwen Local Proxy",
        description="Local proxy for Qwen Chat API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/")
    async def root():
        return {"service": "Qwen Local Proxy", "version": "0.1.0", "status": "running"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


# Module-level instance so `uvicorn src.api.server:app` works directly.
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.api.server:app", host=API_HOST, port=API_PORT, reload=DEBUG_API)
