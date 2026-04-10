from __future__ import annotations

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import load_config
from app.db import build_session_factory
from app.models import User
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

COMMANDS = [
    BotCommand("start", "Регистрация/вход в ChatDB MVP"),
    BotCommand("whoami", "Показать текущего пользователя"),
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
        "/ping - проверка бота\n"
        "/help - это меню"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    session_factory = context.application.bot_data["session_factory"]
    tg_user = update.effective_user
    user_payload: dict[str, str | int | bool] | None = None
    with session_factory() as session:
        user = session.scalar(
            select(User).options(joinedload(User.role)).where(User.telegram_user_id == tg_user.id)
        )
        if user:
            user_payload = {
                "id": user.id,
                "full_name": user.full_name,
                "role_name": user.role.name,
                "is_active": user.is_active,
            }

    if not user_payload:
        await update.message.reply_text("Пользователь не найден в БД.")
        return

    await update.message.reply_text(
        "id={id}, full_name={full_name}, role={role_name}, active={is_active}".format(
            **user_payload
        )
    )


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


def run_bot(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    session_factory = build_session_factory(config.database_url)

    app = Application.builder().token(config.bot_token).post_init(on_startup).build()
    app.bot_data["session_factory"] = session_factory

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("help", help_command))

    app.run_polling()
