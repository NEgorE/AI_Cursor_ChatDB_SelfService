"""Бизнес-логика веб-интерфейса.

Правила видимости, валидации и переходов не копируются: приватные хелперы
app.bot импортируются напрямую, чтобы бот и веб всегда вели себя одинаково.
Веб-операции принимают actor как объект User (сессия веба) и обращаются к
хелперам бота через actor.telegram_user_id — пользователь попадает в веб
только через код из бота, поэтому привязка гарантирована.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bot import (
    PROTECTED_ROLE_NAMES,
    _apply_connection_edits,
    _apply_trigger_edits,
    _check_database_url,
    _connection_visible_to_roles,
    _create_connection_record,
    _create_trigger_record,
    _delete_trigger_record,
    _fetch_usable_connections,
    _fetch_visible_connections,
    _fetch_visible_triggers,
    _format_connection_check_message,
    _format_row_values,
    _normalize_allowed_roles,
    _parse_allowed_roles,
    _remove_roles_from_user,
    _render_trigger_message,
    _resolve_connection_by_id_or_name,
    _resolve_trigger_by_id_or_name,
    _resolve_user_by_id_or_telegram,
    _schedule_trigger_job,
    _set_user_active_status,
    _set_user_role,
    _trigger_job_name,
    _trigger_visible_to_user,
    _unschedule_trigger_job,
    _user_can_use_connection,
    _user_role_names,
)
from app.connection_display import parse_database_url
from app.models import Connection, Role, Trigger, User, UserRole
from app.trigger_service import (
    daily_input_to_utc,
    format_schedule_label,
    iter_schedule_occurrences,
    local_timezone,
    normalize_schedule,
    parse_chat_ref,
    parse_schedule,
    validate_and_execute_row_sql,
)


def actor_telegram_id(actor: User) -> int:
    if actor.telegram_user_id is None:
        raise RuntimeError(
            f"Пользователь #{actor.id} ({actor.full_name}) без привязки к Telegram "
            "не может выполнять операции веб-интерфейса."
        )
    return actor.telegram_user_id


def normalize_schedule_input(raw: str) -> tuple[str | None, str | None]:
    """Сырой ввод расписания → нормализованное значение или текст ошибки."""
    try:
        schedule = normalize_schedule(daily_input_to_utc(raw))
        if schedule is not None:
            parse_schedule(schedule)
    except ValueError as exc:
        return None, str(exc)
    return schedule, None


def parse_chat_id(raw: str) -> tuple[int | None, int | None, str | None]:
    """Чат из числа, ссылки t.me/c/<id>/<топик> или "chat_id:топик" (общий хелпер с ботом)."""
    parsed = parse_chat_ref(raw)
    if parsed is None:
        return None, None, (
            "Не распознал чат. Укажите числовой chat_id "
            "(например -1001234567890) или ссылку https://t.me/c/2345678901/496."
        )
    chat_id, thread_id = parsed
    return chat_id, thread_id, None


# --- Подключения ---


def connection_audience_text(connection: Connection) -> str:
    audience = _connection_visible_to_roles(connection)
    if audience is None:
        return "все пользователи"
    if audience:
        return ", ".join(audience)
    return "никому (кроме Admin)"


def create_connection_from_form(
    session: Session, actor: User, name: str, url: str, roles_raw: str
) -> tuple[bool, str]:
    return _create_connection_record(
        session,
        actor_telegram_id(actor),
        name.strip(),
        url.strip(),
        _normalize_allowed_roles(roles_raw),
    )


def edit_connection_from_form(
    session: Session, actor: User, connection: Connection, name: str, url: str, roles_raw: str
) -> tuple[bool, str]:
    """Обновить только реально изменившиеся поля (в духе /edit_connection)."""
    changes: dict[str, str] = {}
    name = name.strip()
    if name and name != connection.name:
        changes["name"] = name
    url = url.strip()
    if url and url != connection.database_url:
        changes["url"] = url
    roles = _normalize_allowed_roles(roles_raw)
    if roles != connection.allowed_roles:
        changes["roles"] = roles
    if not changes:
        return True, "Изменений нет."
    return _apply_connection_edits(session, actor_telegram_id(actor), connection.id, changes)


def check_connection_record(
    session: Session, actor: User, connection: Connection
) -> tuple[bool, str]:
    ok, details = _check_database_url(connection.database_url)
    return ok, _format_connection_check_message(connection.name, ok, details)


# --- Триггеры ---


def can_manage_trigger(actor: User, trigger: Trigger) -> bool:
    if actor.role is not None and actor.role.name == "Admin":
        return True
    return trigger.created_by_user_id == actor.id


def create_trigger_from_form(
    session: Session,
    actor: User,
    *,
    name: str,
    connection_ref: str,
    trigger_type: str,
    chat_raw: str,
    schedule_raw: str,
    sql: str,
    message_raw: str,
) -> tuple[bool, str, int | None]:
    if trigger_type not in ("personal", "group"):
        return False, "Неизвестный тип триггера.", None

    chat_id: int | None = None
    thread_id: int | None = None
    if trigger_type == "group":
        chat_id, thread_id, error = parse_chat_id(chat_raw)
        if error:
            return False, error, None

    schedule, error = normalize_schedule_input(schedule_raw or "-")
    if error:
        return False, error, None

    message_template = None if message_raw in ("", "-") else message_raw
    return _create_trigger_record(
        session,
        actor_telegram_id(actor),
        name=name.strip(),
        connection_ref=connection_ref,
        trigger_type=trigger_type,
        chat_id=chat_id,
        schedule=schedule,
        sql_query=sql,
        message_template=message_template,
        message_thread_id=thread_id,
    )


def edit_trigger_from_form(
    session: Session,
    actor: User,
    trigger: Trigger,
    *,
    connection_ref: str,
    trigger_type: str,
    chat_raw: str,
    schedule_raw: str,
    sql: str,
    message_raw: str,
) -> tuple[bool, str]:
    """Собрать изменения формы в формат /edit_trigger и применить их."""
    changes: dict[str, str] = {}

    connection_ref = (connection_ref or "").strip()
    if connection_ref:
        connection = _resolve_connection_by_id_or_name(session, connection_ref)
        if connection is None:
            return False, "Подключение не найдено. Укажите #id или имя из списка.", False
        if connection.id != trigger.connection_id:
            changes["connection"] = connection_ref

    trigger_type = (trigger_type or "personal").strip().lower()
    if trigger_type == "group":
        chat_id, thread_id, error = parse_chat_id(chat_raw)
        if error:
            return False, error, False
        if (
            trigger.trigger_type != "group"
            or trigger.chat_id != chat_id
            or (trigger.message_thread_id or None) != (thread_id or None)
        ):
            changes["group"] = (
                f"group:{chat_id}:{thread_id}" if thread_id else f"group:{chat_id}"
            )
    elif trigger.trigger_type != "personal":
        changes["group"] = "personal"

    schedule_raw = (schedule_raw or "").strip()
    if schedule_raw:
        schedule, error = normalize_schedule_input(schedule_raw)
        if error:
            return False, error, False
        if schedule != trigger.schedule:
            changes["schedule"] = schedule_raw

    sql = (sql or "").strip()
    if sql and sql != trigger.sql_query:
        if "connection" in changes:
            connection = _resolve_connection_by_id_or_name(session, changes["connection"])
        else:
            connection = trigger.connection
        if connection is None:
            return False, "Подключение не найдено.", False
        ok, details, _params = validate_and_execute_row_sql(connection.database_url, sql)
        if not ok:
            return False, f"SQL не прошёл проверку:\n{details}", False
        changes["sql"] = sql

    message_raw = (message_raw or "").strip()
    new_message = "" if message_raw == "-" else message_raw
    if new_message != (trigger.message_template or ""):
        changes["message"] = message_raw or "-"

    if not changes:
        return True, "Изменений нет.", False

    ok, message, _trigger_id = _apply_trigger_edits(
        session, actor_telegram_id(actor), trigger.id, changes
    )
    return ok, message, ok


def set_trigger_active(
    session: Session, actor: User, trigger: Trigger, activate: bool
) -> tuple[bool, str]:
    if not can_manage_trigger(actor, trigger):
        return False, "Управлять триггером может только автор или Admin."
    if activate:
        if trigger.is_active:
            return False, f"Триггер «{trigger.name}» уже активен."
        trigger.is_active = True
        session.commit()
        return True, f"Триггер «{trigger.name}» активирован."
    if not trigger.is_active:
        return False, f"Триггер «{trigger.name}» уже деактивирован."
    trigger.is_active = False
    session.commit()
    return True, f"Триггер «{trigger.name}» деактивирован."


def reschedule_trigger_job(application, trigger_id: int) -> None:
    """Пересчитать задачу расписания; при отсутствии бота (web-only) пропустить."""
    if application is None:
        return
    with application.bot_data["session_factory"]() as session:
        trigger = session.get(Trigger, trigger_id)
        if trigger is None:
            _unschedule_trigger_job(application, trigger_id)
            return
        _schedule_trigger_job(application, trigger.id, trigger.schedule)


@dataclass
class TriggerCheckResult:
    executed: bool = False
    ok: bool = False
    elapsed_ms: int = 0
    rendered: str = ""
    values: str = ""
    error: str = ""
    blocked_reason: str = ""


def check_trigger_record(session: Session, actor: User, trigger: Trigger) -> TriggerCheckResult:
    result = TriggerCheckResult()
    if not trigger.is_active:
        result.blocked_reason = "Триггер деактивирован, проверка не выполнена."
        return result
    if not trigger.connection.is_active:
        result.blocked_reason = "Подключение триггера деактивировано, проверка не выполнена."
        return result

    result.executed = True
    started_at = datetime.now()
    ok, details, params = validate_and_execute_row_sql(
        trigger.connection.database_url, trigger.sql_query
    )
    result.elapsed_ms = round((datetime.now() - started_at).total_seconds() * 1000)
    if ok:
        result.ok = True
        result.rendered = _render_trigger_message(trigger.message_template, params)
        result.values = _format_row_values(params)
    else:
        result.error = details[:3500]
    return result


def next_scheduled_runs(
    session: Session, actor: User, application, limit: int = 10
) -> list[dict[str, object]]:
    """Ближайшие срабатывания видимых триггеров (зеркало «Расписания на сутки»)."""
    rows = _fetch_visible_triggers(session, actor)
    now = datetime.now(timezone.utc)
    events: list[dict[str, object]] = []
    for trigger in rows:
        if not trigger.is_active or not trigger.schedule:
            continue
        try:
            parsed = parse_schedule(trigger.schedule)
        except ValueError:
            continue
        if not parsed:
            continue

        first_run = None
        if application is not None:
            job_queue = application.job_queue
            if job_queue:
                jobs = job_queue.get_jobs_by_name(_trigger_job_name(trigger.id))
                if jobs:
                    first_run = getattr(jobs[0], "next_ttr", None)

        for when in iter_schedule_occurrences(trigger.schedule, now=now, first_run=first_run):
            events.append(
                {
                    "when": when,
                    "trigger_id": trigger.id,
                    "trigger_name": trigger.name,
                    "label": format_schedule_label(parsed.display or trigger.schedule),
                }
            )
    events.sort(key=lambda item: item["when"])
    tz = local_timezone()
    for event in events:
        event["time"] = event.pop("when").astimezone(tz).strftime("%d.%m %H:%M")
    return events[:limit]


# --- Пользователи (admin) ---


def set_user_active_web(
    session: Session, actor: User, target_user_id: int, activate: bool
) -> tuple[bool, str]:
    return _set_user_active_status(
        session, actor_telegram_id(actor), target_user_id, activate
    )


def add_role_web(session: Session, actor: User, target_user_id: int, role_id: int) -> tuple[bool, str]:
    role = session.get(Role, role_id)
    if role is None:
        return False, "Роль не найдена."
    return _set_user_role(session, actor_telegram_id(actor), target_user_id, role)


def remove_role_web(
    session: Session, actor: User, target_user: User, role_name: str
) -> tuple[bool, str]:
    ok, message = _remove_roles_from_user(session, target_user, [role_name])
    if ok:
        session.commit()
    return ok, message


# --- Роли (admin) ---


def rename_role_record(session: Session, actor: User, role_id: int, new_name: str) -> tuple[bool, str]:
    if actor.role is None or actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."
    role = session.get(Role, role_id)
    if role is None:
        return False, "Роль не найдена."
    new_name = new_name.strip()
    if not new_name:
        return False, "Название роли не должно быть пустым."
    existing = session.scalar(select(Role).where(Role.name == new_name))
    if existing and existing.id != role.id:
        return False, f"Роль '{new_name}' уже существует."

    old_name = role.name
    role.name = new_name
    # Переименовываем упоминания роли в видимости подключений (как в боте).
    for connection in session.scalars(select(Connection)).all():
        roles = _parse_allowed_roles(connection.allowed_roles)
        if old_name in roles:
            connection.allowed_roles = ", ".join(
                new_name if r == old_name else r for r in roles
            )
    session.commit()
    return True, f"Роль '{old_name}' переименована в '{new_name}'."


def update_role_description_record(
    session: Session, actor: User, role_id: int, description: str
) -> tuple[bool, str]:
    if actor.role is None or actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."
    role = session.get(Role, role_id)
    if role is None:
        return False, "Роль не найдена."
    old_description = role.description or "-"
    role.description = (description or "").strip()
    session.commit()
    return True, (
        f"Описание роли «{role.name}» обновлено:\n"
        f"{old_description}  →  {role.description or '-'}"
    )


def delete_role_record(session: Session, actor: User, role_id: int) -> tuple[bool, str]:
    if actor.role is None or actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."
    role = session.get(Role, role_id)
    if role is None:
        return False, "Роль не найдена."
    if role.name in PROTECTED_ROLE_NAMES:
        return False, f"Роль '{role.name}' защищена от удаления."

    default_role = session.scalar(select(Role).where(Role.name == "DefaultUser"))
    if default_role is None:
        return False, "Базовая роль DefaultUser не найдена."

    old_name = role.name
    moved_users = 0
    for user_role in session.scalars(select(UserRole).where(UserRole.role_id == role.id)).all():
        user = session.get(User, user_role.user_id)
        if user is None:
            continue
        user.roles = [r for r in user.roles if r.id != role.id]
        if not any(r.id == default_role.id for r in user.roles):
            user.roles.append(default_role)
        moved_users += 1
    for connection in session.scalars(select(Connection)).all():
        roles = _parse_allowed_roles(connection.allowed_roles)
        if old_name in roles:
            connection.allowed_roles = ", ".join(r for r in roles if r != old_name)
    session.delete(role)
    session.commit()
    return True, (
        f"Роль '{old_name}' удалена. Пользователей переведено в DefaultUser: {moved_users}."
    )


# --- Dashboard ---


def dashboard_stats(session: Session, actor: User) -> dict[str, object]:
    connections = _fetch_visible_connections(session, actor)
    triggers = _fetch_visible_triggers(session, actor)
    stats: dict[str, object] = {
        "conn_count": len(connections),
        "trigger_count": len(triggers),
        "scheduled_count": sum(1 for t in triggers if t.is_active and t.schedule),
        "users_count": None,
    }
    if actor.role is not None and actor.role.name == "Admin":
        stats["users_count"] = session.scalar(select(func.count()).select_from(User)) or 0
    return stats
