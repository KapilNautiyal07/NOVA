"""NOVA backend entrypoint."""
from __future__ import annotations

import logging
import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .database import Base, engine
from .routers import assistant, auth, integrations, resources
from .services.common import NotFound

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _origin_regex(spec: str) -> str:
    parts = []
    for origin in (o.strip() for o in spec.split(",") if o.strip()):
        pat = re.escape(origin).replace(r"\*", "[a-z0-9]+")
        if origin.startswith(("http://localhost", "http://127.0.0.1")) and ":" not in origin.split("//", 1)[1]:
            pat += r"(:\d+)?"
        parts.append(pat)
    return "^(" + "|".join(parts) + ")$"


def create_app(create_tables: bool = True) -> FastAPI:
    app = FastAPI(
        title="NOVA API",
        version="1.0.0",
        description="Backend for the NOVA AI browser assistant (LangGraph agents, reminders, tasks, memory, files).",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_origin_regex(settings.allowed_origins),
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(NotFound)
    def _not_found(_: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/health", tags=["meta"])
    def health():
        return {"status": "ok", "app": settings.app_name, "database": engine.dialect.name}

    app.include_router(auth.router)
    app.include_router(resources.router)
    app.include_router(assistant.router)
    app.include_router(integrations.router)

    if create_tables and engine.dialect.name == "sqlite":
        # Dev convenience. PostgreSQL schemas are managed by Alembic migrations.
        Base.metadata.create_all(engine)
    return app


app = create_app()
