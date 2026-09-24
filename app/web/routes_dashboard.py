"""Дашборд: кто я, статистика, ближайшие срабатывания."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.bot import _format_roles_line
from app.web.deps import load_actor, render
from app.web.service import dashboard_stats, next_scheduled_runs

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        stats = dashboard_stats(session, actor)
        runs = next_scheduled_runs(session, actor, request.app.state.bot_application)
        whoami = {
            "id": actor.id,
            "full_name": actor.full_name,
            "username": actor.telegram_username,
            "telegram_id": actor.telegram_user_id,
            "roles_line": _format_roles_line(actor),
            "is_active": actor.is_active,
        }
    return render(
        request,
        "dashboard.html",
        actor=actor,
        nav="dashboard",
        runs=runs,
        whoami=whoami,
        **stats,
    )
