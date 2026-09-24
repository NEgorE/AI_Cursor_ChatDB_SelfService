"""Фабрика веб-приложения и управление uvicorn-сервером внутри цикла PTB."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig
from app.web.auth import make_signer
from app.web.deps import BlockedUser, LoginRequired, render

if TYPE_CHECKING:
    from uvicorn import Server as UvicornServer

BASE_DIR = Path(__file__).resolve().parent

LOAD_TIMEOUT_SECONDS = 15.0


def create_web_app(
    *,
    engine,
    config: AppConfig,
    bot_application=None,
) -> FastAPI:
    app = FastAPI(title="ChatDB SelfService", docs_url=None, redoc_url=None, openapi_url=None)
    # Своя фабрика поверх того же движка: без expire_on_commit, иначе
    # обращение к атрибутам actor после commit() внутри хендлеров падает
    # с DetachedInstanceError (сессия на запрос закрывается).
    app.state.session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
        expire_on_commit=False,
    )
    app.state.config = config
    app.state.web_config = config.web
    app.state.bot_application = bot_application
    app.state.signer = make_signer(config.bot_token)
    app.state.flash_store = {}
    app.state.templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    from app.web.routes_admin import router as admin_router
    from app.web.routes_auth import router as auth_router
    from app.web.routes_connections import router as connections_router
    from app.web.routes_dashboard import router as dashboard_router
    from app.web.routes_triggers import router as triggers_router

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(connections_router)
    app.include_router(triggers_router)
    app.include_router(admin_router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(BlockedUser)
    async def _blocked_user(request: Request, exc: BlockedUser):
        return render(request, "blocked.html", user=exc.user, status_code=403)

    return app


def assert_port_available(host: str, port: int) -> None:
    """Быстрая проверка порта до старта uvicorn (иначе он делает sys.exit внутри задачи)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"Адрес {host}:{port} уже занят — освободите порт или поменяйте web.port в config.yaml."
            ) from exc


async def start_web_server(app: FastAPI, host: str, port: int) -> "UvicornServer":
    """Поднять uvicorn в текущем asyncio-цикле (общем с ботом).

    Свои обработчики сигналов uvicorn не ставит: Ctrl+C остаётся у бота,
    веб останавливается через post_shutdown.
    """
    import uvicorn

    assert_port_available(host, port)
    server_config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(server_config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    serve_task = asyncio.create_task(server.serve(), name="chatdb-web-server")
    server.chatdb_serve_task = serve_task  # type: ignore[attr-defined]

    deadline = asyncio.get_running_loop().time() + LOAD_TIMEOUT_SECONDS
    while not server.started:
        if serve_task.done():
            if not server.started:
                raise RuntimeError(f"Веб-сервер не запустился: {serve_task.exception()}")
            break
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError("Веб-сервер не запустился за отведённое время.")
        await asyncio.sleep(0.05)
    return server


async def stop_web_server(server: "UvicornServer") -> None:
    server.should_exit = True
    task = getattr(server, "chatdb_serve_task", None)
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
