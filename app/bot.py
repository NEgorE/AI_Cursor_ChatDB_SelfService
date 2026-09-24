from __future__ import annotations

import html
import re
import ssl
import warnings

from datetime import datetime, timezone

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
import certifi
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.config import AppConfig, CommandConfig, WebConfig, load_config
from app.connection_display import parse_database_url
from app.db import build_engine, build_session_factory
from app.init_db import _get_or_create_role
from app.models import Base, Connection, Role, Trigger, User, UserRole
from app.web.auth import issue_login_code
from app.trigger_service import (
    daily_input_to_utc,
    format_schedule_label,
    iter_schedule_occurrences,
    local_timezone,
    normalize_schedule,
    parse_chat_ref,
    parse_schedule,
    validate_and_execute_row_sql,
    validate_and_execute_scalar_sql,
)
from sqlalchemy import create_engine, func, or_, select, text
from sqlalchemy.orm import joinedload, selectinload
from telegram.warnings import PTBUserWarning

# Диалог /edit_trigger сочетает ввод текстом и inline-кнопки,
# поэтому per_message=False обязателен — глушим штатное предупреждение PTB.
warnings.filterwarnings(
    "ignore",
    message=".*If 'per_message=False', 'CallbackQueryHandler' will not be tracked.*",
    category=PTBUserWarning,
)

USERS_PER_PAGE = 5
CONNECTIONS_PER_PAGE = 5
GROUPS_PER_PAGE = 5
TRIGGERS_PER_PAGE = 5
BLOCKED_USER_TEXT = "Пользователь заблокирован, обратитесь к администратору."
PROTECTED_ROLE_NAMES = frozenset({"Admin", "DefaultUser"})
TRIGGER_JOB_PREFIX = "trigger:"


async def block_inactive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if not tg_user:
        return

    is_bot_command = bool(
        update.message and update.message.text and update.message.text.startswith("/")
    )
    if not is_bot_command and not update.callback_query:
        return

    session_factory = context.application.bot_data.get("session_factory")
    if not session_factory:
        return

    with session_factory() as session:
        user = _get_bound_user(session, tg_user.id)

    if not user or user.is_active:
        return

    if update.callback_query:
        await update.callback_query.answer(BLOCKED_USER_TEXT, show_alert=True)
    elif update.message:
        await update.message.reply_text(BLOCKED_USER_TEXT)
    raise ApplicationHandlerStop()


async def on_startup(application: Application) -> None:
    base_commands = _build_bot_commands(application.bot_data["command_configs"], include_admin=False)
    await application.bot.set_my_commands(base_commands)
    _reschedule_all_triggers(application)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    user_payload: dict[str, str] | None = None

    with session_factory() as session:
        user = session.scalar(
            select(User).where(User.telegram_user_id == tg_user.id)
        )
        if not user:
            candidate = _find_candidate_user(session, tg_user.full_name, tg_user.username)
            if candidate:
                candidate.telegram_user_id = tg_user.id
                candidate.telegram_username = tg_user.username
                session.commit()
                user = session.scalar(
                    select(User)
                    
                    .where(User.telegram_user_id == tg_user.id)
                )
            else:
                default_role = _get_or_create_role(session, "DefaultUser")
                new_user = User(
                    full_name=tg_user.full_name,
                    work_email=None,
                    telegram_user_id=tg_user.id,
                    telegram_username=tg_user.username,
                    roles=[default_role],
                    is_active=True,
                )
                session.add(new_user)
                session.commit()
                user = session.scalar(
                    select(User)
                    
                    .where(User.telegram_user_id == tg_user.id)
                )

        if user:
            user_payload = {
                "full_name": user.full_name,
                "role_name": user.role.name,
            }
            if update.effective_chat:
                try:
                    await _sync_user_menu_commands(
                        context.application, update.effective_chat.id, tg_user.id, user.role.name
                    )
                except TelegramError:
                    # Menu personalization failure must not block /start flow.
                    pass

    if not user_payload:
        await update.message.reply_text(
            "Вы не зарегистрированы в MVP-базе. Обратитесь к администратору."
        )
        return

    await update.message.reply_text(
        f"Привет, {user_payload['full_name']}! Ваша роль: {user_payload['role_name']}."
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("pong")


async def weblogin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выдать одноразовый код входа в веб-интерфейс.

    Пользователь, ещё не привязанный к БД, регистрируется так же, как в /start.
    """
    if not update.effective_user or not update.message:
        return

    web_config: WebConfig | None = context.application.bot_data.get("web_config")
    if web_config is not None and not web_config.enabled:
        await update.message.reply_text(
            "Веб-интерфейс отключён в конфигурации (web.enabled: false)."
        )
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    with session_factory() as session:
        user = session.scalar(
            select(User).where(User.telegram_user_id == tg_user.id)
        )
        if not user:
            candidate = _find_candidate_user(session, tg_user.full_name, tg_user.username)
            if candidate:
                candidate.telegram_user_id = tg_user.id
                candidate.telegram_username = tg_user.username
                session.commit()
                user = candidate
            else:
                default_role = _get_or_create_role(session, "DefaultUser")
                new_user = User(
                    full_name=tg_user.full_name,
                    work_email=None,
                    telegram_user_id=tg_user.id,
                    telegram_username=tg_user.username,
                    roles=[default_role],
                    is_active=True,
                )
                session.add(new_user)
                session.commit()
                user = session.scalar(
                    select(User).where(User.telegram_user_id == tg_user.id)
                )
        if not user:
            await update.message.reply_text(
                "Не удалось создать пользователя. Обратитесь к администратору."
            )
            return
        code = issue_login_code(session, user)
        url = web_config.display_url() if web_config is not None else "http://127.0.0.1:8000"

    await update.message.reply_text(
        f"Код для входа в веб-интерфейс: {code}\n"
        "Код одноразовый, действует 5 минут.\n"
        f"Открой {url} и введи код."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    with session_factory() as session:
        actor = _get_bound_user(session, tg_user.id) if tg_user else None
        is_admin = bool(actor and actor.role and actor.role.name == "Admin")

    commands = _build_bot_commands(
        context.application.bot_data["command_configs"],
        include_admin=is_admin,
    )
    lines = ["Доступные команды:"]
    for command in commands:
        lines.append(f"/{command.command} - {command.description}")
    await update.message.reply_text("\n".join(lines))


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    with session_factory() as session:
        user = session.scalar(
            select(User)
            
            .where(User.telegram_user_id == tg_user.id)
        )
        if not user:
            await update.message.reply_text("Пользователь не найден в БД.")
            return

        username_text = f"@{user.telegram_username}" if user.telegram_username else "-"

    roles_line = ", ".join(r.name for r in user.roles) if user.roles else "-"
    await update.message.reply_text(
        f"id={user.id}, full_name={user.full_name}, username={username_text}, roles={roles_line}, "
        f"active={user.is_active}"
    )


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    with session_factory() as session:
        actor = _get_bound_user(session, tg_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return
        if actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return

        user_rows = _fetch_all_users(session)
        if not user_rows:
            await update.message.reply_text(
                "Пользователей в БД пока нет.\n\n"
                "Активировать: /activate_user\n"
                "Деактивировать: /deactivate_user\n"
                "Присвоить роль: /add_role\n"
                "Забрать роль: /remove_role"
            )
            return

        text, keyboard = _build_users_page_view(user_rows, page=0, role_name=actor.role.name)
        if keyboard:
            await update.message.reply_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text)


# --- Пошаговые /activate_user и /deactivate_user ---

US_WAIT_REF, US_CONFIRM = range(70, 72)


def _user_status_wanted_state(user: User, activate: bool) -> str:
    if activate:
        return "активировать" if not user.is_active else "уже активен"
    return "деактивировать" if user.is_active else "уже деактивирован"


async def _user_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE, activate: bool) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    user_id = _extract_user_id_from_command(context.args, update.message.text, activate)
    if user_id is not None:
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            ok, message = _set_user_active_status(
                session,
                actor_telegram_id=update.effective_user.id,
                target_user_id=user_id,
                activate=activate,
            )
        await update.message.reply_text(message)
        return ConversationHandler.END

    verb = "активировать" if activate else "деактивировать"
    await update.message.reply_text(
        f"Какого пользователя {verb}?\n\n"
        "Пришлите #id из /users или telegram id (строка «id: …» в карточке).\n"
        "Отмена: /cancel"
    )
    return US_WAIT_REF


async def user_status_receive_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return US_WAIT_REF
    ref = update.message.text.strip().lstrip("#")
    if not ref.isdigit():
        await update.message.reply_text(
            "Нужен числовой #id из /users или telegram id. Попробуйте ещё раз или /cancel"
        )
        return US_WAIT_REF

    activate = context.user_data.get("user_status_activate", True)
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        target_user = _resolve_user_by_id_or_telegram(session, int(ref))
        if not target_user:
            await update.message.reply_text(
                "Пользователь не найден. Попробуйте ещё раз или /cancel"
            )
            return US_WAIT_REF
        if _user_status_wanted_state(target_user, activate).startswith("уже"):
            state_text = _user_status_wanted_state(target_user, activate)
            await update.message.reply_text(
                f"Пользователь #{target_user.id} ({target_user.full_name}): {state_text}."
            )
            return ConversationHandler.END
        context.user_data["user_status_target_id"] = target_user.id
        card = _format_user_card(target_user)

    verb = "Активировать" if activate else "Деактивировать"
    callback_prefix = "uact" if activate else "udeact"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ {verb}", callback_data=f"{callback_prefix}yes"),
                InlineKeyboardButton("Отмена", callback_data=f"{callback_prefix}cancel"),
            ]
        ]
    )
    await update.message.reply_text(f"{verb} этого пользователя?\n\n{card}", reply_markup=keyboard)
    return US_CONFIRM


async def _user_status_start_activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["user_status_activate"] = True
    return await _user_status_start(update, context, True)


async def _user_status_start_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["user_status_activate"] = False
    return await _user_status_start(update, context, False)


async def user_status_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return US_CONFIRM
    await query.answer()

    activate = context.user_data.get("user_status_activate", True)
    yes_data = "uactyes" if activate else "udeactyes"
    if query.data != yes_data:
        context.user_data.pop("user_status_target_id", None)
        context.user_data.pop("user_status_activate", None)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    target_id = context.user_data.pop("user_status_target_id", None)
    context.user_data.pop("user_status_activate", None)
    if target_id is None:
        await query.edit_message_text("Сессия утеряна. Начните заново.")
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        ok, message = _set_user_active_status(
            session,
            actor_telegram_id=update.effective_user.id,
            target_user_id=target_id,
            activate=activate,
        )
    await query.edit_message_text(message)
    return ConversationHandler.END


async def user_status_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("user_status_target_id", "user_status_activate"):
        context.user_data.pop(key, None)
    if update.message:
        await update.message.reply_text("Отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Отменено.")
    return ConversationHandler.END


def user_status_conversations() -> list[ConversationHandler]:
    status_filter = CallbackQueryHandler(
        user_status_confirm, pattern=r"^uactyes$|^udeactyes$|^uactcancel$|^udeactcancel$"
    )
    return [
        ConversationHandler(
            entry_points=[CommandHandler("activate_user", _user_status_start_activate)],
            states={
                US_WAIT_REF: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, user_status_receive_ref)
                ],
                US_CONFIRM: [status_filter],
            },
            fallbacks=[CommandHandler("cancel", user_status_cancel)],
            allow_reentry=True,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("deactivate_user", _user_status_start_deactivate)],
            states={
                US_WAIT_REF: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, user_status_receive_ref)
                ],
                US_CONFIRM: [status_filter],
            },
            fallbacks=[CommandHandler("cancel", user_status_cancel)],
            allow_reentry=True,
        ),
    ]


async def users_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not query.message:
        return

    if not query.data.startswith("upage:"):
        await query.answer()
        return

    page_raw = query.data.removeprefix("upage:")
    if not page_raw.isdigit():
        await query.answer()
        return

    page = int(page_raw)
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor or actor.role.name != "Admin":
            await query.answer("Недостаточно прав.", show_alert=True)
            return
        user_rows = _fetch_all_users(session)
        text, keyboard = _build_users_page_view(user_rows, page=page, role_name=actor.role.name)

    if keyboard:
        await query.edit_message_text(text, reply_markup=keyboard)
    else:
        await query.edit_message_text(text)
    await query.answer()


ROLES_ALL_MARKER = "*"


def _normalize_allowed_roles(raw: str) -> str:
    """Пусто — наследует роль создателя; «-» — видят все; иначе список ролей."""
    raw = raw.strip()
    if raw == "-":
        return ROLES_ALL_MARKER
    return ", ".join(role.strip() for role in raw.split(",") if role.strip())


def _create_connection_record(
    session, actor_telegram_id: int, name: str, database_url: str, allowed_roles: str
) -> tuple[bool, str]:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return False, "Пользователь не найден в БД."
    if actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."
    if session.scalar(select(Connection).where(Connection.name == name)):
        return False, f"Подключение с именем '{name}' уже существует."
    connection = Connection(
        name=name,
        database_url=database_url,
        created_by_user_id=actor.id,
        allowed_roles=allowed_roles,
        is_active=True,
    )
    session.add(connection)
    session.commit()
    if allowed_roles == ROLES_ALL_MARKER:
        roles_text = "все пользователи"
    else:
        roles_text = allowed_roles or "роль создателя"
    return True, f"Подключение '{name}' создано. Видимость: {roles_text}."


# --- Пошаговые команды подключений ---

CC_NAME, CC_URL, CC_ROLES, CC_CONFIRM = range(80, 84)
CX_WAIT_REF, CX_CONFIRM = range(84, 86)


async def _connections_wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("create_connection", "conn_status_activate", "conn_status_id"):
        context.user_data.pop(key, None)
    if update.message:
        await update.message.reply_text("Отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Отменено.")
    return ConversationHandler.END


def _create_connection_summary(state: dict) -> str:
    raw_roles = state.get("allowed_roles") or ""
    roles_text = "все пользователи" if raw_roles == ROLES_ALL_MARKER else (raw_roles or "не задано (роль создателя)")
    return (
        f"Имя: {state['name']}\n"
        f"URL: {state['database_url']}\n"
        f"Видимость (роли): {roles_text}"
    )


async def create_connection_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    if len(context.args) >= 3:
        connection_name = context.args[0].strip()
        database_url = context.args[1].strip()
        allowed_roles = _normalize_allowed_roles(context.args[2].strip())
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            ok, message = _create_connection_record(
                session, update.effective_user.id, connection_name, database_url, allowed_roles
            )
        await update.message.reply_text(message)
        return ConversationHandler.END

    if not update.effective_user:
        return ConversationHandler.END
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor or actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return ConversationHandler.END

    context.user_data["create_connection"] = {}
    await update.message.reply_text(
        "Создание подключения, шаг 1 из 3 — имя.\n\n"
        "Пришлите уникальное имя подключения.\nОтмена: /cancel"
    )
    return CC_NAME


async def create_connection_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_connection")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Имя не должно быть пустым. Попробуйте ещё раз или /cancel")
        return CC_NAME

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        if session.scalar(select(Connection).where(Connection.name == name)):
            await update.message.reply_text(
                f"Подключение с именем '{name}' уже существует. Пришлите другое имя или /cancel"
            )
            return CC_NAME
    state["name"] = name
    await update.message.reply_text(
        f"Шаг 2 из 3 — адрес базы (для «{name}»).\n\n"
        "Пришлите database_url, например:\n"
        "postgresql://user:password@host:5432/dbname\n"
        "sqlite:///data/mydb.sqlite3\n\n"
        "URL будет проверен подключением к базе.\nОтмена: /cancel"
    )
    return CC_URL


async def create_connection_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_connection")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    database_url = update.message.text.strip()
    if not database_url:
        await update.message.reply_text("URL не должен быть пустым. Попробуйте ещё раз или /cancel")
        return CC_URL

    ok, details = _check_database_url(database_url)
    if not ok:
        await update.message.reply_text(
            "Проверка подключения не прошла:\n"
            f"{details[:1500]}\n\n"
            "Пришлите исправленный URL или `-`, чтобы сохранить как есть."
        )
        return CC_URL

    state["database_url"] = database_url
    await update.message.reply_text(
        "Шаг 3 из 3 — видимость (роли).\n\n"
        "Роли через запятую, которым видно подключение (например: Admin, Analyst).\n"
        "«-» — видят все пользователи.\nОтмена: /cancel"
    )
    return CC_ROLES


async def create_connection_receive_roles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_connection")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    roles_raw = update.message.text.strip()
    if not roles_raw:
        await update.message.reply_text("Укажите роли или «-». Попробуйте ещё раз или /cancel")
        return CC_ROLES
    state["allowed_roles"] = _normalize_allowed_roles(roles_raw)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💾 Создать", callback_data="ccsave"),
                InlineKeyboardButton("Отмена", callback_data="cccancel"),
            ]
        ]
    )
    await update.message.reply_text(
        f"Создаём подключение?\n\n{_create_connection_summary(state)}",
        reply_markup=keyboard,
    )
    return CC_CONFIRM


async def create_connection_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.pop("create_connection", None)
    query = update.callback_query
    if state is None or not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    if query.data != "ccsave":
        await query.edit_message_text("Создание подключения отменено.")
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        ok, message = _create_connection_record(
            session,
            update.effective_user.id,
            state["name"],
            state["database_url"],
            state.get("allowed_roles", ""),
        )
    await query.edit_message_text(message)
    return ConversationHandler.END


# --- /check_connection: пошагово ---


async def check_connection_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    if len(context.args) >= 1:
        connection_ref = " ".join(context.args).strip()
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            message = _run_connection_check(session, update.effective_user.id, connection_ref)
        await update.message.reply_text(message)
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor or actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return ConversationHandler.END

    await update.message.reply_text(
        "Какое подключение проверить?\n\n"
        "Пришлите #id или имя из /connections.\nОтмена: /cancel"
    )
    return CX_WAIT_REF


# --- /edit_connection: пошаговое изменение подключения ---

EC_WAIT_REF, EC_FIELD_SELECT, EC_WAIT_VALUE = range(86, 89)

EDIT_CONNECTION_FIELD_LABELS = {
    "name": "Название",
    "url": "Адрес базы (URL)",
    "roles": "Видимость (роли)",
}


def _load_editable_connection(session, actor_telegram_id: int, ref: str) -> Connection | None:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor or actor.role.name != "Admin":
        return None
    connection = _resolve_connection_by_id_or_name(session, ref.lstrip("#"))
    if not connection:
        return None
    return connection


def _edit_connection_current_values(connection: Connection) -> dict[str, str]:
    roles = _parse_allowed_roles(connection.allowed_roles)
    if ROLES_ALL_MARKER in roles:
        roles_text = "- (все пользователи)"
    elif roles:
        roles_text = ", ".join(roles)
    else:
        roles_text = "не задано (роль создателя)"
    return {
        "name": connection.name,
        "url": connection.database_url,
        "roles": roles_text,
    }


def _edit_connection_menu_text(connection: Connection, changes: dict[str, str]) -> str:
    current = _edit_connection_current_values(connection)
    lines = [
        f"Изменение подключения #{connection.id} «{connection.name}»",
        "",
        "Выберите поле кнопкой, введите новое значение — и так для каждого поля.",
        "Когда всё готово, нажмите «Сохранить».",
        "",
    ]
    for field, label in EDIT_CONNECTION_FIELD_LABELS.items():
        line = f"• {label}: {current[field]}"
        if field in changes:
            line += f"  →  {changes[field]}"
        lines.append(line)
    lines.append("")
    lines.append("Изменения сохраняются только после нажатия «Сохранить».")
    return "\n".join(lines)


def _edit_connection_menu_keyboard(changes: dict[str, str]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for field, label in EDIT_CONNECTION_FIELD_LABELS.items():
        mark = " ✓" if field in changes else ""
        buttons.append(
            [InlineKeyboardButton(f"Изменить: {label}{mark}", callback_data=f"econn:field:{field}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("💾 Сохранить", callback_data="econn:save"),
            InlineKeyboardButton("Отмена", callback_data="econn:cancel"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def edit_connection_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    ref = " ".join(context.args).strip() if context.args else ""
    if ref:
        return await _edit_connection_begin(update, context, ref)

    await update.message.reply_text(
        "Какое подключение изменить? Пришлите #id или имя из /connections.\nОтмена: /cancel"
    )
    return EC_WAIT_REF


async def edit_connection_receive_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EC_WAIT_REF
    return await _edit_connection_begin(update, context, update.message.text.strip())


async def _edit_connection_begin(update: Update, context: ContextTypes.DEFAULT_TYPE, ref: str) -> int:
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        connection = _load_editable_connection(session, update.effective_user.id, ref)
        if not connection:
            await update.message.reply_text(
                "Подключение не найдено или нет прав. Команда доступна только Admin."
            )
            return ConversationHandler.END
        context.user_data["edit_connection"] = {"connection_id": connection.id, "changes": {}}
        text = _edit_connection_menu_text(connection, {})
    await update.message.reply_text(text, reply_markup=_edit_connection_menu_keyboard({}))
    return EC_FIELD_SELECT


def _edit_connection_value_prompt(connection: Connection, field: str) -> str:
    current = _edit_connection_current_values(connection)
    hints = {
        "name": "уникальное имя подключения",
        "url": "postgresql://user:password@host:5432/dbname — будет проверен; `-` оставить как есть",
        "roles": "роли через запятую (Admin, Analyst); `-` — видят все",
    }
    return (
        f"Текущее значение ({EDIT_CONNECTION_FIELD_LABELS[field]}):\n"
        f"{current[field]}\n\n"
        f"Пришлите новое значение.\n{hints[field]}\n"
        f"Отмена: /cancel"
    )


async def edit_connection_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return EC_FIELD_SELECT

    if query.data == "econn:cancel":
        await query.answer()
        context.user_data.pop("edit_connection", None)
        await query.edit_message_text("Изменение подключения отменено.")
        return ConversationHandler.END

    state = context.user_data.get("edit_connection")
    if state is None:
        await query.answer()
        await query.edit_message_text("Сессия утеряна. Начните заново: /edit_connection")
        return ConversationHandler.END

    if query.data == "econn:save":
        changes = state.get("changes", {})
        if not changes:
            await query.answer("Нет изменений.", show_alert=True)
            return EC_FIELD_SELECT
        await query.answer()
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            ok, message = _apply_connection_edits(
                session, update.effective_user.id, state["connection_id"], changes
            )
        context.user_data.pop("edit_connection", None)
        await query.edit_message_text(message)
        return ConversationHandler.END

    await query.answer()
    field = query.data.removeprefix("econn:field:")
    if field not in EDIT_CONNECTION_FIELD_LABELS:
        return EC_FIELD_SELECT
    state["field"] = field
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        connection = session.get(Connection, state["connection_id"])
    if not connection:
        await query.edit_message_text("Подключение не найдено. Начните заново: /edit_connection")
        return ConversationHandler.END
    await query.edit_message_text(_edit_connection_value_prompt(connection, field))
    return EC_WAIT_VALUE


async def edit_connection_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("edit_connection")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    field = state.get("field")
    if not field:
        return ConversationHandler.END

    value = update.message.text.strip()
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        connection = session.get(Connection, state["connection_id"])
        if not connection:
            await update.message.reply_text("Подключение не найдено. Начните заново: /edit_connection")
            return ConversationHandler.END

        if field == "name":
            if not value:
                await update.message.reply_text("Имя не должно быть пустым. Попробуйте ещё раз или /cancel")
                return EC_WAIT_VALUE
            existing = session.scalar(select(Connection).where(Connection.name == value))
            if existing and existing.id != connection.id:
                await update.message.reply_text(
                    f"Подключение с именем '{value}' уже существует. Попробуйте ещё раз или /cancel"
                )
                return EC_WAIT_VALUE

        if field == "url":
            if value == "-":
                state["changes"].pop("url", None)
            else:
                ok, details = _check_database_url(value)
                if not ok:
                    await update.message.reply_text(
                        "Проверка подключения не прошла:\n"
                        f"{details[:1500]}\n\n"
                        "Пришлите исправленный URL или `-`, чтобы оставить прежний."
                    )
                    return EC_WAIT_VALUE
                state["changes"]["url"] = value

        if field == "roles":
            if not value:
                await update.message.reply_text("Укажите роли или «-». Попробуйте ещё раз или /cancel")
                return EC_WAIT_VALUE
            state["changes"]["roles"] = _normalize_allowed_roles(value)

        if field == "name" and value:
            state["changes"]["name"] = value

        changes = state.get("changes", {})
        text = _edit_connection_menu_text(connection, changes)
    await update.message.reply_text("Принято.\n\n" + text, reply_markup=_edit_connection_menu_keyboard(changes))
    state["field"] = None
    return EC_FIELD_SELECT


def _apply_connection_edits(
    session, actor_telegram_id: int, connection_id: int, changes: dict[str, str]
) -> tuple[bool, str]:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor or actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."
    connection = session.get(Connection, connection_id)
    if not connection:
        return False, "Подключение не найдено."

    if "name" in changes:
        existing = session.scalar(select(Connection).where(Connection.name == changes["name"]))
        if existing and existing.id != connection.id:
            return False, f"Подключение с именем '{changes['name']}' уже существует, изменения не сохранены."
        connection.name = changes["name"]
    if "url" in changes:
        connection.database_url = changes["url"]
    if "roles" in changes:
        connection.allowed_roles = changes["roles"]
    session.commit()
    return (
        True,
        f"Подключение «{connection.name}» (#{connection.id}) изменёно.\n"
        f"Изменённые поля: {', '.join(EDIT_CONNECTION_FIELD_LABELS[f] for f in changes)}.",
    )


async def edit_connection_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit_connection", None)
    if update.message:
        await update.message.reply_text("Изменение подключения отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Изменение подключения отменено.")
    return ConversationHandler.END


def edit_connection_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("edit_connection", edit_connection_start)],
        states={
            EC_WAIT_REF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_connection_receive_ref)
            ],
            EC_FIELD_SELECT: [
                CallbackQueryHandler(edit_connection_field_chosen, pattern=r"^econn:")
            ],
            EC_WAIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_connection_receive_value)
            ],
        },
        fallbacks=[CommandHandler("cancel", edit_connection_cancel)],
        allow_reentry=True,
    )


def _run_connection_check(session, actor_telegram_id: int, connection_ref: str) -> str:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return "Пользователь не найден в БД."
    if actor.role.name != "Admin":
        return "Недостаточно прав. Команда доступна только Admin."
    connection = _resolve_connection_by_id_or_name(session, connection_ref.lstrip("#"))
    if not connection:
        return "Подключение не найдено. Укажите #id или имя из /connections."
    ok, details = _check_database_url(connection.database_url)
    return _format_connection_check_message(connection.name, ok, details)


async def check_connection_receive_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return CX_WAIT_REF
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        message = _run_connection_check(
            session, update.effective_user.id, update.message.text.strip()
        )
    await update.message.reply_text(message)
    return ConversationHandler.END


# --- /activate_connection и /deactivate_connection: пошагово ---


async def _connection_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE, activate: bool) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    if len(context.args) >= 1:
        connection_ref = " ".join(context.args).strip()
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            ok, message = _set_connection_active_status(
                session,
                actor_telegram_id=update.effective_user.id,
                connection_ref=connection_ref,
                activate=activate,
            )
        await update.message.reply_text(message)
        return ConversationHandler.END

    context.user_data["conn_status_activate"] = activate
    verb = "активировать" if activate else "деактивировать"
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor or actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return ConversationHandler.END

    await update.message.reply_text(
        f"Какое подключение {verb}?\n\n"
        "Пришлите #id или имя из /connections.\nОтмена: /cancel"
    )
    return CX_WAIT_REF


async def connection_status_receive_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return CX_WAIT_REF
    activate = context.user_data.get("conn_status_activate", True)
    ref = update.message.text.strip()

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        connection = _resolve_connection_by_id_or_name(session, ref.lstrip("#"))
        if not connection:
            await update.message.reply_text(
                "Подключение не найдено. Попробуйте ещё раз или /cancel"
            )
            return CX_WAIT_REF
        state_text = (
            ("активировано" if connection.is_active else "деактивировано")
        )
        if activate and connection.is_active:
            await update.message.reply_text(
                f"Подключение #{connection.id} ({connection.name}): уже активировано."
            )
            context.user_data.pop("conn_status_activate", None)
            return ConversationHandler.END
        if not activate and not connection.is_active:
            await update.message.reply_text(
                f"Подключение #{connection.id} ({connection.name}): уже деактивировано."
            )
            context.user_data.pop("conn_status_activate", None)
            return ConversationHandler.END

        context.user_data["conn_status_id"] = connection.id
        card = _format_connection_card(connection)

    verb = "Активировать" if activate else "Деактивировать"
    callback_prefix = "cstatyes" if activate else "cstatno"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ {verb}", callback_data=callback_prefix),
                InlineKeyboardButton("Отмена", callback_data="cstatcancel"),
            ]
        ]
    )
    await update.message.reply_text(
        f"{verb} это подключение? (сейчас: {state_text})\n\n{card}",
        reply_markup=keyboard,
    )
    return CX_CONFIRM


async def connection_status_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return CX_CONFIRM
    await query.answer()

    activate = context.user_data.get("conn_status_activate", True)
    yes_data = "cstatyes" if activate else "cstatno"
    if query.data != yes_data:
        context.user_data.pop("conn_status_id", None)
        context.user_data.pop("conn_status_activate", None)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    connection_id = context.user_data.pop("conn_status_id", None)
    context.user_data.pop("conn_status_activate", None)
    if connection_id is None:
        await query.edit_message_text("Сессия утеряна. Начните заново.")
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        connection = session.get(Connection, connection_id)
        if not connection:
            await query.edit_message_text("Подключение не найдено.")
            return ConversationHandler.END
        ok, message = _set_connection_active_status(
            session,
            actor_telegram_id=update.effective_user.id,
            connection_ref=str(connection.id),
            activate=activate,
        )
    await query.edit_message_text(message)
    return ConversationHandler.END


async def _connection_status_start_activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _connection_status_start(update, context, True)


async def _connection_status_start_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _connection_status_start(update, context, False)


def connections_conversations() -> list[ConversationHandler]:
    return [
        ConversationHandler(
            entry_points=[CommandHandler("create_connection", create_connection_start)],
            states={
                CC_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_connection_receive_name)
                ],
                CC_URL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_connection_receive_url)
                ],
                CC_ROLES: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_connection_receive_roles)
                ],
                CC_CONFIRM: [
                    CallbackQueryHandler(create_connection_confirm, pattern=r"^ccsave$|^cccancel$")
                ],
            },
            fallbacks=[CommandHandler("cancel", _connections_wizard_cancel)],
            allow_reentry=True,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("check_connection", check_connection_start)],
            states={
                CX_WAIT_REF: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, check_connection_receive_ref),
                ],
            },
            fallbacks=[CommandHandler("cancel", _connections_wizard_cancel)],
            allow_reentry=True,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("activate_connection", _connection_status_start_activate)],
            states={
                CX_WAIT_REF: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, connection_status_receive_ref),
                ],
                CX_CONFIRM: [
                    CallbackQueryHandler(
                        connection_status_confirm,
                        pattern=r"^cstatyes$|^cstatno$|^cstatcancel$",
                    )
                ],
            },
            fallbacks=[CommandHandler("cancel", _connections_wizard_cancel)],
            allow_reentry=True,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("deactivate_connection", _connection_status_start_deactivate)],
            states={
                CX_WAIT_REF: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, connection_status_receive_ref),
                ],
                CX_CONFIRM: [
                    CallbackQueryHandler(
                        connection_status_confirm,
                        pattern=r"^cstatyes$|^cstatno$|^cstatcancel$",
                    )
                ],
            },
            fallbacks=[CommandHandler("cancel", _connections_wizard_cancel)],
            allow_reentry=True,
        ),
    ]


def _user_role_names(user: User) -> set[str]:
    return {role.name for role in user.roles}


def _connection_visible_to_roles(connection: Connection) -> list[str] | None:
    """Эффективная аудитория подключения.

    None — видят все; [] — только Admin; иначе список ролей.
    Пустое поле наследует роль создателя, «*» (ввод «-») — все пользователи.
    """
    allowed = [role for role in _parse_allowed_roles(connection.allowed_roles) if role]
    if ROLES_ALL_MARKER in allowed:
        return None
    if allowed:
        return allowed
    creator_role = connection.created_by.role.name if connection.created_by else None
    return [creator_role] if creator_role else []


def _fetch_visible_connections(session, actor: User) -> list[Connection]:
    rows = session.scalars(
        select(Connection)
        .options(joinedload(Connection.created_by))
        .order_by(Connection.name)
    ).all()
    if actor.role.name == "Admin":
        return list(rows)
    actor_roles = _user_role_names(actor)
    visible: list[Connection] = []
    for connection in rows:
        if not connection.is_active:
            continue
        audience = _connection_visible_to_roles(connection)
        if audience is None or actor_roles & set(audience):
            visible.append(connection)
    return visible


async def connections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    with session_factory() as session:
        actor = _get_bound_user(session, tg_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return

        connection_rows = _fetch_visible_connections(session, actor)
        if not connection_rows:
            await update.message.reply_text(
                f"Доступных подключений пока нет (роль: {actor.role.name}).\n\n"
                + (
                    "Создать подключение: /create_connection\n"
                    "Активировать: /activate_connection\n"
                    "Проверить: /check_connection\n"
                    "Изменить: /edit_connection\n"
                    "Деактивировать: /deactivate_connection"
                    if actor.role.name == "Admin"
                    else f"Подключения, доступные роли {actor.role.name}, появятся здесь."
                )
            )
            return

        text, keyboard = _build_connections_page_view(connection_rows, page=0, role_name=actor.role.name)
        if keyboard:
            await update.message.reply_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text)


async def connection_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not query.message:
        return

    session_factory = context.application.bot_data["session_factory"]

    if query.data.startswith("cpage:"):
        page_raw = query.data.removeprefix("cpage:")
        if not page_raw.isdigit():
            await query.answer()
            return
        page = int(page_raw)
        with session_factory() as session:
            actor = _get_bound_user(session, update.effective_user.id)
            if not actor:
                await query.answer("Пользователь не найден.", show_alert=True)
                return
            connection_rows = _fetch_visible_connections(session, actor)
            text, keyboard = _build_connections_page_view(connection_rows, page=page, role_name=actor.role.name)
        if keyboard:
            await query.edit_message_text(text, reply_markup=keyboard)
        else:
            await query.edit_message_text(text)
        await query.answer()
        return

    await query.answer()


def _find_candidate_user(session, tg_full_name: str, tg_username: str | None) -> User | None:
    filters = [User.full_name == tg_full_name]
    if tg_username:
        username_no_at = tg_username.strip().lstrip("@")
        if username_no_at:
            filters.extend(
                [
                    User.telegram_username == username_no_at,
                    User.full_name == username_no_at,
                    User.full_name == f"@{username_no_at}",
                ]
            )

    return session.scalar(select(User).where(or_(*filters)))


def _get_bound_user(session, telegram_user_id: int) -> User | None:
    return session.scalar(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )


def _resolve_user_by_id_or_telegram(session, user_ref: int) -> User | None:
    """Find user by internal #id from /users or by telegram_user_id."""
    user = session.scalar(
        select(User)
        .where(User.id == user_ref)
        
    )
    if user:
        return user
    return session.scalar(
        select(User).where(User.telegram_user_id == user_ref)
    )


def _fetch_all_users(session) -> list[User]:
    return session.scalars(
        select(User)
        
        .order_by(User.is_active.desc(), User.id)
    ).all()


def _set_user_active_status(
    session, actor_telegram_id: int, target_user_id: int, activate: bool
) -> tuple[bool, str]:
    """Включить/выключить пользователя (админ-операция)."""
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return False, "Пользователь не найден в БД."
    if actor.role is None or actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."

    target_user = _resolve_user_by_id_or_telegram(session, target_user_id)
    if not target_user:
        return False, "Пользователь не найден. Укажите #id из /users или telegram id."

    if activate:
        if target_user.is_active:
            return False, (
                f"Пользователь #{target_user.id} ({target_user.full_name}): уже активен."
            )
        target_user.is_active = True
        session.commit()
        return True, f"Пользователь #{target_user.id} ({target_user.full_name}) активирован."

    if not target_user.is_active:
        return False, (
            f"Пользователь #{target_user.id} ({target_user.full_name}): уже деактивирован."
        )
    target_user.is_active = False
    session.commit()
    return True, f"Пользователь #{target_user.id} ({target_user.full_name}) деактивирован."


def _users_total_pages(total_users: int) -> int:
    return max(1, (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE)


def _format_user_card(row: User) -> str:
    tg = str(row.telegram_user_id) if row.telegram_user_id is not None else "-"
    un = f"@{row.telegram_username}" if row.telegram_username else "-"
    return "\n".join(
        [
            f"#{row.id} {row.full_name}",
            f"roles: {_format_roles_line(row)} | active: {row.is_active}",
            f"telegram: {un} (id: {tg})",
        ]
    )


def _build_users_page_view(
    user_rows: list[User], page: int, role_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    total_pages = _users_total_pages(len(user_rows))
    page = max(0, min(page, total_pages - 1))
    start = page * USERS_PER_PAGE
    page_rows = user_rows[start : start + USERS_PER_PAGE]

    header = f"Пользователи ({len(user_rows)})"
    if role_name:
        header += f" — роль: {role_name}"
    header += ":"
    if total_pages > 1:
        header += f"\nстр. {page + 1}/{total_pages}"
    header += (
        "\n\nАктивировать: /activate_user"
        "\nДеактивировать: /deactivate_user"
        "\nПрисвоить роль: /add_role"
        "\nЗабрать роль: /remove_role"
    )

    if page_rows:
        body = "\n\n".join(_format_user_card(row) for row in page_rows)
        text = f"{header}\n\n{body}"
    else:
        text = f"{header}\n\nНа этой странице нет пользователей."

    keyboard = _build_users_page_keyboard(page, total_pages)
    return text, keyboard


def _build_users_page_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"upage:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"upage:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    if not buttons:
        return None
    return InlineKeyboardMarkup(buttons)


def _fetch_all_connections(session) -> list[Connection]:
    return session.scalars(select(Connection).order_by(Connection.name)).all()


def _connections_total_pages(total_connections: int) -> int:
    return max(1, (total_connections + CONNECTIONS_PER_PAGE - 1) // CONNECTIONS_PER_PAGE)


def _format_connection_card(connection: Connection) -> str:
    host, database, user = parse_database_url(connection.database_url)
    audience = _connection_visible_to_roles(connection)
    if audience is None:
        roles_text = "все пользователи"
    elif audience:
        roles_text = ", ".join(audience)
    else:
        roles_text = "никому (кроме Admin)"
    creator = "-"
    if connection.created_by:
        creator = f"#{connection.created_by.id} {connection.created_by.full_name}"
        if connection.created_by.telegram_username:
            creator += f" (@{connection.created_by.telegram_username})"
    return "\n".join(
        [
            f"#{connection.id} {connection.name}",
            f"host: {host} | active: {connection.is_active}",
            f"база: {database}",
            f"пользователь: {user}",
            f"видимость (роли): {roles_text}",
            f"создал: {creator}",
        ]
    )


def _build_connections_page_view(
    connection_rows: list[Connection], page: int, role_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    total_pages = _connections_total_pages(len(connection_rows))
    page = max(0, min(page, total_pages - 1))
    start = page * CONNECTIONS_PER_PAGE
    page_rows = connection_rows[start : start + CONNECTIONS_PER_PAGE]

    header = f"Подключения ({len(connection_rows)})"
    if role_name:
        header += f" — роль: {role_name}"
    header += ":"
    if total_pages > 1:
        header += f"\nстр. {page + 1}/{total_pages}"
    header += (
        "\n\nСоздать подключение: /create_connection"
        "\nАктивировать: /activate_connection"
        "\nПроверить: /check_connection"
        "\nИзменить: /edit_connection"
        "\nДеактивировать: /deactivate_connection"
    )

    if page_rows:
        body = "\n\n".join(_format_connection_card(row) for row in page_rows)
        text = f"{header}\n\n{body}"
    else:
        text = f"{header}\n\nНа этой странице нет подключений."

    keyboard = _build_connections_page_keyboard(page_rows, page, total_pages)
    return text, keyboard


def _build_connections_page_keyboard(
    page_rows: list[Connection], page: int, total_pages: int
) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"cpage:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"cpage:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    if not buttons:
        return None
    return InlineKeyboardMarkup(buttons)


def _resolve_connection_by_id_or_name(session, connection_ref: str) -> Connection | None:
    if connection_ref.isdigit():
        connection = session.scalar(
            select(Connection).where(Connection.id == int(connection_ref))
        )
        if connection:
            return connection
    return session.scalar(select(Connection).where(Connection.name == connection_ref))


def _set_connection_active_status(
    session, actor_telegram_id: int, connection_ref: str, activate: bool
) -> tuple[bool, str]:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return False, "Пользователь не найден в БД."
    if actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."

    connection = _resolve_connection_by_id_or_name(session, connection_ref)
    if not connection:
        return False, "Подключение не найдено. Укажите #id из /connections или имя."

    if activate:
        if connection.is_active:
            return False, "Подключение уже активно."
        connection.is_active = True
        session.commit()
        return True, f"Подключение '{connection.name}' активировано."

    if not connection.is_active:
        return False, "Подключение уже деактивировано."
    connection.is_active = False
    session.commit()
    return True, f"Подключение '{connection.name}' деактивировано."


def _extract_user_id_from_command(
    args: list[str], message_text: str | None, activate: bool
) -> int | None:
    if len(args) == 1 and args[0].isdigit():
        return int(args[0])

    if not message_text:
        return None

    command_name = "activate_user" if activate else "deactivate_user"
    for part in message_text.strip().split():
        if part.startswith(f"/{command_name}"):
            suffix = part.removeprefix(f"/{command_name}")
            if suffix.isdigit():
                return int(suffix)
            continue
        if part.isdigit():
            return int(part)
    return None


def _extract_command_payload(message_text: str | None, command_name: str) -> str:
    if not message_text:
        return ""

    stripped = message_text.strip()
    first_token, _, remainder = stripped.partition(" ")
    command_token = first_token.split("@", 1)[0]
    if command_token != f"/{command_name}":
        return ""
    return remainder.strip()


async def triggers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return

        trigger_rows = _fetch_visible_triggers(session, actor)
        if not trigger_rows:
            await update.message.reply_text(
                f"Доступных триггеров пока нет (роль: {actor.role.name}).\n\n"
                + _triggers_commands_help()
            )
            return

        text, keyboard = _build_triggers_page_view(trigger_rows, page=0, role_name=actor.role.name)
        if keyboard:
            await update.message.reply_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text)


async def triggers_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not query.message:
        return

    if query.data == "tsched":
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            actor = _get_bound_user(session, update.effective_user.id)
            if not actor:
                await query.answer("Пользователь не найден.", show_alert=True)
                return
            trigger_rows = _fetch_visible_triggers(session, actor)
            text = _build_triggers_day_schedule_text(
                trigger_rows, application=context.application
            )
        await query.answer()
        await query.message.reply_text(text)
        return

    if not query.data.startswith("tpage:"):
        await query.answer()
        return

    page_raw = query.data.removeprefix("tpage:")
    if not page_raw.isdigit():
        await query.answer()
        return

    page = int(page_raw)
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await query.answer("Пользователь не найден.", show_alert=True)
            return
        trigger_rows = _fetch_visible_triggers(session, actor)
        text, keyboard = _build_triggers_page_view(trigger_rows, page=page, role_name=actor.role.name)

    if keyboard:
        await query.edit_message_text(text, reply_markup=keyboard)
    else:
        await query.edit_message_text(text)
    await query.answer()


# --- Пошаговое создание триггера (/create_trigger) ---

CT_NAME, CT_CONNECTION, CT_TYPE, CT_GROUP, CT_SCHEDULE, CT_SQL, CT_MESSAGE, CT_CONFIRM = range(10, 18)


def _render_trigger_message(template: str | None, params: dict[str, object] | object) -> str:
    """Подставить параметры $1, $2… в текст сообщения.

    `params` — словарь {номер: значение}. Для обратной совместимости
    принимается одиночное значение (тогда это $1). Отсутствующие параметры
    остаются в тексте как есть ($3 без столбца 3).
    """
    if not isinstance(params, dict):
        params = {"1": params}
    if not template:
        # Без шаблона: одно значение как есть, несколько — построчно.
        if len(params) <= 1:
            return str(params.get("1", ""))
        return "\n".join(f"${key} = {value}" for key, value in params.items())
    return re.sub(
        r"\$(\d+)",
        lambda match: str(params.get(match.group(1), match.group(0))),
        template,
    )


def _format_row_values(params: dict[str, object]) -> str:
    return " | ".join(f"${key}={value}" for key, value in params.items())


def _fetch_usable_connections(session, actor: User) -> list[Connection]:
    rows = session.scalars(
        select(Connection).where(Connection.is_active.is_(True)).order_by(Connection.name)
    ).all()
    return [connection for connection in rows if _user_can_use_connection(actor, connection)]


def _parse_allowed_roles(allowed_roles: str | None) -> list[str]:
    if not allowed_roles:
        return []
    return [role.strip() for role in allowed_roles.split(",") if role.strip()]


def _create_trigger_summary(state: dict) -> str:
    schedule = state.get("schedule") or "- (только вручную)"
    if state.get("trigger_type") == "group":
        topic = state.get("topic_id")
        target = (
            f"group (chat_id: {state.get('chat_id')}, топик: {topic})"
            if topic
            else f"group (chat_id: {state.get('chat_id')})"
        )
    else:
        target = "personal"
    message_template = state.get("message_template") or "- (значение запроса)"
    return (
        f"Имя: {state['name']}\n"
        f"Подключение: {state['connection_name']}\n"
        f"Тип: {target}\n"
        f"Расписание: {schedule}\n"
        f"Текст сообщения: {message_template}\n"
        f"SQL: {state['sql']}"
    )


async def create_trigger_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END
    context.user_data["create_trigger"] = {}
    await update.message.reply_text(
        "Создание триггера, шаг 1 из 7 — имя.\n\n"
        "Пришлите уникальное имя триггера.\n"
        "Отмена: /cancel"
    )
    return CT_NAME


async def create_trigger_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_trigger")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Имя не должно быть пустым. Попробуйте ещё раз или /cancel")
        return CT_NAME

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return ConversationHandler.END
        if session.scalar(select(Trigger).where(Trigger.name == name)):
            await update.message.reply_text(
                f"Триггер с именем '{name}' уже существует. Пришлите другое имя или /cancel"
            )
            return CT_NAME
        connections = _fetch_usable_connections(session, actor)
    if not connections:
        await update.message.reply_text("Нет доступных активных подключений.")
        return ConversationHandler.END

    state["name"] = name
    buttons = [
        [InlineKeyboardButton(f"{c.name}", callback_data=f"ctrig:conn:{c.id}")]
        for c in connections
    ]
    await update.message.reply_text(
        f"Шаг 2 из 7 — подключение (для «{name}»).\n\n"
        "Выберите кнопкой или пришлите #id/имя из /connections.\n"
        "Отмена: /cancel",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CT_CONNECTION


async def create_trigger_connection_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_trigger")
    if state is None:
        return ConversationHandler.END
    query = update.callback_query
    if query and query.data and query.data.startswith("ctrig:conn:"):
        await query.answer()
        connection_id = int(query.data.removeprefix("ctrig:conn:"))
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            connection = session.get(Connection, connection_id)
        if not connection or not connection.is_active:
            await query.edit_message_text("Подключение не найдено. Начните заново: /create_trigger")
            return ConversationHandler.END
        state["connection_id"] = connection.id
        state["connection_name"] = connection.name
        await query.edit_message_text(f"Подключение: {connection.name}")
        await _create_trigger_ask_type(context, query.message.chat_id)
        return CT_TYPE

    return CT_TYPE


async def _create_trigger_ask_type(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Личный (ответ мне в личку)", callback_data="ctrig:personal")],
            [InlineKeyboardButton("Групповой (в чат Telegram)", callback_data="ctrig:group")],
        ]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Шаг 3 из 7 — тип триггера.\n\nОтмена: /cancel",
        reply_markup=keyboard,
    )


async def create_trigger_receive_connection_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    state = context.user_data.get("create_trigger")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    ref = update.message.text.strip()
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return ConversationHandler.END
        connection = _resolve_connection_by_id_or_name(session, ref)
        if not connection or not connection.is_active or not _user_can_use_connection(actor, connection):
            await update.message.reply_text(
                "Подключение не найдено или нет доступа. Попробуйте ещё раз или /cancel"
            )
            return CT_CONNECTION
        state["connection_id"] = connection.id
        state["connection_name"] = connection.name
    await _create_trigger_ask_type(context, update.effective_chat.id)
    return CT_TYPE


async def create_trigger_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_trigger")
    if state is None or not update.callback_query or not update.callback_query.data:
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()

    if query.data == "ctrig:personal":
        state["trigger_type"] = "personal"
        state["chat_id"] = None
        await query.edit_message_text("Тип: личный (уведомления придут вам в личку).")
        await _create_trigger_ask_schedule(context, query.message.chat_id)
        return CT_SCHEDULE

    if query.data == "ctrig:group":
        state["trigger_type"] = "group"
        await query.edit_message_text(
            "Шаг 3 из 7 — чат для уведомлений.\n\n"
            "Пришлите chat_id чата Telegram или ссылку на сообщение в чате.\n"
            "Например: -1001234567890 или https://t.me/c/2345678901/496\n"
            "(бот должен быть добавлен в этот чат).\n"
            "Отмена: /cancel"
        )
        return CT_GROUP

    return CT_TYPE


async def create_trigger_group_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_trigger")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    parsed_chat = parse_chat_ref(update.message.text)
    if parsed_chat is None:
        await update.message.reply_text(
            "Не распознал чат. Пришлите числовой chat_id (например -1001234567890) "
            "или ссылку вида https://t.me/c/2345678901/496. Попробуйте ещё раз или /cancel"
        )
        return CT_GROUP
    state["chat_id"], state["topic_id"] = parsed_chat
    await update.message.reply_text(f"Чат для уведомлений: {state['chat_id']}")
    await _create_trigger_ask_schedule(context, update.effective_chat.id)
    return CT_SCHEDULE


async def _create_trigger_ask_schedule(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Шаг 4 из 7 — расписание.\n\n"
            "Пришлите расписание:\n"
            "-  — без расписания (только вручную через /check_trigger)\n"
            "every:5m / every:1h — каждые 5 минут / час\n"
            "daily:09:00 — каждый день в 09:00 МЕСТНОГО времени (хранится в UTC)\n"
            "Отмена: /cancel"
        ),
    )


async def create_trigger_receive_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_trigger")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    raw = update.message.text.strip()
    try:
        # Пользователь задаёт daily:HH:MM по местному времени — переводим в UTC.
        schedule = normalize_schedule(daily_input_to_utc(raw))
        if schedule is not None:
            parse_schedule(schedule)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\n\nПопробуйте ещё раз или /cancel")
        return CT_SCHEDULE
    state["schedule"] = schedule
    await update.message.reply_text(
        f"Расписание: {format_schedule_label(schedule)}.\n\n"
        "Шаг 5 из 7 — SQL-запрос.\n\n"
        "Запрос должен вернуть ровно 1 строку.\n"
        "Для нескольких параметров именуйте столбцы числами в кавычках:\n"
        "SELECT x AS \"1\", y AS \"2\"\n"
        "Столбец \"1\" подставится в $1 текста сообщения, \"2\" — в $2 и т.д.\n"
        "Один столбец можно не именовать.\n"
        "Он будет проверен прямо сейчас.\n"
        "Отмена: /cancel"
    )
    return CT_SQL


async def create_trigger_receive_sql(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_trigger")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    sql = update.message.text.strip()
    if not sql:
        await update.message.reply_text("SQL не должен быть пустым. Попробуйте ещё раз или /cancel")
        return CT_SQL

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        connection = session.get(Connection, state["connection_id"])
        if not connection:
            await update.message.reply_text("Подключение не найдено. Начните заново: /create_trigger")
            return ConversationHandler.END
        ok, details, params = validate_and_execute_row_sql(connection.database_url, sql)
    if not ok:
        await update.message.reply_text(
            f"SQL не прошёл проверку:\n{details}\n\nПришлите исправленный запрос или /cancel"
        )
        return CT_SQL

    state["sql"] = sql
    state["test_value"] = _format_row_values(params)
    await update.message.reply_text(
        "SQL принят, проверочное значение:\n"
        f"{_format_row_values(params)}\n\n"
        "Шаг 6 из 7 — текст сообщения.\n\n"
        "Текст, который придёт при срабатывании триггера.\n"
        "$1 будет заменён на результат запроса.\n"
        "Пришлите `-`, чтобы отправлять просто значение.\n\n"
        "Пример:\n"
        "Текущая версия стенда: $1\n"
        "Отмена: /cancel"
    )
    return CT_MESSAGE


async def create_trigger_receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("create_trigger")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    raw = update.message.text.strip()
    state["message_template"] = None if raw == "-" else raw
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💾 Создать", callback_data="ctrig:save"),
                InlineKeyboardButton("Отмена", callback_data="ctrig:cancel"),
            ]
        ]
    )
    await update.message.reply_text(
        f"Шаг 7 из 7 — проверка.\n\n{_create_trigger_summary(state)}\n\n"
        f"Проверочное значение: {state['test_value']}\n\nСоздаём триггер?",
        reply_markup=keyboard,
    )
    return CT_CONFIRM


async def create_trigger_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.pop("create_trigger", None)
    query = update.callback_query
    if state is None or not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    if query.data == "ctrig:cancel":
        await query.edit_message_text("Создание триггера отменено.")
        return ConversationHandler.END
    if query.data != "ctrig:save":
        return CT_CONFIRM

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        ok, message, trigger_id = _create_trigger_record(
            session,
            actor_telegram_id=update.effective_user.id,
            name=state["name"],
            connection_ref=state["connection_name"],
            trigger_type=state["trigger_type"],
            chat_id=state.get("chat_id"),
            message_thread_id=state.get("topic_id"),
            schedule=state["schedule"],
            sql_query=state["sql"],
            message_template=state.get("message_template"),
        )
    if not ok or trigger_id is None:
        await query.edit_message_text(message)
        return ConversationHandler.END
    await query.edit_message_text(message)
    _schedule_trigger_job(context.application, trigger_id, state["schedule"])
    return ConversationHandler.END


async def create_trigger_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("create_trigger", None)
    if update.message:
        await update.message.reply_text("Создание триггера отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Создание триггера отменено.")
    return ConversationHandler.END


def create_trigger_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("create_trigger", create_trigger_start)],
        states={
            CT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_trigger_receive_name)],
            CT_CONNECTION: [
                CallbackQueryHandler(create_trigger_connection_chosen, pattern=r"^ctrig:conn:"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, create_trigger_receive_connection_text
                ),
            ],
            CT_TYPE: [CallbackQueryHandler(create_trigger_type_chosen, pattern=r"^ctrig:")],
            CT_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_trigger_group_chosen)],
            CT_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_trigger_receive_schedule)],
            CT_SQL: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_trigger_receive_sql)],
            CT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_trigger_receive_message)],
            CT_CONFIRM: [CallbackQueryHandler(create_trigger_confirm, pattern=r"^ctrig:")],
        },
        fallbacks=[CommandHandler("cancel", create_trigger_cancel)],
        allow_reentry=True,
    )


# --- Пошаговое изменение триггера (/edit_trigger) ---

EDIT_TRIGGER_WAIT_REF, EDIT_TRIGGER_FIELD_SELECT, EDIT_TRIGGER_WAIT_VALUE = range(3)

EDIT_TRIGGER_FIELD_LABELS = {
    "connection": "Подключение",
    "group": "Тип/чат",
    "schedule": "Расписание",
    "sql": "SQL-запрос",
    "message": "Текст сообщения",
}


def _edit_trigger_current_values(trigger: Trigger) -> dict[str, str]:
    return {
        "connection": trigger.connection.name,
        "group": (
            (
                f"group (chat_id: {trigger.chat_id}, топик: {trigger.message_thread_id})"
                if trigger.message_thread_id
                else f"group (chat_id: {trigger.chat_id})"
            )
            if trigger.trigger_type == "group"
            else "personal"
        ),
        "schedule": format_schedule_label(trigger.schedule),
        "sql": trigger.sql_query,
        "message": trigger.message_template or "- (значение запроса)",
    }


def _edit_trigger_menu_text(trigger: Trigger, changes: dict[str, str]) -> str:
    current = _edit_trigger_current_values(trigger)
    lines = [
        f"Изменение триггера #{trigger.id} «{trigger.name}»",
        "",
        "Выберите поле кнопкой, введите новое значение — и так для каждого поля.",
        "Когда всё готово, нажмите «Сохранить».",
        "",
    ]
    for field, label in EDIT_TRIGGER_FIELD_LABELS.items():
        line = f"• {label}: {current[field]}"
        if field in changes:
            line += f"  →  {changes[field]}"
        lines.append(line)
    lines.append("")
    lines.append("Изменения сохраняются только после нажатия «Сохранить».")
    return "\n".join(lines)


def _edit_trigger_menu_keyboard(changes: dict[str, str]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for field, label in EDIT_TRIGGER_FIELD_LABELS.items():
        mark = " ✓" if field in changes else ""
        buttons.append(
            [InlineKeyboardButton(f"Изменить: {label}{mark}", callback_data=f"etrig:field:{field}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("💾 Сохранить", callback_data="etrig:save"),
            InlineKeyboardButton("Отмена", callback_data="etrig:cancel"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def _load_editable_trigger(session, actor: User, trigger_ref: str) -> Trigger | None:
    trigger = _resolve_trigger_by_id_or_name(session, trigger_ref)
    if not trigger:
        return None
    if actor.role.name != "Admin" and trigger.created_by_user_id != actor.id:
        return None
    return trigger


async def edit_trigger_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    trigger_ref = " ".join(context.args).strip() if context.args else ""
    if not trigger_ref:
        await update.message.reply_text(
            "Какой триггер изменить? Пришлите #id или имя из /triggers.\n"
            "Отмена: /cancel"
        )
        return EDIT_TRIGGER_WAIT_REF
    return await _edit_trigger_begin(update, context, trigger_ref)


async def edit_trigger_receive_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_TRIGGER_WAIT_REF
    return await _edit_trigger_begin(update, context, update.message.text.strip())


async def _edit_trigger_begin(update: Update, context: ContextTypes.DEFAULT_TYPE, trigger_ref: str) -> int:
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return ConversationHandler.END
        trigger = _load_editable_trigger(session, actor, trigger_ref)
        if not trigger:
            await update.message.reply_text(
                "Триггер не найден или нет прав. Изменить триггер может только автор или Admin."
            )
            return ConversationHandler.END
        context.user_data["edit_trigger"] = {"trigger_id": trigger.id, "changes": {}}
        text = _edit_trigger_menu_text(trigger, {})
    await update.message.reply_text(text, reply_markup=_edit_trigger_menu_keyboard({}))
    return EDIT_TRIGGER_FIELD_SELECT


def _edit_trigger_value_prompt(trigger: Trigger, field: str) -> str:
    current = _edit_trigger_current_values(trigger)
    hints = {
        "connection": "#id или имя из /connections",
        "group": "personal — личный; group:<chat_id или ссылка t.me> — в чат",
        "schedule": "daily:HH:MM — местное время; `-` (только вручную), every:5m, every:1h",
        "sql": "1 строка; несколько значений — столбцы \"1\", \"2\"… (SELECT x AS \"1\", y AS \"2\")",
        "message": "$1, $2… — значения столбцов; `-` — отправлять просто значение",
    }
    return (
        f"Текущее значение ({EDIT_TRIGGER_FIELD_LABELS[field]}):\n"
        f"{current[field]}\n\n"
        f"Пришлите новое значение.\n{hints[field]}\n"
        f"Отмена: /cancel"
    )


async def edit_trigger_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return EDIT_TRIGGER_FIELD_SELECT

    if query.data == "etrig:cancel":
        await query.answer()
        context.user_data.pop("edit_trigger", None)
        await query.edit_message_text("Изменение триггера отменено.")
        return ConversationHandler.END

    state = context.user_data.get("edit_trigger")
    if state is None:
        await query.edit_message_text("Сессия изменения утеряна. Начните заново: /edit_trigger")
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        trigger = (
            session.get(Trigger, state["trigger_id"])
            if actor
            else None
        )
        if not trigger or not actor:
            context.user_data.pop("edit_trigger", None)
            await query.edit_message_text("Триггер не найден. Начните заново: /edit_trigger")
            return ConversationHandler.END

        if query.data == "etrig:save":
            changes = state["changes"]
            if not changes:
                await query.answer("Нет изменений.", show_alert=True)
                return EDIT_TRIGGER_FIELD_SELECT
            ok, message, trigger_id = _apply_trigger_edits(
                session,
                actor_telegram_id=update.effective_user.id,
                trigger_id=trigger.id,
                changes=changes,
            )
            new_schedule = trigger.schedule
        else:
            field = query.data.removeprefix("etrig:field:")
            if field not in EDIT_TRIGGER_FIELD_LABELS:
                return EDIT_TRIGGER_FIELD_SELECT
            state["field"] = field
            await query.edit_message_text(_edit_trigger_value_prompt(trigger, field))
            return EDIT_TRIGGER_WAIT_VALUE

    if query.data == "etrig:save":
        context.user_data.pop("edit_trigger", None)
        await query.edit_message_text(message)
        if ok and trigger_id is not None:
            _schedule_trigger_job(context.application, trigger_id, new_schedule)
        return ConversationHandler.END

    return EDIT_TRIGGER_FIELD_SELECT


async def edit_trigger_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("edit_trigger")
    if state is None or not update.message or not update.message.text:
        return ConversationHandler.END
    field = state.get("field")
    if not field:
        return ConversationHandler.END

    value = update.message.text.strip()
    error = _validate_trigger_field_value(
        context.application.bot_data["session_factory"],
        actor_telegram_id=update.effective_user.id,
        trigger_id=state["trigger_id"],
        field=field,
        value=value,
        state_changes=state["changes"],
    )
    if error:
        await update.message.reply_text(f"{error}\n\nПопробуйте ещё раз или /cancel")
        return EDIT_TRIGGER_WAIT_VALUE

    state["changes"][field] = value
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        trigger = session.get(Trigger, state["trigger_id"])
        if not trigger:
            context.user_data.pop("edit_trigger", None)
            await update.message.reply_text("Триггер не найден. Начните заново: /edit_trigger")
            return ConversationHandler.END
        text = _edit_trigger_menu_text(trigger, state["changes"])
    await update.message.reply_text("Принято.\n\n" + text, reply_markup=_edit_trigger_menu_keyboard(state["changes"]))
    return EDIT_TRIGGER_FIELD_SELECT


async def edit_trigger_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit_trigger", None)
    if update.message:
        await update.message.reply_text("Изменение триггера отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Изменение триггера отменено.")
    return ConversationHandler.END


def _validate_trigger_field_value(
    session_factory,
    actor_telegram_id: int,
    trigger_id: int,
    field: str,
    value: str,
    state_changes: dict[str, str] | None = None,
) -> str | None:
    if not value:
        return "Значение не должно быть пустым."
    with session_factory() as session:
        actor = _get_bound_user(session, actor_telegram_id)
        trigger = session.get(Trigger, trigger_id) if actor else None
        if not actor or not trigger:
            return "Триггер не найден."

        if field == "connection":
            connection = _resolve_connection_by_id_or_name(session, value)
            if not connection:
                return "Подключение не найдено. Укажите #id или имя из /connections."
            if not connection.is_active:
                return "Подключение деактивировано."
            if not _user_can_use_connection(actor, connection):
                return "Нет доступа к этому подключению."
            return None

        if field == "group":
            if value.lower() == "personal":
                return None
            if not value.lower().startswith("group:"):
                return "Формат: personal или group:<chat_id>"
            chat_raw = value.split(":", 1)[1].strip()
            if parse_chat_ref(chat_raw) is None:
                return (
                    "Не распознал чат. Укажите числовой chat_id "
                    "(например -1001234567890) или ссылку https://t.me/c/2345678901/496."
                )
            return None

        if field == "schedule":
            try:
                schedule = normalize_schedule(value)
                if schedule is not None:
                    parse_schedule(schedule)
            except ValueError as exc:
                return str(exc)
            return None

        if field == "message":
            return None

        if field == "sql":
            if "connection" in state_changes:
                connection = _resolve_connection_by_id_or_name(session, state_changes["connection"])
            else:
                connection = trigger.connection
            if not connection:
                return "Подключение не найдено."
            ok, details, _params = validate_and_execute_row_sql(
                connection.database_url, value
            )
            if not ok:
                return f"SQL не прошёл проверку:\n{details}"
            return None

    return None


def _apply_trigger_edits(
    session,
    actor_telegram_id: int,
    trigger_id: int,
    changes: dict[str, str],
) -> tuple[bool, str, int | None]:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return False, "Пользователь не найден в БД.", None
    trigger = session.get(Trigger, trigger_id)
    if not trigger:
        return False, "Триггер не найден.", None
    if actor.role.name != "Admin" and trigger.created_by_user_id != actor.id:
        return False, "Изменить триггер может только автор или Admin.", None

    if "connection" in changes:
        connection = _resolve_connection_by_id_or_name(session, changes["connection"])
        if not connection or not connection.is_active or not _user_can_use_connection(actor, connection):
            return False, "Подключение недоступно, изменения не сохранены.", None
        trigger.connection_id = connection.id

    if "group" in changes:
        value = changes["group"].lower()
        if value == "personal":
            trigger.trigger_type = "personal"
            trigger.chat_id = None
            trigger.message_thread_id = None
        else:
            chat_raw = changes["group"].split(":", 1)[1].strip()
            parsed_chat = parse_chat_ref(chat_raw)
            if parsed_chat is None:
                return (
                    False,
                    "Не распознал чат. Укажите числовой chat_id "
                    "(например -1001234567890) или ссылку https://t.me/c/2345678901/496.",
                    None,
                )
            trigger.trigger_type = "group"
            trigger.chat_id, trigger.message_thread_id = parsed_chat

    if "schedule" in changes:
        try:
            # Ввод daily:HH:MM — местное время, храним в UTC.
            schedule = normalize_schedule(daily_input_to_utc(changes["schedule"]))
            if schedule is not None:
                parse_schedule(schedule)
        except ValueError as exc:
            return False, str(exc), None
        trigger.schedule = schedule

    if "message" in changes:
        raw = changes["message"].strip()
        trigger.message_template = None if raw == "-" else raw

    if "sql" in changes:
        trigger.sql_query = changes["sql"].strip().rstrip(";")

    session.commit()
    return (
        True,
        f"Триггер «{trigger.name}» (#{trigger.id}) изменён.\n"
        f"Изменённые поля: {', '.join(EDIT_TRIGGER_FIELD_LABELS[f] for f in changes)}.",
        trigger.id,
    )


# --- Роли: список, создание, изменение, удаление ---

ROLE_WAIT_NAME, ROLE_PICK, ROLE_CONFIRM_DELETE, ROLE_WAIT_DESCRIPTION, ROLE_FIELD_PICK = range(50, 55)


async def _roles_require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        role_name = actor.role.name if actor and actor.role else None
    if role_name != "Admin":
        if update.message:
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
        elif update.callback_query:
            await update.callback_query.answer("Недостаточно прав.", show_alert=True)
        return False
    return True


def _format_role_card(role: Role, users_count: int, connections_count: int) -> str:
    card = f"#{role.id} {role.name}"
    if role.description:
        card += f"\nОписание: {role.description}"
    card += f"\nПользователей: {users_count} | Подключений с этой ролью: {connections_count}"
    return card


def _role_users_count(session, role_id: int) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
        )
        or 0
    )


def _role_connections_count(session, role_name: str) -> int:
    rows = session.scalars(select(Connection.allowed_roles)).all()
    count = 0
    for raw in rows:
        if role_name in _parse_allowed_roles(raw):
            count += 1
    return count


async def roles_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return
        if actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return
        role_rows = session.scalars(select(Role).order_by(Role.id)).all()
        cards = [
            _format_role_card(
                role,
                _role_users_count(session, role.id),
                _role_connections_count(session, role.name),
            )
            for role in role_rows
        ]

    header = (
        f"Роли ({len(role_rows)}) — роль: {actor.role.name}:"
        "\n\nСоздать: /create_role"
        "\nПереименовать: /update_role"
        "\nУдалить: /delete_role"
    )
    body = "\n\n".join(cards) if cards else "Ролей нет."
    await update.message.reply_text(f"{header}\n\n{body}")


async def create_role_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    role_name = " ".join(context.args).strip() if context.args else ""
    if role_name:
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            ok, message = _create_role_record(session, update.effective_user.id, role_name)
        await update.message.reply_text(message)
        return ConversationHandler.END

    if not await _roles_require_admin(update, context):
        return ConversationHandler.END
    await update.message.reply_text(
        "Создание роли, шаг 1 из 2 — название.\n\n"
        "Пришлите название новой роли.\nОтмена: /cancel"
    )
    return ROLE_WAIT_NAME


def _create_role_record(
    session, actor_telegram_id: int, role_name: str, description: str = ""
) -> tuple[bool, str]:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor or actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."
    role_name = role_name.strip()
    if not role_name:
        return False, "Название роли не должно быть пустым."
    if session.scalar(select(Role).where(Role.name == role_name)):
        return False, f"Роль '{role_name}' уже существует."
    session.add(Role(name=role_name, description=(description or "").strip()))
    session.commit()
    return True, f"Роль '{role_name}' создана."


async def create_role_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ROLE_WAIT_NAME
    role_name = update.message.text.strip()
    if not role_name:
        await update.message.reply_text("Название роли не должно быть пустым. Попробуйте ещё раз или /cancel")
        return ROLE_WAIT_NAME

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        if session.scalar(select(Role).where(Role.name == role_name)):
            await update.message.reply_text(
                f"Роль '{role_name}' уже существует. Пришлите другое название или /cancel"
            )
            return ROLE_WAIT_NAME
    context.user_data["role_create_name"] = role_name
    await update.message.reply_text(
        f"Шаг 2 из 2 — описание роли «{role_name}».\n\n"
        "Пришлите описание или `-`, чтобы оставить без описания.\nОтмена: /cancel"
    )
    return ROLE_WAIT_DESCRIPTION


async def create_role_receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ROLE_WAIT_DESCRIPTION
    raw = update.message.text.strip()
    description = "" if raw == "-" else raw
    role_name = context.user_data.pop("role_create_name", None)
    if role_name is None:
        await update.message.reply_text("Сессия утеряна. Начните заново: /create_role")
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        ok, message = _create_role_record(
            session, update.effective_user.id, role_name, description
        )
    await update.message.reply_text(message)
    return ConversationHandler.END


async def update_role_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END
    if not await _roles_require_admin(update, context):
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        role_rows = session.scalars(select(Role).order_by(Role.id)).all()
    if not role_rows:
        await update.message.reply_text("Ролей нет.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(role.name, callback_data=f"roledit:{role.id}")]
        for role in role_rows
    ]
    await update.message.reply_text(
        "Какую роль изменить?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ROLE_PICK


async def update_role_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ROLE_PICK
    await query.answer()
    if not query.data.startswith("roledit:"):
        return ROLE_PICK

    role_id = int(query.data.removeprefix("roledit:"))
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        role = session.get(Role, role_id)
    if not role:
        await query.edit_message_text("Роль не найдена.")
        return ConversationHandler.END
    context.user_data["role_rename_id"] = role.id
    description = role.description or "-"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Название", callback_data="redit:name")],
            [InlineKeyboardButton("📝 Описание", callback_data="redit:desc")],
        ]
    )
    await query.edit_message_text(
        f"Что изменить у роли «{role.name}»?\n\n"
        f"Текущее описание: {description}",
        reply_markup=keyboard,
    )
    return ROLE_FIELD_PICK


async def update_role_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ROLE_FIELD_PICK
    await query.answer()
    if not query.data.startswith("redit:"):
        return ROLE_FIELD_PICK

    role_id = context.user_data.get("role_rename_id")
    if role_id is None:
        await query.edit_message_text("Сессия утеряна. Начните заново: /update_role")
        return ConversationHandler.END

    if query.data == "redit:name":
        await query.edit_message_text(
            "Пришлите новое название роли.\nОтмена: /cancel"
        )
        return ROLE_WAIT_NAME

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        role = session.get(Role, role_id)
    if not role:
        await query.edit_message_text("Роль не найдена.")
        return ConversationHandler.END
    await query.edit_message_text(
        f"Текущее описание роли «{role.name}»: {role.description or '-'}\n\n"
        "Пришлите новое описание или `-`, чтобы оставить без описания.\nОтмена: /cancel"
    )
    return ROLE_WAIT_DESCRIPTION


async def update_role_receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ROLE_WAIT_DESCRIPTION
    role_id = context.user_data.pop("role_rename_id", None)
    if role_id is None:
        await update.message.reply_text("Сессия утеряна. Начните заново: /update_role")
        return ConversationHandler.END

    raw = update.message.text.strip()
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor or actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return ConversationHandler.END
        role = session.get(Role, role_id)
        if not role:
            await update.message.reply_text("Роль не найдена.")
            return ConversationHandler.END
        old_description = role.description or "-"
        role.description = "" if raw == "-" else raw
        session.commit()
        new_description = role.description or "-"
        name = role.name
    await update.message.reply_text(
        f"Описание роли «{name}» обновлено:\n{old_description}  →  {new_description}"
    )
    return ConversationHandler.END


async def update_role_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ROLE_WAIT_NAME
    role_id = context.user_data.pop("role_rename_id", None)
    if role_id is None:
        await update.message.reply_text("Сессия утеряна. Начните заново: /update_role")
        return ConversationHandler.END

    new_name = update.message.text.strip()
    if not new_name:
        await update.message.reply_text("Название роли не должно быть пустым. Попробуйте ещё раз или /cancel")
        return ROLE_WAIT_NAME

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor or actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return ConversationHandler.END
        role = session.get(Role, role_id)
        if not role:
            await update.message.reply_text("Роль не найдена.")
            return ConversationHandler.END
        old_name = role.name
        if session.scalar(select(Role).where(Role.name == new_name)):
            await update.message.reply_text(
                f"Роль '{new_name}' уже существует. Пришлите другое название или /cancel"
            )
            context.user_data["role_rename_id"] = role_id
            return ROLE_WAIT_NAME

        role.name = new_name
        # Обновляем упоминания роли в видимости подключений.
        for connection in session.scalars(select(Connection)).all():
            roles = _parse_allowed_roles(connection.allowed_roles)
            if old_name in roles:
                connection.allowed_roles = ", ".join(
                    new_name if r == old_name else r for r in roles
                )
        session.commit()
    await update.message.reply_text(f"Роль '{old_name}' переименована в '{new_name}'.")
    return ConversationHandler.END


async def delete_role_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END
    if not await _roles_require_admin(update, context):
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        role_rows = [
            role
            for role in session.scalars(select(Role).order_by(Role.id)).all()
            if role.name not in PROTECTED_ROLE_NAMES
        ]
    if not role_rows:
        await update.message.reply_text(
            f"Удаляемых ролей нет (базовые {', '.join(sorted(PROTECTED_ROLE_NAMES))} защищены)."
        )
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(role.name, callback_data=f"roledel:{role.id}")]
        for role in role_rows
    ]
    await update.message.reply_text(
        "Какую роль удалить?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ROLE_PICK


async def delete_role_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ROLE_PICK
    await query.answer()
    if not query.data.startswith("roledel:"):
        return ROLE_PICK

    role_id = int(query.data.removeprefix("roledel:"))
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        role = session.get(Role, role_id)
        if not role:
            await query.edit_message_text("Роль не найдена.")
            return ConversationHandler.END
        context.user_data["role_delete_id"] = role.id
        card = _format_role_card(
            role,
            _role_users_count(session, role.id),
            _role_connections_count(session, role.name),
        )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗑 Удалить", callback_data="roledelyes"),
                InlineKeyboardButton("Отмена", callback_data="roledelcancel"),
            ]
        ]
    )
    await query.edit_message_text(
        f"Удалить роль?\n\n{card}\n\n"
        "Пользователи этой роли будут переведены в DefaultUser,\n"
        "упоминания в видимости подключений будут убраны.",
        reply_markup=keyboard,
    )
    return ROLE_CONFIRM_DELETE


async def delete_role_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ROLE_CONFIRM_DELETE
    await query.answer()
    if query.data != "roledelyes":
        await query.edit_message_text("Удаление отменено.")
        return ConversationHandler.END

    role_id = context.user_data.pop("role_delete_id", None)
    if role_id is None:
        await query.edit_message_text("Сессия утеряна. Начните заново: /delete_role")
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor or actor.role.name != "Admin":
            await query.edit_message_text("Недостаточно прав. Команда доступна только Admin.")
            return ConversationHandler.END
        role = session.get(Role, role_id)
        if not role:
            await query.edit_message_text("Роль уже удалена.")
            return ConversationHandler.END
        if role.name in PROTECTED_ROLE_NAMES:
            await query.edit_message_text(f"Роль '{role.name}' защищена от удаления.")
            return ConversationHandler.END

        default_role = session.scalar(select(Role).where(Role.name == "DefaultUser"))
        if not default_role:
            await query.edit_message_text("Базовая роль DefaultUser не найдена.")
            return ConversationHandler.END

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
                remaining = [r for r in roles if r != old_name]
                connection.allowed_roles = ", ".join(remaining)
        session.delete(role)
        session.commit()

    await query.edit_message_text(
        f"Роль '{old_name}' удалена. Пользователей переведено в DefaultUser: {moved_users}."
    )
    return ConversationHandler.END


async def roles_wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("role_rename_id", "role_delete_id", "role_create_name"):
        context.user_data.pop(key, None)
    if update.message:
        await update.message.reply_text("Отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Отменено.")
    return ConversationHandler.END


def roles_conversations() -> list[ConversationHandler]:
    return [
        ConversationHandler(
            entry_points=[CommandHandler("create_role", create_role_start)],
            states={
                ROLE_WAIT_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_role_receive_name)
                ],
                ROLE_WAIT_DESCRIPTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_role_receive_description)
                ],
            },
            fallbacks=[CommandHandler("cancel", roles_wizard_cancel)],
            allow_reentry=True,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("update_role", update_role_start)],
            states={
                ROLE_PICK: [CallbackQueryHandler(update_role_pick, pattern=r"^roledit:\d+$")],
                ROLE_FIELD_PICK: [
                    CallbackQueryHandler(update_role_field_chosen, pattern=r"^redit:")
                ],
                ROLE_WAIT_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, update_role_receive_name)
                ],
                ROLE_WAIT_DESCRIPTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, update_role_receive_description)
                ],
            },
            fallbacks=[CommandHandler("cancel", roles_wizard_cancel)],
            allow_reentry=True,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("delete_role", delete_role_start)],
            states={
                ROLE_PICK: [CallbackQueryHandler(delete_role_pick, pattern=r"^roledel:\d+$")],
                ROLE_CONFIRM_DELETE: [
                    CallbackQueryHandler(
                        delete_role_confirm, pattern=r"^roledelyes$|^roledelcancel$"
                    )
                ],
            },
            fallbacks=[CommandHandler("cancel", roles_wizard_cancel)],
            allow_reentry=True,
        ),
    ]


# --- /add_role и /remove_role: управление ролями пользователя ---

AR_WAIT_USER, AR_WAIT_ROLE, RR_WAIT_USER, RR_WAIT_ROLE = range(60, 64)


def _format_roles_line(user: User) -> str:
    return ", ".join(role.name for role in user.roles) if user.roles else "-"


def _set_single_role(session, target_user: User, role: Role) -> None:
    """Задать роль как единственную (Admin и DefaultUser — эксклюзивные)."""
    target_user.roles = [role]


def _add_role_to_user(session, target_user: User, role: Role) -> tuple[bool, str]:
    if role.name in PROTECTED_ROLE_NAMES and role.name == "Admin":
        if any(r.name == "Admin" for r in target_user.roles):
            return False, f"Пользователь #{target_user.id} ({target_user.full_name}) уже Admin."
        _set_single_role(session, target_user, role)
        return True, (
            f"Пользователю #{target_user.id} ({target_user.full_name}) присвоена роль Admin "
            "(прочие роли сняты — Admin единственная роль)."
        )

    current_names = [r.name for r in target_user.roles]
    if role.name in current_names:
        return False, (
            f"Пользователь #{target_user.id} ({target_user.full_name}) "
            f"уже имеет роль '{role.name}' (роли: {_format_roles_line(target_user)})."
        )

    # DefaultUser — роль «без других ролей»: при добавлении конкретной роли он уходит.
    target_user.roles = [r for r in target_user.roles if r.name != "DefaultUser"] + [role]
    removed_default = "DefaultUser" in current_names
    message = (
        f"Пользователю #{target_user.id} ({target_user.full_name}) присвоена роль "
        f"'{role.name}'. Роли: {_format_roles_line(target_user)}."
    )
    if removed_default:
        message += " DefaultUser снят."
    return True, message


def _remove_roles_from_user(
    session, target_user: User, role_names: list[str]
) -> tuple[bool, str]:
    current_names = [r.name for r in target_user.roles]
    if "Admin" in role_names:
        return False, "Роль Admin нельзя забрать — выдайте пользователю другую роль вместо этого."
    if current_names == ["DefaultUser"]:
        return False, (
            f"У пользователя #{target_user.id} ({target_user.full_name}) только роль "
            "DefaultUser (базовая) — забирать нечего."
        )
    if not any(name in current_names for name in role_names):
        return False, (
            f"У пользователя #{target_user.id} ({target_user.full_name}) нет таких ролей "
            f"(роли: {_format_roles_line(target_user)})."
        )

    target_user.roles = [r for r in target_user.roles if r.name not in role_names]
    message = (
        f"У пользователя #{target_user.id} ({target_user.full_name}) забраны роли: "
        f"{', '.join(n for n in role_names if n in current_names)}."
    )
    if not target_user.roles:
        default_role = session.scalar(select(Role).where(Role.name == "DefaultUser"))
        if default_role:
            target_user.roles.append(default_role)
        message += f" Ролей не осталось, назначен DefaultUser (роли: {_format_roles_line(target_user)})."
    else:
        message += f" Оставшиеся роли: {_format_roles_line(target_user)}."
    return True, message


def _set_user_role(session, actor_telegram_id: int, user_ref: int, role: Role) -> tuple[bool, str]:
    """Назначить роль пользователю (админ-операция)."""
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return False, "Пользователь не найден в БД."
    if actor.role is None or actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."
    target_user = _resolve_user_by_id_or_telegram(session, user_ref)
    if not target_user:
        return False, "Целевой пользователь не найден. Укажите #id из /users или telegram id."
    ok, message = _add_role_to_user(session, target_user, role)
    session.commit()
    return ok, message


async def add_role_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    args = list(context.args)
    if len(args) >= 2:
        ref = args[0].lstrip("#")
        if not ref.isdigit():
            await update.message.reply_text("user_id должен быть числом.")
            return ConversationHandler.END
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            role = session.scalar(
                select(Role).where(Role.name == " ".join(args[1:]).strip())
            )
            if not role:
                await update.message.reply_text(
                    f"Роль '{' '.join(args[1:]).strip()}' не найдена. Список: /roles"
                )
                return ConversationHandler.END
            ok, message = _set_user_role(
                session, update.effective_user.id, int(ref), role
            )
        await update.message.reply_text(message)
        return ConversationHandler.END

    if not await _roles_require_admin(update, context):
        return ConversationHandler.END
    await update.message.reply_text(
        "Присвоение роли.\n\nПришлите #id пользователя из /users или его telegram id.\nОтмена: /cancel"
    )
    return AR_WAIT_USER


async def add_role_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return AR_WAIT_USER
    ref = update.message.text.strip().lstrip("#")
    if not ref.isdigit():
        await update.message.reply_text(
            "Нужен числовой #id из /users или telegram id. Попробуйте ещё раз или /cancel"
        )
        return AR_WAIT_USER

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        target_user = _resolve_user_by_id_or_telegram(session, int(ref))
        if not target_user:
            await update.message.reply_text(
                "Пользователь не найден. Попробуйте ещё раз или /cancel"
            )
            return AR_WAIT_USER
        roles = session.scalars(select(Role).order_by(Role.name)).all()
        context.user_data["add_role_user_id"] = target_user.id
        card = _format_user_card(target_user)
    buttons = [
        [InlineKeyboardButton(role.name, callback_data=f"arole:{role.id}")] for role in roles
    ]
    await update.message.reply_text(
        f"Какую роль присвоить?\n\n{card}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AR_WAIT_ROLE


async def add_role_pick_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return AR_WAIT_ROLE
    await query.answer()
    if not query.data.startswith("arole:"):
        return AR_WAIT_ROLE

    user_id = context.user_data.pop("add_role_user_id", None)
    if user_id is None:
        await query.edit_message_text("Сессия утеряна. Начните заново: /add_role")
        return ConversationHandler.END

    role_id = int(query.data.removeprefix("arole:"))
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        role = session.get(Role, role_id)
        if not role:
            await query.edit_message_text("Роль не найдена.")
            return ConversationHandler.END
        ok, message = _set_user_role(session, update.effective_user.id, user_id, role)
    await query.edit_message_text(message)
    return ConversationHandler.END


async def add_role_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("add_role_user_id", "remove_role_user_id"):
        context.user_data.pop(key, None)
    if update.message:
        await update.message.reply_text("Отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Отменено.")
    return ConversationHandler.END


def add_role_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("add_role", add_role_start)],
        states={
            AR_WAIT_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_role_receive_user)
            ],
            AR_WAIT_ROLE: [CallbackQueryHandler(add_role_pick_role, pattern=r"^arole:\d+$")],
        },
        fallbacks=[CommandHandler("cancel", add_role_cancel)],
        allow_reentry=True,
    )


# --- /remove_role: забрать роль (пошагово) ---


async def remove_role_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    args = list(context.args)
    if len(args) >= 2:
        ref = args[0].lstrip("#")
        role_name = " ".join(args[1:]).strip()
        session_factory = context.application.bot_data["session_factory"]
        with session_factory() as session:
            target_user = _resolve_user_by_id_or_telegram(
                session, int(ref) if ref.isdigit() else -1
            )
            if not target_user:
                await update.message.reply_text("Пользователь не найден.")
                return ConversationHandler.END
            ok, message = _remove_roles_from_user(session, target_user, [role_name])
            session.commit()
        await update.message.reply_text(message)
        return ConversationHandler.END

    if not await _roles_require_admin(update, context):
        return ConversationHandler.END
    await update.message.reply_text(
        "Забрать роль.\n\nПришлите #id пользователя из /users или его telegram id.\nОтмена: /cancel"
    )
    return RR_WAIT_USER


async def remove_role_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return RR_WAIT_USER
    ref = update.message.text.strip().lstrip("#")
    if not ref.isdigit():
        await update.message.reply_text(
            "Нужен числовой #id из /users или telegram id. Попробуйте ещё раз или /cancel"
        )
        return RR_WAIT_USER

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        target_user = _resolve_user_by_id_or_telegram(session, int(ref))
        if not target_user:
            await update.message.reply_text(
                "Пользователь не найден. Попробуйте ещё раз или /cancel"
            )
            return RR_WAIT_USER
        context.user_data["remove_role_user_id"] = target_user.id
        card = _format_user_card(target_user)
        role_names = [r.name for r in target_user.roles]

    if not role_names:
        await update.message.reply_text("У пользователя нет ролей.")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(f"забрать: {name}", callback_data=f"rrm:{name}")]
        for name in role_names
    ]
    buttons.append([InlineKeyboardButton("🗑 Забрать ВСЕ роли", callback_data="rrm:__all__")])
    await update.message.reply_text(
        f"Какую роль забрать?\n\n{card}\n\nМожно забрать одну роль или все сразу.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return RR_WAIT_ROLE


async def remove_role_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return RR_WAIT_ROLE
    await query.answer()
    if not query.data.startswith("rrm:"):
        return RR_WAIT_ROLE

    user_id = context.user_data.pop("remove_role_user_id", None)
    if user_id is None:
        await query.edit_message_text("Сессия утеряна. Начните заново: /remove_role")
        return ConversationHandler.END

    payload = query.data.removeprefix("rrm:")
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        target_user = _resolve_user_by_id_or_telegram(session, user_id)
        if not target_user:
            await query.edit_message_text("Пользователь не найден.")
            return ConversationHandler.END
        if payload == "__all__":
            role_names = [r.name for r in target_user.roles]
            ok, message = _remove_roles_from_user(session, target_user, role_names)
        else:
            ok, message = _remove_roles_from_user(session, target_user, [payload])
        session.commit()
    await query.edit_message_text(message)
    return ConversationHandler.END


def remove_role_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("remove_role", remove_role_start)],
        states={
            RR_WAIT_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_role_receive_user)
            ],
            RR_WAIT_ROLE: [
                CallbackQueryHandler(remove_role_pick, pattern=r"^rrm:")
            ],
        },
        fallbacks=[CommandHandler("cancel", add_role_cancel)],
        allow_reentry=True,
    )


CK_WAIT_REF = 30


async def check_trigger_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    trigger_ref = " ".join(context.args).strip() if context.args else ""
    if trigger_ref:
        return await _check_trigger_show(update, context, trigger_ref)

    await update.message.reply_text(
        "Какой триггер проверить? Пришлите #id или имя из /triggers.\nОтмена: /cancel"
    )
    return CK_WAIT_REF


async def check_trigger_receive_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return CK_WAIT_REF
    return await _check_trigger_show(update, context, update.message.text.strip())


async def _check_trigger_show(
    update: Update, context: ContextTypes.DEFAULT_TYPE, trigger_ref: str
) -> int:
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return ConversationHandler.END
        message, trigger, ok, body = _run_trigger_check(session, actor, trigger_ref)
        await update.message.reply_text(message)
        if trigger is None or ok is None:
            return ConversationHandler.END
        # Результат проверки доставляется как при срабатывании: личный — автору
        # в личку, групповой — в настроенный чат. Если проверяют прямо там,
        # карточка выше уже и есть доставка — не дублируем.
        targets = _trigger_notification_targets(trigger)
        target_chat, target_thread = targets[0] if targets else (None, None)
        if target_chat is not None and update.effective_chat.id != target_chat:
            delivered = await _send_trigger_notification(context.bot, trigger, ok, body)
            if delivered:
                if trigger.trigger_type == "group":
                    where = f"настроенный чат {target_chat}"
                    if target_thread:
                        where += f", топик: {target_thread}"
                else:
                    where = f"личку автора ({target_chat})"
                await update.message.reply_text(
                    f"Результат проверки также отправлен в {where} — как при срабатывании."
                )
    return ConversationHandler.END


async def check_trigger_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Проверка отменена.")
    return ConversationHandler.END


def check_trigger_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("check_trigger", check_trigger_start)],
        states={
            CK_WAIT_REF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_trigger_receive_ref)
            ],
        },
        fallbacks=[CommandHandler("cancel", check_trigger_cancel)],
        allow_reentry=True,
    )


def edit_trigger_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("edit_trigger", edit_trigger_start)],
        states={
            EDIT_TRIGGER_WAIT_REF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_trigger_receive_ref)
            ],
            EDIT_TRIGGER_FIELD_SELECT: [
                CallbackQueryHandler(edit_trigger_field_chosen, pattern=r"^etrig:")
            ],
            EDIT_TRIGGER_WAIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_trigger_receive_value)
            ],
        },
        fallbacks=[CommandHandler("cancel", edit_trigger_cancel)],
        allow_reentry=True,
    )


# --- Пошаговое удаление триггера (/delete_trigger) ---

DT_WAIT_REF, DT_CONFIRM = range(20, 22)


async def delete_trigger_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END
    trigger_ref = " ".join(context.args).strip() if context.args else ""
    if not trigger_ref:
        await update.message.reply_text(
            "Какой триггер удалить? Пришлите #id или имя из /triggers.\nОтмена: /cancel"
        )
        return DT_WAIT_REF
    return await _delete_trigger_show(update, context, trigger_ref)


async def delete_trigger_receive_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return DT_WAIT_REF
    return await _delete_trigger_show(update, context, update.message.text.strip())


async def _delete_trigger_show(
    update: Update, context: ContextTypes.DEFAULT_TYPE, trigger_ref: str
) -> int:
    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        actor = _get_bound_user(session, update.effective_user.id)
        if not actor:
            await update.message.reply_text("Пользователь не найден в БД.")
            return ConversationHandler.END
        trigger = _resolve_trigger_by_id_or_name(session, trigger_ref)
        if not trigger or (
            actor.role.name != "Admin" and trigger.created_by_user_id != actor.id
        ):
            await update.message.reply_text(
                "Триггер не найден или нет прав. Удалить триггер может только автор или Admin."
            )
            return ConversationHandler.END
        context.user_data["delete_trigger_id"] = trigger.id
        card = _format_trigger_card(trigger)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗑 Удалить", callback_data="dtrig:del"),
                InlineKeyboardButton("Отмена", callback_data="dtrig:cancel"),
            ]
        ]
    )
    await update.message.reply_text(
        f"Удалить этот триггер?\n\n{card}", reply_markup=keyboard
    )
    return DT_CONFIRM


async def delete_trigger_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    trigger_id = context.user_data.pop("delete_trigger_id", None)
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    if query.data == "dtrig:cancel" or trigger_id is None:
        await query.edit_message_text("Удаление отменено.")
        return ConversationHandler.END

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        ok, message, deleted_id = _delete_trigger_record(
            session,
            actor_telegram_id=update.effective_user.id,
            # Чистый id без "#": резолвер принимает только числа.
            trigger_ref=str(trigger_id),
        )
    await query.edit_message_text(message)
    if ok and deleted_id is not None:
        _unschedule_trigger_job(context.application, deleted_id)
    return ConversationHandler.END


async def delete_trigger_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("delete_trigger_id", None)
    if update.message:
        await update.message.reply_text("Удаление триггера отменено.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Удаление триггера отменено.")
    return ConversationHandler.END


def delete_trigger_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("delete_trigger", delete_trigger_start)],
        states={
            DT_WAIT_REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_trigger_receive_ref)],
            DT_CONFIRM: [CallbackQueryHandler(delete_trigger_confirm, pattern=r"^dtrig:")],
        },
        fallbacks=[CommandHandler("cancel", delete_trigger_cancel)],
        allow_reentry=True,
    )


def _triggers_commands_help() -> str:
    return (
        "Создать: /create_trigger\n"
        "Изменить: /edit_trigger\n"
        "Проверить: /check_trigger\n"
        "Удалить: /delete_trigger"
    )


def _fetch_visible_triggers(session, actor: User) -> list[Trigger]:
    query = (
        select(Trigger)
        .options(
            joinedload(Trigger.connection),
            joinedload(Trigger.created_by),
        )
        .order_by(Trigger.name)
    )
    rows = session.scalars(query).unique().all()
    if actor.role.name == "Admin":
        return list(rows)

    actor_roles = _user_role_names(actor)
    visible: list[Trigger] = []
    for trigger in rows:
        if trigger.trigger_type == "personal":
            if trigger.created_by_user_id == actor.id:
                visible.append(trigger)
        else:
            # Групповые триггеры видны, если роли создателя и зрителя пересекаются.
            creator_roles = _user_role_names(trigger.created_by) if trigger.created_by else set()
            if creator_roles & actor_roles:
                visible.append(trigger)
    return visible


def _triggers_total_pages(total_triggers: int) -> int:
    return max(1, (total_triggers + TRIGGERS_PER_PAGE - 1) // TRIGGERS_PER_PAGE)


def _format_trigger_card(trigger: Trigger) -> str:
    if trigger.trigger_type == "group":
        topic = f", топик: {trigger.message_thread_id}" if trigger.message_thread_id else ""
        type_line = f"type: group | chat_id: {trigger.chat_id}{topic}"
    else:
        creator = "-"
        if trigger.created_by:
            if trigger.created_by.telegram_username:
                creator = f"@{trigger.created_by.telegram_username}"
            else:
                creator = trigger.created_by.full_name
        type_line = f"type: personal({creator})"
    return "\n".join(
        [
            f"#{trigger.id} {trigger.name}",
            type_line,
            f"connection: {trigger.connection.name} | active: {trigger.is_active}",
            f"schedule: {format_schedule_label(trigger.schedule)}",
            f"message: {trigger.message_template or '- (значение запроса)'}",
        ]
    )


def _build_triggers_page_view(
    trigger_rows: list[Trigger], page: int, role_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    total_pages = _triggers_total_pages(len(trigger_rows))
    page = max(0, min(page, total_pages - 1))
    start = page * TRIGGERS_PER_PAGE
    page_rows = trigger_rows[start : start + TRIGGERS_PER_PAGE]

    header = f"Триггеры ({len(trigger_rows)})"
    if role_name:
        header += f" — роль: {role_name}"
    header += ":"
    if total_pages > 1:
        header += f"\nстр. {page + 1}/{total_pages}"
    header += f"\n\n{_triggers_commands_help()}"

    if page_rows:
        body = "\n\n".join(_format_trigger_card(row) for row in page_rows)
        text = f"{header}\n\n{body}"
    else:
        text = f"{header}\n\nНа этой странице нет триггеров."

    keyboard = _build_triggers_page_keyboard(page_rows, page, total_pages)
    return text, keyboard


def _build_triggers_page_keyboard(
    page_rows: list[Trigger], page: int, total_pages: int
) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    buttons.append(
        [InlineKeyboardButton("Расписание на сутки", callback_data="tsched")]
    )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"tpage:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"tpage:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    return InlineKeyboardMarkup(buttons)


def _build_triggers_day_schedule_text(
    trigger_rows: list[Trigger], *, application: Application
) -> str:
    now = datetime.now(timezone.utc)
    events: list[tuple[datetime, Trigger, str]] = []

    for trigger in trigger_rows:
        if not trigger.is_active or not trigger.schedule:
            continue
        try:
            parsed = parse_schedule(trigger.schedule)
        except ValueError:
            continue
        if not parsed:
            continue

        first_run = None
        job_queue = application.job_queue
        if job_queue:
            jobs = job_queue.get_jobs_by_name(_trigger_job_name(trigger.id))
            if jobs:
                next_ttr = getattr(jobs[0], "next_ttr", None)
                if next_ttr is not None:
                    first_run = next_ttr

        for when in iter_schedule_occurrences(
            trigger.schedule, now=now, first_run=first_run
        ):
            events.append((when, trigger, parsed.display or trigger.schedule))

    if not events:
        return (
            "Расписание на ближайшие 24 часа (местное время):\n\n"
            "Нет активных триггеров с расписанием."
        )

    events.sort(key=lambda item: item[0])
    tz = local_timezone()
    lines = [
        f"{when.astimezone(tz).strftime('%Y-%m-%d %H:%M')}  #{trigger.id} {trigger.name} ({format_schedule_label(label)})"
        for when, trigger, label in events
    ]

    header = (
        f"Расписание на ближайшие 24 часа (местное время)\n"
        f"сейчас: {now.astimezone(tz).strftime('%Y-%m-%d %H:%M')}\n"
        f"запусков: {len(lines)}"
    )
    text = header + "\n\n" + "\n".join(lines)
    if len(text) > 3500:
        kept: list[str] = []
        used = len(header) + 2
        for line in lines:
            if used + len(line) + 1 > 3400:
                remaining = len(lines) - len(kept)
                kept.append(f"... ещё {remaining} запусков")
                break
            kept.append(line)
            used += len(line) + 1
        text = header + "\n\n" + "\n".join(kept)
    return text


def _user_can_use_connection(actor: User, connection: Connection) -> bool:
    if actor.role.name == "Admin":
        return True
    if not connection.is_active:
        return False
    audience = _connection_visible_to_roles(connection)
    if audience is None:
        return True
    return bool(_user_role_names(actor) & set(audience))


def _resolve_trigger_by_id_or_name(session, trigger_ref: str) -> Trigger | None:
    if trigger_ref.isdigit():
        trigger = session.scalar(
            select(Trigger)
            .where(Trigger.id == int(trigger_ref))
            .options(
                joinedload(Trigger.connection),
                joinedload(Trigger.created_by),
            )
        )
        if trigger:
            return trigger
    return session.scalar(
        select(Trigger)
        .where(Trigger.name == trigger_ref)
        .options(
            joinedload(Trigger.connection),
            joinedload(Trigger.created_by),
        )
    )


def _trigger_visible_to_user(session, actor: User, trigger: Trigger) -> bool:
    if actor.role.name == "Admin":
        return True
    if trigger.trigger_type == "personal":
        return trigger.created_by_user_id == actor.id
    # Групповые триггеры видны, если роли создателя и зрителя пересекаются.
    creator_roles = _user_role_names(trigger.created_by) if trigger.created_by else set()
    return bool(creator_roles & _user_role_names(actor))


def _create_trigger_record(
    session,
    actor_telegram_id: int,
    name: str,
    connection_ref: str,
    trigger_type: str,
    chat_id: int | None,
    schedule: str | None,
    sql_query: str,
    message_template: str | None = None,
    message_thread_id: int | None = None,
) -> tuple[bool, str, int | None]:
    actor = session.scalar(
        select(User)
        .where(User.telegram_user_id == actor_telegram_id)
        
    )
    if not actor:
        return False, "Пользователь не найден в БД.", None

    existing = session.scalar(select(Trigger).where(Trigger.name == name))
    if existing:
        return False, f"Триггер с именем '{name}' уже существует.", None

    connection = _resolve_connection_by_id_or_name(session, connection_ref)
    if not connection:
        return False, "Подключение не найдено. Укажите #id или имя из /connections.", None
    if not connection.is_active:
        return False, "Подключение деактивировано.", None

    if not _user_can_use_connection(actor, connection):
        return False, "Нет доступа к этому подключению.", None

    if trigger_type == "group":
        if chat_id is None:
            return False, "Для group-триггера нужно указать chat_id чата.", None
    else:
        chat_id = None

    ok, details, _params = validate_and_execute_row_sql(connection.database_url, sql_query)
    if not ok:
        return False, f"Триггер не создан.\n{details}", None

    trigger = Trigger(
        name=name,
        sql_query=sql_query.strip().rstrip(";"),
        connection_id=connection.id,
        created_by_user_id=actor.id,
        trigger_type=trigger_type,
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        schedule=schedule,
        message_template=message_template,
        is_active=True,
    )
    session.add(trigger)
    session.commit()
    session.refresh(trigger)

    schedule_text = format_schedule_label(schedule)
    return (
        True,
        f"Триггер '{name}' создан (#{trigger.id}).\n"
        f"Тип: {trigger_type}\n"
        f"Подключение: {connection.name}\n"
        f"Расписание: {schedule_text}\n"
        f"Проверочное значение: {_params}",
        trigger.id,
    )


def _run_trigger_check(
    session, actor: User, trigger_ref: str
) -> tuple[str, Trigger | None, bool | None, str]:
    """Проверить триггер → (карточка-ответ, триггер, ok, тело для уведомления).

    ok=None — проверка не выполнялась (нет доступа/деактивирован).
    Тело — отрендеренное сообщение при ok, иначе текст ошибки.
    """
    trigger = _resolve_trigger_by_id_or_name(session, trigger_ref)
    if not trigger:
        return "Триггер не найден.", None, None, ""
    if not _trigger_visible_to_user(session, actor, trigger):
        return "Нет доступа к этому триггеру.", None, None, ""

    card = _format_trigger_card(trigger)
    if not trigger.is_active:
        return f"Триггер деактивирован, проверка не выполнена.\n\n{card}", trigger, None, ""
    if not trigger.connection.is_active:
        return (
            f"Подключение триггера деактивировано, проверка не выполнена.\n\n{card}",
            trigger,
            None,
            "",
        )

    started_at = datetime.now()
    ok, details, params = validate_and_execute_row_sql(
        trigger.connection.database_url, trigger.sql_query
    )
    elapsed_ms = round((datetime.now() - started_at).total_seconds() * 1000)

    sql_preview = trigger.sql_query
    if len(sql_preview) > 500:
        sql_preview = sql_preview[:500] + "…"

    if ok:
        rendered = _render_trigger_message(trigger.message_template, params)
        result_block = f"Результат ({elapsed_ms} мс): {rendered}"
        if len(params) > 1 or trigger.message_template:
            result_block += f"\n(значения: {_format_row_values(params)})"
        body = rendered
    else:
        body = details[:3500]
        result_block = f"Ошибка ({elapsed_ms} мс):\n{body}"

    message = (
        f"Проверка триггера #{trigger.id} «{trigger.name}»\n"
        f"\n{card}"
        f"\n\nSQL:\n{sql_preview}"
        f"\n\n{result_block}"
    )
    return message, trigger, ok, body


def _delete_trigger_record(
    session, actor_telegram_id: int, trigger_ref: str
) -> tuple[bool, str, int | None]:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return False, "Пользователь не найден в БД.", None

    trigger = _resolve_trigger_by_id_or_name(session, trigger_ref)
    if not trigger:
        return False, "Триггер не найден.", None

    if actor.role.name != "Admin" and trigger.created_by_user_id != actor.id:
        return False, "Удалить триггер может только автор или Admin.", None

    trigger_id = trigger.id
    trigger_name = trigger.name
    session.delete(trigger)
    session.commit()
    return True, f"Триггер '{trigger_name}' удалён.", trigger_id


def _trigger_job_name(trigger_id: int) -> str:
    return f"{TRIGGER_JOB_PREFIX}{trigger_id}"


def _unschedule_trigger_job(application: Application, trigger_id: int) -> None:
    job_queue = application.job_queue
    if not job_queue:
        return
    for job in job_queue.get_jobs_by_name(_trigger_job_name(trigger_id)):
        job.schedule_removal()


def _schedule_trigger_job(application: Application, trigger_id: int, schedule: str | None) -> None:
    job_queue = application.job_queue
    if not job_queue:
        return

    _unschedule_trigger_job(application, trigger_id)
    if not schedule:
        return

    try:
        parsed = parse_schedule(schedule)
    except ValueError:
        return
    if not parsed:
        return

    job_name = _trigger_job_name(trigger_id)
    if parsed.kind == "interval" and parsed.seconds:
        job_queue.run_repeating(
            _scheduled_trigger_job,
            interval=parsed.seconds,
            first=parsed.seconds,
            name=job_name,
            data={"trigger_id": trigger_id},
        )
    elif parsed.kind == "daily" and parsed.daily_time:
        job_queue.run_daily(
            _scheduled_trigger_job,
            time=parsed.daily_time,
            name=job_name,
            data={"trigger_id": trigger_id},
        )


def _reschedule_all_triggers(application: Application) -> None:
    job_queue = application.job_queue
    if not job_queue:
        return

    session_factory = application.bot_data.get("session_factory")
    if not session_factory:
        return

    with session_factory() as session:
        triggers_list = session.scalars(
            select(Trigger).where(Trigger.is_active.is_(True), Trigger.schedule.is_not(None))
        ).all()
        for trigger in triggers_list:
            _schedule_trigger_job(application, trigger.id, trigger.schedule)


def _trigger_notification_targets(trigger: Trigger) -> list[tuple[int, int | None]]:
    """Кому доставляется срабатывание/результат: (chat_id, message_thread_id | None)."""
    if trigger.trigger_type == "personal":
        if trigger.created_by is not None and trigger.created_by.telegram_user_id:
            return [(trigger.created_by.telegram_user_id, None)]
        return []
    if trigger.chat_id is not None:
        return [(trigger.chat_id, trigger.message_thread_id)]
    return []


def _trigger_notification_text(trigger: Trigger, ok: bool, body: str) -> str:
    header = f"<b><i>Триггер #{trigger.id} «{html.escape(trigger.name)}»</i></b>"
    if ok:
        return f"{header}\n{html.escape(body)}"
    return f"{header}\nОшибка выполнения:\n{html.escape(body)}"


async def _send_trigger_notification(bot, trigger: Trigger, ok: bool, body: str) -> int:
    """Доставить уведомление как при срабатывании. Возвращает число доставок.

    Вызывать, пока trigger присоединён к сессии (или все нужные атрибуты загружены).
    Топик (message_thread_id) мог не приняться: чат не форум или тема удалена —
    повторяем без него, затем без разметки.
    """
    text_message = _trigger_notification_text(trigger, ok, body)
    delivered = 0
    for chat_id, thread_id in dict.fromkeys(_trigger_notification_targets(trigger)):
        with_topic = {
            "chat_id": chat_id,
            "text": text_message,
            "parse_mode": "HTML",
            "message_thread_id": thread_id,
        } if thread_id else {
            "chat_id": chat_id,
            "text": text_message,
            "parse_mode": "HTML",
        }
        attempts = [
            with_topic,
            {"chat_id": chat_id, "text": text_message, "parse_mode": "HTML"},
            {"chat_id": chat_id, "text": text_message},
        ] if thread_id else [
            with_topic,
            {"chat_id": chat_id, "text": text_message},
        ]
        for kwargs in attempts:
            try:
                await bot.send_message(**kwargs)
                delivered += 1
                break
            except TelegramError:
                continue
    return delivered


async def _scheduled_trigger_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    trigger_id = (context.job.data or {}).get("trigger_id") if context.job else None
    if not trigger_id:
        return

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        trigger = session.scalar(
            select(Trigger)
            .where(Trigger.id == trigger_id)
            .options(
                joinedload(Trigger.connection),
                joinedload(Trigger.created_by),
            )
        )
        if not trigger or not trigger.is_active or not trigger.schedule:
            return
        if not trigger.connection.is_active:
            return

        ok, details, params = validate_and_execute_row_sql(
            trigger.connection.database_url, trigger.sql_query
        )
        body = _render_trigger_message(trigger.message_template, params) if ok else details
        # Отправка внутри сессии: хелперу нужны атрибуты trigger (получатель, топик).
        await _send_trigger_notification(context.bot, trigger, ok, body)


def _check_database_url(database_url: str) -> tuple[bool, str]:
    try:
        engine = create_engine(database_url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _format_connection_check_message(connection_name: str, ok: bool, details: str) -> str:
    if ok:
        return f"Подключение '{connection_name}' успешно проверено."
    error_text = details.strip() or "Неизвестная ошибка подключения."
    return f"Ошибка проверки подключения '{connection_name}':\n{error_text[:3500]}"


async def _sync_user_menu_commands(
    application: Application, chat_id: int, user_id: int, role_name: str
) -> None:
    del user_id
    scoped_commands = _build_bot_commands(
        application.bot_data["command_configs"],
        include_admin=(role_name == "Admin"),
    )
    # Роль показываем прямо в описаниях команд списков, чтобы было видно,
    # что именно выведет команда для этого пользователя.
    role_hint = f"(роль: {role_name})"
    scoped_commands = [
        (
            BotCommand(cmd.command, f"{cmd.description} {role_hint}")
            if cmd.command in ("triggers", "connections", "users", "roles")
            else cmd
        )
        for cmd in scoped_commands
    ]
    # In private chats Chat scope is per-user chat and works reliably.
    chat_scope = BotCommandScopeChat(chat_id=chat_id)
    await application.bot.set_my_commands(scoped_commands, scope=chat_scope)


def _build_bot_commands(command_configs: list[CommandConfig], include_admin: bool) -> list[BotCommand]:
    ordered = sorted(command_configs, key=lambda cmd: (cmd.order, cmd.command))
    return [
        BotCommand(config.command, config.description)
        for config in ordered
        if config.show_in_menu and (include_admin or not config.admin_only)
    ]


async def _post_init_with_web(application: Application) -> None:
    """Стандартный startup бота + запуск веб-интерфейса в том же asyncio-цикле."""
    await on_startup(application)

    web_config: WebConfig | None = application.bot_data.get("web_config")
    if web_config is None or not web_config.enabled:
        return

    from app.web.server import create_web_app, start_web_server

    web_app = create_web_app(
        engine=application.bot_data["engine"],
        config=application.bot_data["config"],
        bot_application=application,
    )
    try:
        server = await start_web_server(web_app, web_config.host, web_config.port)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — падение веба не должно ронять бота
        print(f"[web] Не удалось запустить веб-интерфейс: {exc}")
        return
    application.bot_data["web_server"] = server
    print(f"[web] Веб-интерфейс запущен: {web_config.display_url()}")


async def _post_shutdown_web(application: Application) -> None:
    server = application.bot_data.get("web_server")
    if server is None:
        return
    from app.web.server import stop_web_server

    await stop_web_server(server)


def run_bot(config_path: str = "config.yaml") -> None:
    config: AppConfig = load_config(config_path)
    engine = build_engine(config.database_url)
    Base.metadata.create_all(engine)
    # SQLite не мигрирует схему автоматически: добавляем новые колонки и убираем
    # колонки/таблицы времён групп видимости, если они ещё существуют.
    with engine.begin() as connection:
        for ddl in (
            "ALTER TABLE triggers ADD COLUMN message_template TEXT",
            "ALTER TABLE triggers ADD COLUMN chat_id INTEGER",
            "ALTER TABLE triggers ADD COLUMN message_thread_id INTEGER",
            "ALTER TABLE connections ADD COLUMN allowed_roles VARCHAR(500) NOT NULL DEFAULT ''",
            "ALTER TABLE roles ADD COLUMN description VARCHAR(500) NOT NULL DEFAULT ''",
            "DROP TABLE IF EXISTS user_visibility_groups",
            "DROP TABLE IF EXISTS connection_visibility_groups",
            "DROP TABLE IF EXISTS visibility_groups",
        ):
            try:
                connection.execute(text(ddl))
            except Exception:
                pass
        # Миграция на несколько ролей.
        # 1) users.role_id в старых базах NOT NULL — перестраиваем таблицу с nullable-колонкой.
        notnull = connection.execute(text("PRAGMA table_info(users)")).all()
        role_id_notnull = any(row[1] == "role_id" and row[3] == 1 for row in notnull)
        if role_id_notnull:
            connection.execute(text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                text(
                    "CREATE TABLE users_new ("
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "full_name VARCHAR(255) NOT NULL, "
                    "work_email VARCHAR(255), "
                    "telegram_user_id INTEGER, "
                    "telegram_username VARCHAR(255), "
                    "role_id INTEGER, "
                    "is_active BOOLEAN NOT NULL, "
                    "created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, "
                    "CONSTRAINT uq_users_telegram_id UNIQUE (telegram_user_id))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO users_new (id, full_name, work_email, telegram_user_id, "
                    "telegram_username, role_id, is_active, created_at) "
                    "SELECT id, full_name, work_email, telegram_user_id, telegram_username, "
                    "role_id, is_active, created_at FROM users"
                )
            )
            connection.execute(text("DROP TABLE users"))
            connection.execute(text("ALTER TABLE users_new RENAME TO users"))
            connection.execute(text("PRAGMA foreign_keys=ON"))
        # 2) Перенос первичной роли в user_roles (идемпотентно).
        connection.execute(
            text(
                "INSERT OR IGNORE INTO user_roles (user_id, role_id) "
                "SELECT id, role_id FROM users WHERE role_id IS NOT NULL"
            )
        )
        try:
            connection.execute(text("ALTER TABLE triggers DROP COLUMN visibility_group_id"))
        except Exception:
            pass
    session_factory = build_session_factory(config.database_url)

    # На Python 3.14 ssl.create_default_context() не подхватывает корневые
    # сертификаты Windows — используем CA-бандл из certifi явно.
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    request = HTTPXRequest(httpx_kwargs={"verify": ssl_context})
    get_updates_request = HTTPXRequest(httpx_kwargs={"verify": ssl_context})
    app = (
        Application.builder()
        .token(config.bot_token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(_post_init_with_web)
        .post_shutdown(_post_shutdown_web)
        .build()
    )
    app.bot_data["session_factory"] = session_factory
    app.bot_data["engine"] = engine
    app.bot_data["command_configs"] = config.commands
    app.bot_data["web_config"] = config.web
    app.bot_data["config"] = config

    app.add_handler(TypeHandler(Update, block_inactive_user), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("weblogin", weblogin))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("users", users))
    for conversation in user_status_conversations():
        app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(users_page_callback, pattern=r"^upage:\d+$"))
    app.add_handler(
        CallbackQueryHandler(connection_status_callback, pattern=r"^cpage:\d+$")
    )
    app.add_handler(add_role_conversation())
    app.add_handler(remove_role_conversation())
    app.add_handler(CommandHandler("roles", roles_list))
    for conversation in roles_conversations():
        app.add_handler(conversation)
    app.add_handler(CommandHandler("connections", connections))
    for conversation in connections_conversations():
        app.add_handler(conversation)
    app.add_handler(edit_connection_conversation())
    app.add_handler(CommandHandler("triggers", triggers))
    app.add_handler(
        CallbackQueryHandler(triggers_page_callback, pattern=r"^tpage:\d+$|^tsched$")
    )
    app.add_handler(create_trigger_conversation())
    app.add_handler(edit_trigger_conversation())
    app.add_handler(check_trigger_conversation())
    app.add_handler(delete_trigger_conversation())
    app.add_handler(CommandHandler("help", help_command))

    app.run_polling()
