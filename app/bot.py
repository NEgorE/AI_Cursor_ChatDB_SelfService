from __future__ import annotations

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from app.config import AppConfig, CommandConfig, load_config
from app.db import build_session_factory
from app.models import Connection, Role, User, UserVisibilityGroup, VisibilityGroup
from sqlalchemy import create_engine, or_, select, text
from sqlalchemy.orm import joinedload, selectinload

USERS_PER_PAGE = 6
BLOCKED_USER_TEXT = "Пользователь заблокирован, обратитесь к администратору."


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    user_payload: dict[str, str] | None = None

    with session_factory() as session:
        user = session.scalar(
            select(User).options(joinedload(User.role)).where(User.telegram_user_id == tg_user.id)
        )
        if not user:
            candidate = _find_candidate_user(session, tg_user.full_name, tg_user.username)
            if candidate:
                candidate.telegram_user_id = tg_user.id
                candidate.telegram_username = tg_user.username
                session.commit()
                user = session.scalar(
                    select(User)
                    .options(joinedload(User.role))
                    .where(User.telegram_user_id == tg_user.id)
                )
            else:
                default_role = _get_or_create_role(session, "DefaultUser")
                default_group = _get_or_create_visibility_group(session, "DefaultUsers")
                new_user = User(
                    full_name=tg_user.full_name,
                    work_email=None,
                    telegram_user_id=tg_user.id,
                    telegram_username=tg_user.username,
                    role_id=default_role.id,
                    is_active=True,
                )
                session.add(new_user)
                session.flush()
                _ensure_user_in_visibility_group(new_user, default_group)
                session.commit()
                user = session.scalar(
                    select(User)
                    .options(joinedload(User.role))
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    with session_factory() as session:
        actor = _get_bound_user(session, tg_user.id) if tg_user else None
        is_admin = bool(actor and actor.role.name == "Admin")

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
            .options(joinedload(User.role))
            .where(User.telegram_user_id == tg_user.id)
        )
        if not user:
            await update.message.reply_text("Пользователь не найден в БД.")
            return

        group_names = session.scalars(
            select(VisibilityGroup.name)
            .join(
                UserVisibilityGroup,
                UserVisibilityGroup.visibility_group_id == VisibilityGroup.id,
            )
            .where(UserVisibilityGroup.user_id == user.id)
            .order_by(VisibilityGroup.name)
        ).all()
        groups_text = ", ".join(group_names) if group_names else "-"
        username_text = f"@{user.telegram_username}" if user.telegram_username else "-"

    await update.message.reply_text(
        f"id={user.id}, full_name={user.full_name}, username={username_text}, role={user.role.name}, "
        f"active={user.is_active}, groups={groups_text}"
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
            await update.message.reply_text("Пользователей в БД пока нет.")
            return

        text, keyboard = _build_users_page_view(user_rows, page=0)
        await update.message.reply_text(text, reply_markup=keyboard)


async def groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        group_rows = session.scalars(
            select(VisibilityGroup)
            .options(selectinload(VisibilityGroup.users))
            .order_by(VisibilityGroup.name)
        ).all()
        if not group_rows:
            await update.message.reply_text(
                "Групп видимости пока нет.\n\n"
                "Создать группу:\n"
                "/create_group <group_name>"
            )
            return

        lines = [
            f"- {group.name} (users: {len(group.users)})"
            for group in group_rows
        ]
        await update.message.reply_text(
            "Группы видимости:\n"
            + "\n".join(lines)
            + "\n\nСоздать группу:\n"
            + "/create_group <group_name>"
        )


async def activate_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_user_status_command(update, context, activate=True)


async def deactivate_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_user_status_command(update, context, activate=False)


async def user_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user or not query.message:
        return

    session_factory = context.application.bot_data["session_factory"]

    if query.data.startswith("upage:"):
        page_raw = query.data.removeprefix("upage:")
        if not page_raw.isdigit():
            await query.answer()
            return
        page = int(page_raw)
        with session_factory() as session:
            actor = _get_bound_user(session, update.effective_user.id)
            if not actor or actor.role.name != "Admin":
                await query.answer("Недостаточно прав.", show_alert=True)
                return
            user_rows = _fetch_all_users(session)
            text, keyboard = _build_users_page_view(user_rows, page=page)
        await query.edit_message_text(text, reply_markup=keyboard)
        await query.answer()
        return

    parsed = _parse_user_action_callback(query.data)
    if not parsed:
        await query.answer()
        return

    activate, target_user_id, page = parsed
    with session_factory() as session:
        ok, message = _set_user_active_status(
            session,
            actor_telegram_id=update.effective_user.id,
            target_user_id=target_user_id,
            activate=activate,
        )
        if not ok:
            await query.answer(message, show_alert=True)
            return

        user_rows = _fetch_all_users(session)
        text, keyboard = _build_users_page_view(user_rows, page=page)

    await query.edit_message_text(f"{message}\n\n{text}", reply_markup=keyboard)
    await query.answer("Готово")


async def create_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    if len(context.args) == 0:
        await update.message.reply_text("Формат: /create_group <group_name>")
        return

    group_name = " ".join(context.args).strip()
    if not group_name:
        await update.message.reply_text("Имя группы не должно быть пустым.")
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

        existing_group = session.scalar(
            select(VisibilityGroup).where(VisibilityGroup.name == group_name)
        )
        if existing_group:
            await update.message.reply_text("Группа с таким именем уже существует.")
            return

        new_group = VisibilityGroup(name=group_name)
        session.add(new_group)
        session.commit()
        await update.message.reply_text(f"Группа '{group_name}' создана.")


async def create_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    if len(context.args) == 0:
        await update.message.reply_text(
            "Чтобы создать подключение, передайте аргументы в формате:\n"
            "/create_connection <name> <database_url> <group1,group2>\n\n"
            "Параметры подключения, которые обычно нужны:\n"
            "- хост\n"
            "- порт\n"
            "- имя базы\n"
            "- логин\n"
            "- пароль\n\n"
            "Пример (PostgreSQL):\n"
            "/create_connection analytics_db "
            "postgresql://app_user:secret123@127.0.0.1:5432/analytics Administrators"
        )
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /create_connection <name> <database_url> <group1,group2>"
        )
        return

    connection_name = context.args[0].strip()
    database_url = context.args[1].strip()
    group_names = _parse_group_names(context.args[2])
    if not connection_name or not database_url or not group_names:
        await update.message.reply_text(
            "Нужно передать непустые name, database_url и минимум одну группу."
        )
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

        existing_connection = session.scalar(
            select(Connection).where(Connection.name == connection_name)
        )
        if existing_connection:
            await update.message.reply_text("Подключение с таким именем уже существует.")
            return

        visibility_groups = session.scalars(
            select(VisibilityGroup).where(VisibilityGroup.name.in_(group_names))
        ).all()
        found_group_names = {group.name for group in visibility_groups}
        missing_groups = sorted(set(group_names) - found_group_names)
        if missing_groups:
            await update.message.reply_text(
                "Не найдены группы: " + ", ".join(missing_groups)
            )
            return

        connection = Connection(
            name=connection_name,
            database_url=database_url,
            created_by_user_id=actor.id,
            is_active=True,
        )
        connection.visibility_groups.extend(visibility_groups)
        session.add(connection)
        session.commit()
        await update.message.reply_text(
            f"Подключение '{connection_name}' создано. Группы: {', '.join(sorted(found_group_names))}."
        )


async def check_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    if len(context.args) != 1:
        await update.message.reply_text("Формат: /check_connection <name>")
        return

    connection_name = context.args[0].strip()
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

        connection = session.scalar(select(Connection).where(Connection.name == connection_name))
        if not connection:
            await update.message.reply_text("Подключение не найдено.")
            return

    ok, details = _check_database_url(connection.database_url)
    if ok:
        await update.message.reply_text(f"Подключение '{connection_name}' успешно проверено.")
        return
    await update.message.reply_text(f"Ошибка проверки '{connection_name}': {details}")


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
        if actor.role.name != "Admin":
            await update.message.reply_text("Недостаточно прав. Команда доступна только Admin.")
            return

        connection_rows = session.scalars(
            select(Connection).options(selectinload(Connection.visibility_groups)).order_by(Connection.name)
        ).all()
        if not connection_rows:
            await update.message.reply_text(
                "Подключений пока нет.\n\n"
                "Создать новое:\n"
                "/create_connection"
            )
            return

        blocks: list[str] = []
        for connection in connection_rows:
            groups = ", ".join(sorted(group.name for group in connection.visibility_groups)) or "-"
            blocks.append(
                "\n".join(
                    [
                        f"Подключение: {connection.name}",
                        f"Группы: {groups}",
                        f"/check_connection {connection.name}",
                    ]
                )
            )

        footer = (
            "\n\nСоздать новое подключение:\n"
            "/create_connection"
        )
        await update.message.reply_text("\n\n".join(blocks) + footer)


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

    return session.scalar(select(User).options(joinedload(User.role)).where(or_(*filters)))


def _get_bound_user(session, telegram_user_id: int) -> User | None:
    return session.scalar(
        select(User).options(joinedload(User.role)).where(User.telegram_user_id == telegram_user_id)
    )


def _fetch_all_users(session) -> list[User]:
    return session.scalars(
        select(User)
        .options(joinedload(User.role), selectinload(User.visibility_groups))
        .order_by(User.id)
    ).all()


def _users_total_pages(total_users: int) -> int:
    return max(1, (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE)


def _format_user_card(row: User) -> str:
    groups_text = ", ".join(sorted(g.name for g in row.visibility_groups)) or "-"
    tg = str(row.telegram_user_id) if row.telegram_user_id is not None else "-"
    un = f"@{row.telegram_username}" if row.telegram_username else "-"
    return "\n".join(
        [
            f"#{row.id} {row.full_name}",
            f"role: {row.role.name} | active: {row.is_active}",
            f"telegram: {un} (id: {tg})",
            f"groups: {groups_text}",
        ]
    )


def _build_users_page_view(user_rows: list[User], page: int) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = _users_total_pages(len(user_rows))
    page = max(0, min(page, total_pages - 1))
    start = page * USERS_PER_PAGE
    page_rows = user_rows[start : start + USERS_PER_PAGE]

    header = f"Пользователи ({len(user_rows)}):"
    if total_pages > 1:
        header += f"\nстр. {page + 1}/{total_pages}"

    if page_rows:
        body = "\n\n".join(_format_user_card(row) for row in page_rows)
        text = f"{header}\n\n{body}"
    else:
        text = f"{header}\n\nНа этой странице нет пользователей."

    keyboard = _build_users_page_keyboard(page_rows, page, total_pages)
    return text, keyboard


def _build_users_page_keyboard(
    page_rows: list[User], page: int, total_pages: int
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row_buttons: list[InlineKeyboardButton] = []

    for user in page_rows:
        if user.is_active:
            label = f"Деактив. #{user.id}"
            callback_data = f"udeact:{user.id}:{page}"
        else:
            label = f"Актив. #{user.id}"
            callback_data = f"uact:{user.id}:{page}"
        row_buttons.append(InlineKeyboardButton(label, callback_data=callback_data))
        if len(row_buttons) == 2:
            buttons.append(row_buttons)
            row_buttons = []

    if row_buttons:
        buttons.append(row_buttons)

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"upage:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"upage:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(buttons)


def _parse_user_action_callback(data: str) -> tuple[bool, int, int] | None:
    if data.startswith("uact:"):
        activate = True
        payload = data.removeprefix("uact:")
    elif data.startswith("udeact:"):
        activate = False
        payload = data.removeprefix("udeact:")
    else:
        return None

    parts = payload.split(":")
    if not parts or not parts[0].isdigit():
        return None

    target_user_id = int(parts[0])
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return activate, target_user_id, page


async def _handle_user_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, activate: bool
) -> None:
    if not update.effective_user or not update.message:
        return

    user_id = _extract_user_id_from_command(context.args, update.message.text, activate)
    if user_id is None:
        command_name = "activate_user" if activate else "deactivate_user"
        await update.message.reply_text(f"Формат: /{command_name} <user_id>")
        return

    session_factory = context.application.bot_data["session_factory"]
    with session_factory() as session:
        ok, message = _set_user_active_status(
            session,
            actor_telegram_id=update.effective_user.id,
            target_user_id=user_id,
            activate=activate,
        )
    await update.message.reply_text(message)


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


def _set_user_active_status(
    session, actor_telegram_id: int, target_user_id: int, activate: bool
) -> tuple[bool, str]:
    actor = _get_bound_user(session, actor_telegram_id)
    if not actor:
        return False, "Пользователь не найден в БД."
    if actor.role.name != "Admin":
        return False, "Недостаточно прав. Команда доступна только Admin."

    target_user = session.scalar(select(User).where(User.id == target_user_id))
    if not target_user:
        return False, "Целевой пользователь не найден."

    if activate:
        if target_user.is_active:
            return False, "Пользователь уже активен."
        target_user.is_active = True
        session.commit()
        return True, f"Пользователь '{target_user.full_name}' (id={target_user.id}) активирован."

    if not target_user.is_active:
        return False, "Пользователь уже деактивирован."
    target_user.is_active = False
    session.commit()
    return True, f"Пользователь '{target_user.full_name}' (id={target_user.id}) деактивирован."


def _parse_group_names(raw_group_names: str) -> list[str]:
    parsed = [name.strip() for name in raw_group_names.split(",")]
    return [name for name in parsed if name]


def _get_or_create_role(session, role_name: str) -> Role:
    role = session.scalar(select(Role).where(Role.name == role_name))
    if role:
        return role
    role = Role(name=role_name)
    session.add(role)
    session.flush()
    return role


def _get_or_create_visibility_group(session, group_name: str) -> VisibilityGroup:
    group = session.scalar(select(VisibilityGroup).where(VisibilityGroup.name == group_name))
    if group:
        return group
    group = VisibilityGroup(name=group_name)
    session.add(group)
    session.flush()
    return group


def _ensure_user_in_visibility_group(user: User, group: VisibilityGroup) -> None:
    if any(existing_group.id == group.id for existing_group in user.visibility_groups):
        return
    user.visibility_groups.append(group)


def _check_database_url(database_url: str) -> tuple[bool, str]:
    try:
        engine = create_engine(database_url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def _sync_user_menu_commands(
    application: Application, chat_id: int, user_id: int, role_name: str
) -> None:
    del user_id
    scoped_commands = _build_bot_commands(
        application.bot_data["command_configs"],
        include_admin=(role_name == "Admin"),
    )
    # In private chats Chat scope is per-user chat and works reliably.
    chat_scope = BotCommandScopeChat(chat_id=chat_id)
    await application.bot.set_my_commands(scoped_commands, scope=chat_scope)


def _build_bot_commands(command_configs: list[CommandConfig], include_admin: bool) -> list[BotCommand]:
    ordered = sorted(command_configs, key=lambda cmd: (cmd.order, cmd.command))
    return [
        BotCommand(config.command, config.description)
        for config in ordered
        if include_admin or not config.admin_only
    ]


def run_bot(config_path: str = "config.yaml") -> None:
    config: AppConfig = load_config(config_path)
    session_factory = build_session_factory(config.database_url)

    app = Application.builder().token(config.bot_token).post_init(on_startup).build()
    app.bot_data["session_factory"] = session_factory
    app.bot_data["command_configs"] = config.commands

    app.add_handler(TypeHandler(Update, block_inactive_user), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("users", users))
    app.add_handler(CommandHandler("activate_user", activate_user))
    app.add_handler(CommandHandler("deactivate_user", deactivate_user))
    app.add_handler(
        CallbackQueryHandler(
            user_status_callback,
            pattern=r"^(uact|udeact):\d+(:\d+)?$|^upage:\d+$",
        )
    )
    app.add_handler(CommandHandler("groups", groups))
    app.add_handler(CommandHandler("create_group", create_group))
    app.add_handler(CommandHandler("create_connection", create_connection))
    app.add_handler(CommandHandler("connections", connections))
    app.add_handler(CommandHandler("check_connection", check_connection))
    app.add_handler(CommandHandler("help", help_command))

    app.run_polling()
