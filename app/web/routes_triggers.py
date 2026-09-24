"""Триггеры: список, расписание на сутки, создание/изменение, проверка, вкл/выкл, удаление."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.bot import (
    _delete_trigger_record,
    _fetch_usable_connections,
    _fetch_visible_triggers,
    _send_trigger_notification,
    _trigger_visible_to_user,
    _unschedule_trigger_job,
)
from app.models import Trigger
from app.trigger_service import format_schedule_label
from app.web.deps import load_actor, paginate, push_flash, render
from app.web.service import (
    actor_telegram_id,
    can_manage_trigger,
    check_trigger_record,
    create_trigger_from_form,
    edit_trigger_from_form,
    next_scheduled_runs,
    reschedule_trigger_job,
    set_trigger_active,
)

router = APIRouter()


def _trigger_view(trigger: Trigger, actor) -> dict:
    if trigger.trigger_type == "group":
        type_text = "групповой"
        target = f"chat_id: {trigger.chat_id}"
        if trigger.message_thread_id:
            target += f", топик: {trigger.message_thread_id}"
    else:
        type_text = "личный"
        creator = trigger.created_by
        if creator and creator.telegram_username:
            target = f"@{creator.telegram_username}"
        elif creator:
            target = creator.full_name
        else:
            target = "-"
    return {
        "id": trigger.id,
        "name": trigger.name,
        "type": type_text,
        "target": target,
        "connection_name": trigger.connection.name if trigger.connection else "-",
        "schedule": format_schedule_label(trigger.schedule),
        "message": trigger.message_template or "- (значение запроса)",
        "is_active": trigger.is_active,
        "can_manage": can_manage_trigger(actor, trigger),
    }


def _redirect_list() -> RedirectResponse:
    return RedirectResponse("/triggers", status_code=303)


def _load_visible_trigger(session, request, actor, trigger_id: int):
    trigger = session.get(Trigger, trigger_id)
    if trigger is None or not _trigger_visible_to_user(session, actor, trigger):
        push_flash(request, actor.id, "err", "Триггер не найден или нет доступа.")
        return None
    return trigger


@router.get("/triggers")
async def triggers_list(request: Request, page: int = 0):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        rows = _fetch_visible_triggers(session, actor)
        page_rows, page, total_pages = paginate(rows, page)
        items = [_trigger_view(row, actor) for row in page_rows]
    return render(
        request,
        "triggers_list.html",
        actor=actor,
        nav="triggers",
        items=items,
        page=page,
        total_pages=total_pages,
        total_count=len(rows),
    )


@router.get("/triggers/schedule")
async def triggers_schedule(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        runs = next_scheduled_runs(session, actor, request.app.state.bot_application, limit=500)
    return render(
        request,
        "trigger_schedule.html",
        actor=actor,
        nav="triggers",
        runs=runs,
    )


def _form_values(
    *,
    name: str = "",
    connection_id: str = "",
    trigger_type: str = "personal",
    chat_id: str = "",
    schedule: str = "",
    sql: str = "",
    message_template: str = "",
) -> dict:
    return {
        "name": name,
        "connection_id": connection_id,
        "trigger_type": trigger_type,
        "chat_id": chat_id,
        "schedule": schedule,
        "sql": sql,
        "message_template": message_template,
    }


@router.get("/triggers/new")
async def trigger_new_form(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        connections = _fetch_usable_connections(session, actor)
        if not connections:
            push_flash(
                request,
                actor.id,
                "err",
                "Нет доступных активных подключений. Сначала создайте подключение.",
            )
            return _redirect_list()
        connection_options = [{"id": c.id, "name": c.name} for c in connections]
    return render(
        request,
        "trigger_form.html",
        actor=actor,
        nav="triggers",
        mode="new",
        trigger_id=None,
        trigger_name=None,
        name_editable=True,
        connections=connection_options,
        values=_form_values(),
        error=None,
    )


@router.post("/triggers/new")
async def trigger_new(request: Request):
    form = await request.form()
    values = _form_values(
        name=str(form.get("name", "")),
        connection_id=str(form.get("connection_id", "")),
        trigger_type=str(form.get("trigger_type", "personal")),
        chat_id=str(form.get("chat_id", "")),
        schedule=str(form.get("schedule", "")),
        sql=str(form.get("sql", "")),
        message_template=str(form.get("message_template", "")),
    )

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        connections = _fetch_usable_connections(session, actor)
        connection_options = [{"id": c.id, "name": c.name} for c in connections]

        if not values["name"].strip():
            return render(
                request,
                "trigger_form.html",
                actor=actor,
                nav="triggers",
                mode="new",
                trigger_id=None,
                trigger_name=None,
                name_editable=True,
                connections=connection_options,
                values=values,
                error="Имя не должно быть пустым.",
            )
        if not values["connection_id"].strip():
            return render(
                request,
                "trigger_form.html",
                actor=actor,
                nav="triggers",
                mode="new",
                trigger_id=None,
                trigger_name=None,
                name_editable=True,
                connections=connection_options,
                values=values,
                error="Выберите подключение.",
            )
        if not values["sql"].strip():
            return render(
                request,
                "trigger_form.html",
                actor=actor,
                nav="triggers",
                mode="new",
                trigger_id=None,
                trigger_name=None,
                name_editable=True,
                connections=connection_options,
                values=values,
                error="SQL-запрос не должен быть пустым.",
            )

        ok, message, trigger_id = create_trigger_from_form(
            session,
            actor,
            name=values["name"],
            connection_ref=values["connection_id"],
            trigger_type=values["trigger_type"],
            chat_raw=values["chat_id"],
            schedule_raw=values["schedule"] or "-",
            sql=values["sql"],
            message_raw=values["message_template"],
        )
    if ok and trigger_id is not None:
        reschedule_trigger_job(request.app.state.bot_application, trigger_id)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    if ok:
        return _redirect_list()
    return render(
        request,
        "trigger_form.html",
        actor=actor,
        nav="triggers",
        mode="new",
        trigger_id=None,
        trigger_name=None,
        name_editable=True,
        connections=connection_options,
        values=values,
        error=None,
    )


@router.get("/triggers/{trigger_id}/edit")
async def trigger_edit_form(request: Request, trigger_id: int):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        trigger = _load_visible_trigger(session, request, actor, trigger_id)
        if trigger is None:
            return _redirect_list()
        if not can_manage_trigger(actor, trigger):
            push_flash(
                request,
                actor.id,
                "err",
                "Изменить триггер может только автор или Admin.",
            )
            return _redirect_list()

        connections = _fetch_usable_connections(session, actor)
        options = [{"id": c.id, "name": c.name} for c in connections]
        if trigger.connection is not None and all(
            c["id"] != trigger.connection_id for c in options
        ):
            options.append({"id": trigger.connection_id, "name": trigger.connection.name})

        values = _form_values(
            name=trigger.name,
            connection_id=str(trigger.connection_id),
            trigger_type=trigger.trigger_type,
            chat_id=(
                f"{trigger.chat_id}:{trigger.message_thread_id}"
                if trigger.chat_id is not None and trigger.message_thread_id
                else str(trigger.chat_id) if trigger.chat_id is not None else ""
            ),
            schedule=trigger.schedule or "",
            sql=trigger.sql_query,
            message_template=trigger.message_template or "",
        )
        trigger_name = trigger.name
    return render(
        request,
        "trigger_form.html",
        actor=actor,
        nav="triggers",
        mode="edit",
        trigger_id=trigger_id,
        trigger_name=trigger_name,
        name_editable=False,
        connections=options,
        values=values,
        error=None,
    )


@router.post("/triggers/{trigger_id}/edit")
async def trigger_edit(request: Request, trigger_id: int):
    form = await request.form()
    values = _form_values(
        name=str(form.get("name", "")),
        connection_id=str(form.get("connection_id", "")),
        trigger_type=str(form.get("trigger_type", "personal")),
        chat_id=str(form.get("chat_id", "")),
        schedule=str(form.get("schedule", "")),
        sql=str(form.get("sql", "")),
        message_template=str(form.get("message_template", "")),
    )

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        trigger = _load_visible_trigger(session, request, actor, trigger_id)
        if trigger is None:
            return _redirect_list()
        if not can_manage_trigger(actor, trigger):
            push_flash(
                request,
                actor.id,
                "err",
                "Изменить триггер может только автор или Admin.",
            )
            return _redirect_list()

        ok, message, changed = edit_trigger_from_form(
            session,
            actor,
            trigger,
            connection_ref=values["connection_id"],
            trigger_type=values["trigger_type"],
            chat_raw=values["chat_id"],
            schedule_raw=values["schedule"],
            sql=values["sql"],
            message_raw=values["message_template"],
        )
        trigger_name = trigger.name
    if ok and changed:
        reschedule_trigger_job(request.app.state.bot_application, trigger_id)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_list()


@router.post("/triggers/{trigger_id}/check")
async def trigger_check(request: Request, trigger_id: int):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        trigger = _load_visible_trigger(session, request, actor, trigger_id)
        if trigger is None:
            return _redirect_list()
        result = check_trigger_record(session, actor, trigger)
        view = _trigger_view(trigger, actor)
        sql_query = trigger.sql_query
        trigger_name = trigger.name

        # Результат проверки доставляется и как при срабатывании:
        # личный — автору в личку, групповой — в настроенный чат.
        tg_note = ""
        if result.executed:
            application = request.app.state.bot_application
            if application is None:
                tg_note = (
                    "Бот не запущен (режим «только веб») — "
                    "в Telegram результат не отправлялся."
                )
            else:
                delivered = await _send_trigger_notification(
                    application.bot,
                    trigger,
                    result.ok,
                    result.rendered if result.ok else result.error,
                )
                if trigger.trigger_type == "group":
                    label = f"настроенный чат {trigger.chat_id}"
                    if trigger.message_thread_id:
                        label += f", топик: {trigger.message_thread_id}"
                elif trigger.created_by is not None:
                    creator = trigger.created_by
                    who = (
                        f"@{creator.telegram_username}"
                        if creator.telegram_username
                        else creator.full_name
                    )
                    label = f"личка автора ({who})"
                else:
                    label = "личка автора"
                tg_note = (
                    f"Результат также отправлен в Telegram ({label}) — как при срабатывании."
                    if delivered
                    else f"Не удалось доставить результат в Telegram ({label}) — "
                    "проверьте, что бот добавлен в чат."
                )

    return render(
        request,
        "trigger_check.html",
        actor=actor,
        nav="triggers",
        trigger=view,
        sql_query=sql_query,
        trigger_name=trigger_name,
        result=result,
        tg_note=tg_note,
    )


@router.post("/triggers/{trigger_id}/toggle")
async def trigger_toggle(request: Request, trigger_id: int):
    form = await request.form()
    activate = str(form.get("state", "on")) == "on"
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        trigger = _load_visible_trigger(session, request, actor, trigger_id)
        if trigger is None:
            return _redirect_list()
        ok, message = set_trigger_active(session, actor, trigger, activate)
        trigger_id_resolved = trigger.id
    if ok:
        reschedule_trigger_job(request.app.state.bot_application, trigger_id_resolved)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_list()


@router.post("/triggers/{trigger_id}/delete")
async def trigger_delete(request: Request, trigger_id: int):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        actor = load_actor(request, session)
        trigger = _load_visible_trigger(session, request, actor, trigger_id)
        if trigger is None:
            return _redirect_list()
        ok, message, deleted_id = _delete_trigger_record(
            session, actor_telegram_id(actor), str(trigger.id)
        )
    if ok and deleted_id is not None:
        application = request.app.state.bot_application
        if application is not None:
            _unschedule_trigger_job(application, deleted_id)
    push_flash(request, actor.id, "ok" if ok else "err", message)
    return _redirect_list()
