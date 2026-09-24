"""Разделы Admin: пользователи и роли."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.bot import (
    PROTECTED_ROLE_NAMES,
    _create_role_record,
    _fetch_all_users,
    _role_connections_count,
    _role_users_count,
)
from app.models import Role, User
from app.web.deps import is_admin, load_actor, push_flash, render
from app.web.service import (
    actor_telegram_id,
    add_role_web,
    delete_role_record,
    remove_role_web,
    rename_role_record,
    set_user_active_web,
    update_role_description_record,
)

router = APIRouter()

ADMIN_ONLY_TEXT = "Недостаточно прав. Раздел доступен только Admin."


def _admin_guard(request: Request, actor) -> RedirectResponse | None:
    if not is_admin(actor):
        push_flash(request, actor.id, "err", ADMIN_ONLY_TEXT)
        return RedirectResponse("/", status_code=303)
    return None


def _redirect_users() -> RedirectResponse:
    return RedirectResponse("/users", status_code=303)


def _redirect_roles() -> RedirectResponse:
    return RedirectResponse("/roles", status_code=303)


# --- Пользователи ---


@router.get("/users")
async def users_list(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        rows = _fetch_all_users(session)
        all_roles = session.query(Role).order_by(Role.name).all()
        users = []
        for row in rows:
            users.append(
                {
                    "id": row.id,
                    "full_name": row.full_name,
                    "username": row.telegram_username,
                    "telegram_id": row.telegram_user_id,
                    "roles": [
                        {"name": r.name, "removable": r.name != "Admin"} for r in row.roles
                    ],
                    "is_active": row.is_active,
                }
            )
        role_options = [{"id": r.id, "name": r.name} for r in all_roles]
    return render(
        request,
        "users_list.html",
        actor=actor,
        nav="users",
        users=users,
        role_options=role_options,
    )


@router.post("/users/{user_id}/status")
async def user_status(request: Request, user_id: int):
    form = await request.form()
    activate = str(form.get("state", "on")) == "on"
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        ok, message = set_user_active_web(session, actor, user_id, activate)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_users()


@router.post("/users/{user_id}/add_role")
async def user_add_role(request: Request, user_id: int):
    form = await request.form()
    raw_role_id = str(form.get("role_id", "")).strip()
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        if not raw_role_id.isdigit():
            push_flash(request, actor.id, "err", "Выберите роль.")
            return _redirect_users()
        ok, message = add_role_web(session, actor, user_id, int(raw_role_id))
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_users()


@router.post("/users/{user_id}/remove_role")
async def user_remove_role(request: Request, user_id: int):
    form = await request.form()
    role_name = str(form.get("role_name", "")).strip()
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        target_user = session.get(User, user_id)
        if target_user is None:
            push_flash(request, actor.id, "err", "Пользователь не найден.")
            return _redirect_users()
        ok, message = remove_role_web(session, actor, target_user, role_name)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_users()


# --- Роли ---


def _role_view(session, role: Role) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description or "",
        "users_count": _role_users_count(session, role.id),
        "connections_count": _role_connections_count(session, role.name),
        "protected": role.name in PROTECTED_ROLE_NAMES,
    }


@router.get("/roles")
async def roles_list(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        role_rows = session.query(Role).order_by(Role.id).all()
        roles = [_role_view(session, role) for role in role_rows]
    return render(
        request,
        "roles_list.html",
        actor=actor,
        nav="roles",
        roles=roles,
        new_values={"name": "", "description": ""},
        new_error=None,
    )


@router.post("/roles/new")
async def role_new(request: Request):
    form = await request.form()
    name = str(form.get("name", ""))
    description = str(form.get("description", ""))
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        if not name.strip():
            push_flash(request, actor.id, "err", "Название роли не должно быть пустым.")
            return _redirect_roles()
        ok, message = _create_role_record(
            session, actor_telegram_id(actor), name, description
        )
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_roles()


@router.get("/roles/{role_id}/edit")
async def role_edit_form(request: Request, role_id: int):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        role = session.get(Role, role_id)
        if role is None:
            push_flash(request, actor.id, "err", "Роль не найдена.")
            return _redirect_roles()
        values = {"name": role.name, "description": role.description or ""}
        role_name = role.name
    return render(
        request,
        "role_form.html",
        actor=actor,
        nav="roles",
        role_id=role_id,
        role_name=role_name,
        values=values,
        error=None,
    )


@router.post("/roles/{role_id}/edit")
async def role_edit(request: Request, role_id: int):
    form = await request.form()
    name = str(form.get("name", ""))
    description = str(form.get("description", ""))
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        role = session.get(Role, role_id)
        if role is None:
            push_flash(request, actor.id, "err", "Роль не найдена.")
            return _redirect_roles()

        ok = True
        message = "Изменений нет."
        if name.strip() and name.strip() != role.name:
            ok, message = rename_role_record(session, actor, role_id, name)
        if ok and description != (role.description or ""):
            ok2, message2 = update_role_description_record(
                session, actor, role_id, description
            )
            ok = ok and ok2
            message = message2 if message == "Изменений нет." else f"{message}\n{message2}"
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_roles()


@router.post("/roles/{role_id}/delete")
async def role_delete(request: Request, role_id: int):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        guard = _admin_guard(request, actor)
        if guard is not None:
            return guard
        ok, message = delete_role_record(session, actor, role_id)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_roles()
