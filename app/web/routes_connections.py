"""Подключения: список (с видимостью по ролям), создание, изменение, проверка, вкл/выкл."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.bot import (
    _check_database_url,
    _fetch_visible_connections,
    _set_connection_active_status,
)
from app.connection_display import parse_database_url
from app.models import Connection
from app.web.deps import is_admin, load_actor, paginate, push_flash, render
from app.web.service import (
    actor_telegram_id,
    check_connection_record,
    connection_audience_text,
    create_connection_from_form,
    edit_connection_from_form,
)

router = APIRouter()

ADMIN_ONLY_TEXT = "Недостаточно прав. Раздел доступен только Admin."


def _connection_view(connection: Connection) -> dict:
    host, database, user = parse_database_url(connection.database_url)
    creator = "-"
    if connection.created_by:
        creator = f"#{connection.created_by.id} {connection.created_by.full_name}"
        if connection.created_by.telegram_username:
            creator += f" (@{connection.created_by.telegram_username})"
    return {
        "id": connection.id,
        "name": connection.name,
        "host": host,
        "database": database,
        "db_user": user,
        "audience": connection_audience_text(connection),
        "creator": creator,
        "is_active": connection.is_active,
    }


def _admin_guard(request: Request, actor) -> RedirectResponse | None:
    if not is_admin(actor):
        push_flash(request, actor.id, "err", ADMIN_ONLY_TEXT)
        return RedirectResponse("/connections", status_code=303)
    return None


def _redirect_list() -> RedirectResponse:
    return RedirectResponse("/connections", status_code=303)


@router.get("/connections")
async def connections_list(request: Request, page: int = 0):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        rows = _fetch_visible_connections(session, actor)
        page_rows, page, total_pages = paginate(rows, page)
        items = [_connection_view(row) for row in page_rows]
    return render(
        request,
        "connections_list.html",
        actor=actor,
        nav="connections",
        items=items,
        page=page,
        total_pages=total_pages,
        total_count=len(rows),
        admin=is_admin(actor),
    )


@router.get("/connections/new")
async def connection_new_form(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
    return render(
        request,
        "connection_form.html",
        actor=actor,
        nav="connections",
        mode="new",
        connection_id=None,
        connection_name=None,
        values={"name": "", "url": "", "roles": ""},
        error=None,
        check_error=None,
        can_force_save=False,
    )


@router.post("/connections/new")
async def connection_new(request: Request):
    form = await request.form()
    name = str(form.get("name", ""))
    url = str(form.get("url", ""))
    roles_raw = str(form.get("roles", ""))
    force = str(form.get("force", "")) == "1"

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard

        values = {"name": name, "url": url, "roles": roles_raw}
        if not name.strip():
            return render(
                request,
                "connection_form.html",
                actor=actor,
                nav="connections",
                mode="new",
                connection_id=None,
                connection_name=None,
                values=values,
                error="Имя не должно быть пустым.",
                check_error=None,
                can_force_save=False,
            )
        if not url.strip():
            return render(
                request,
                "connection_form.html",
                actor=actor,
                nav="connections",
                mode="new",
                connection_id=None,
                connection_name=None,
                values=values,
                error="URL не должен быть пустым.",
                check_error=None,
                can_force_save=False,
            )
        if not force:
            ok, details = _check_database_url(url.strip())
            if not ok:
                return render(
                    request,
                    "connection_form.html",
                    actor=actor,
                    nav="connections",
                    mode="new",
                    connection_id=None,
                    connection_name=None,
                    values=values,
                    error=None,
                    check_error=details[:1500],
                    can_force_save=True,
                )
        ok, message = create_connection_from_form(session, actor, name, url, roles_raw)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_list()


@router.get("/connections/{connection_id}/edit")
async def connection_edit_form(request: Request, connection_id: int):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        connection = session.get(Connection, connection_id)
        if connection is None:
            push_flash(request, actor.id, "err", "Подключение не найдено.")
            return _redirect_list()
        values = {
            "name": connection.name,
            "url": connection.database_url,
            "roles": connection.allowed_roles,
        }
        connection_name = connection.name
    return render(
        request,
        "connection_form.html",
        actor=actor,
        nav="connections",
        mode="edit",
        connection_id=connection_id,
        connection_name=connection_name,
        values=values,
        error=None,
        check_error=None,
        can_force_save=False,
    )


@router.post("/connections/{connection_id}/edit")
async def connection_edit(request: Request, connection_id: int):
    form = await request.form()
    name = str(form.get("name", ""))
    url = str(form.get("url", ""))
    roles_raw = str(form.get("roles", ""))
    force = str(form.get("force", "")) == "1"

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        connection = session.get(Connection, connection_id)
        if connection is None:
            push_flash(request, actor.id, "err", "Подключение не найдено.")
            return _redirect_list()

        if url.strip() and url.strip() != connection.database_url and not force:
            ok, details = _check_database_url(url.strip())
            if not ok:
                return render(
                    request,
                    "connection_form.html",
                    actor=actor,
                    nav="connections",
                    mode="edit",
                    connection_id=connection_id,
                    connection_name=connection.name,
                    values={"name": name, "url": url, "roles": roles_raw},
                    error=None,
                    check_error=details[:1500],
                    can_force_save=True,
                )
        ok, message = edit_connection_from_form(
            session, actor, connection, name, url, roles_raw
        )
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_list()


@router.post("/connections/{connection_id}/check")
async def connection_check(request: Request, connection_id: int):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        connection = session.get(Connection, connection_id)
        if connection is None:
            push_flash(request, actor.id, "err", "Подключение не найдено.")
            return _redirect_list()
        ok, message = check_connection_record(session, actor, connection)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_list()


@router.post("/connections/{connection_id}/toggle")
async def connection_toggle(request: Request, connection_id: int):
    form = await request.form()
    activate = str(form.get("state", "on")) == "on"
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        ok, message = _set_connection_active_status(
            session,
            actor_telegram_id(actor),
            str(connection_id),
            activate=activate,
        )
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_list()
