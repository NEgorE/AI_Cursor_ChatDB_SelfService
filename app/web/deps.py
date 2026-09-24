"""Общие зависимости веб-интерфейса: текущий пользователь, флеш-сообщения, рендер."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models import User
from app.web.auth import SESSION_COOKIE, decode_session


class LoginRequired(Exception):
    """Нет/просрочена сессия — редирект на страницу входа."""


class BlockedUser(Exception):
    """Пользователь деактивирован — показываем экран блокировки."""

    def __init__(self, user: User) -> None:
        self.user = user


def is_admin(actor: User) -> bool:
    return actor.role is not None and actor.role.name == "Admin"


PAGE_SIZE = 10


def paginate(rows: list, page: int) -> tuple[list, int, int]:
    """Нарезать список по страницам → (страница, номер, всего страниц)."""
    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    return rows[start : start + PAGE_SIZE], page, total_pages


def resolve_user_id(request: Request) -> int | None:
    signer = request.app.state.signer
    return decode_session(signer, request.cookies.get(SESSION_COOKIE))


def load_actor_optional(request: Request, session: Session) -> User | None:
    """Пользователь по cookie или None (без исключений) — для страницы входа."""
    user_id = resolve_user_id(request)
    if user_id is None:
        return None
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def load_actor(request: Request, session: Session) -> User:
    user_id = resolve_user_id(request)
    if user_id is None:
        raise LoginRequired()
    user = session.get(User, user_id)
    if user is None:
        raise LoginRequired()
    if not user.is_active:
        raise BlockedUser(user)
    return user


# --- Флеш-сообщения (в памяти процесса: бот и веб живут в одном процессе) ---


def push_flash(request: Request, user_id: int, category: str, text: str) -> None:
    store: dict[int, list[tuple[str, str]]] = request.app.state.flash_store
    store.setdefault(user_id, []).append((category, text))


def pop_flashes(request: Request, user_id: int) -> list[tuple[str, str]]:
    store: dict[int, list[tuple[str, str]]] = request.app.state.flash_store
    return store.pop(user_id, [])


def render(
    request: Request,
    template: str,
    *,
    actor: User | None = None,
    nav: str = "",
    status_code: int = 200,
    **context: Any,
):
    templates: Jinja2Templates = request.app.state.templates
    ctx: dict[str, Any] = {
        "request": request,
        "actor": actor,
        "role_name": actor.role.name if actor is not None and actor.role else "-",
        "is_admin": is_admin(actor) if actor is not None else False,
        "nav": nav,
        "flashes": pop_flashes(request, actor.id) if actor is not None else [],
        "web_url": str(request.base_url).rstrip("/"),
    }
    ctx.update(context)
    return templates.TemplateResponse(
        request=request, name=template, context=ctx, status_code=status_code
    )
