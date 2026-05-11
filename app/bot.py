from __future__ import annotations

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import load_config
from app.db import build_session_factory
from app.models import Connection, User, VisibilityGroup
from sqlalchemy import create_engine, or_, select, text
from sqlalchemy.orm import joinedload, selectinload

COMMANDS = [
    BotCommand("start", "Регистрация/вход в ChatDB MVP"),
    BotCommand("whoami", "Показать текущего пользователя"),
    BotCommand("groups", "Показать группы (Admin)"),
    BotCommand("connections", "Показать подключения (Admin)"),
    BotCommand("ping", "Проверка, что бот жив"),
    BotCommand("help", "Показать меню команд"),
]


async def on_startup(application: Application) -> None:
    await application.bot.set_my_commands(COMMANDS)


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

        if user:
            user_payload = {
                "full_name": user.full_name,
                "role_name": user.role.name,
            }

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

    await update.message.reply_text(
        "Доступные команды:\n"
        "/start - регистрация/вход\n"
        "/whoami - текущий пользователь\n"
        "/groups - группы видимости (Admin)\n"
        "/connections - список подключений\n"
        "/ping - проверка бота\n"
        "/help - это меню"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    user_payload: dict[str, str | int | bool | list[str]] | None = None
    with session_factory() as session:
        user = session.scalar(
            select(User)
            .options(joinedload(User.role), selectinload(User.visibility_groups))
            .where(User.telegram_user_id == tg_user.id)
        )
        if user:
            user_payload = {
                "id": user.id,
                "full_name": user.full_name,
                "role_name": user.role.name,
                "is_active": user.is_active,
                "groups": sorted(group.name for group in user.visibility_groups),
            }

    if not user_payload:
        await update.message.reply_text("Пользователь не найден в БД.")
        return

    groups = user_payload["groups"]
    groups_text = ", ".join(groups) if groups else "-"
    await update.message.reply_text(
        "id={id}, full_name={full_name}, role={role_name}, active={is_active}, groups={groups}".format(
            **user_payload, groups=groups_text
        )
    )


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


def _parse_group_names(raw_group_names: str) -> list[str]:
    parsed = [name.strip() for name in raw_group_names.split(",")]
    return [name for name in parsed if name]


def _check_database_url(database_url: str) -> tuple[bool, str]:
    try:
        engine = create_engine(database_url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_bot(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    session_factory = build_session_factory(config.database_url)

    app = Application.builder().token(config.bot_token).post_init(on_startup).build()
    app.bot_data["session_factory"] = session_factory

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("groups", groups))
    app.add_handler(CommandHandler("create_group", create_group))
    app.add_handler(CommandHandler("create_connection", create_connection))
    app.add_handler(CommandHandler("connections", connections))
    app.add_handler(CommandHandler("check_connection", check_connection))
    app.add_handler(CommandHandler("help", help_command))

    app.run_polling()
