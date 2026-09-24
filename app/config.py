from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CommandConfig:
    command: str
    description: str
    order: int
    admin_only: bool = False
    show_in_menu: bool = True


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    # Ссылка, которую бот присылает вместе с кодом входа.
    # Пусто — строится из host/port.
    public_url: str = ""

    def display_url(self) -> str:
        if self.public_url.strip():
            return self.public_url.strip().rstrip("/")
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    initial_superuser_name: str
    database_url: str
    commands: list[CommandConfig]
    web: WebConfig = WebConfig()


DEFAULT_COMMANDS: list[CommandConfig] = [
    CommandConfig(command="start", description="Регистрация/вход в ChatDB MVP", order=10),
    CommandConfig(command="whoami", description="Показать текущего пользователя", order=20),
    CommandConfig(command="users", description="Показать пользователей (Admin)", order=30, admin_only=True),
    CommandConfig(command="groups", description="Показать группы (Admin)", order=40, admin_only=True),
    CommandConfig(command="connections", description="Показать подключения (Admin)", order=50, admin_only=True),
    CommandConfig(command="triggers", description="Показать триггеры", order=55),
    CommandConfig(command="ping", description="Проверка, что бот жив", order=60),
    CommandConfig(command="help", description="Показать меню команд", order=70),
]


def load_config(path: str = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file '{config_path}' not found. Create it from config.example.yaml."
        )

    with config_path.open("r", encoding="utf-8") as file:
        raw: dict[str, Any] = yaml.safe_load(file) or {}

    bot_token = str(raw.get("bot_token", "")).strip()
    initial_superuser_name = str(raw.get("initial_superuser_name", "")).strip()
    database_url = str(raw.get("database_url", "sqlite:///data/chatdb.sqlite3")).strip()

    if not bot_token:
        raise ValueError("config.bot_token is required.")
    if not initial_superuser_name:
        raise ValueError("config.initial_superuser_name is required.")
    if not database_url:
        raise ValueError("config.database_url is required.")
    commands = _parse_commands(raw.get("commands"))
    web = _parse_web(raw.get("web"))

    return AppConfig(
        bot_token=bot_token,
        initial_superuser_name=initial_superuser_name,
        database_url=database_url,
        commands=commands,
        web=web,
    )


def _parse_web(raw: Any) -> WebConfig:
    if not isinstance(raw, dict):
        return WebConfig()
    try:
        port = int(raw.get("port", 8000))
    except (TypeError, ValueError):
        port = 8000
    return WebConfig(
        enabled=bool(raw.get("enabled", True)),
        host=str(raw.get("host", "127.0.0.1")).strip() or "127.0.0.1",
        port=port,
        public_url=str(raw.get("public_url", "")).strip(),
    )


def _parse_commands(raw_commands: Any) -> list[CommandConfig]:
    if not isinstance(raw_commands, list):
        return list(DEFAULT_COMMANDS)

    parsed: list[CommandConfig] = []
    for item in raw_commands:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command", "")).strip().lstrip("/")
        description = str(item.get("description", "")).strip()
        if not command or not description:
            continue
        try:
            order = int(item.get("order", 0))
        except (TypeError, ValueError):
            continue
        admin_only = bool(item.get("admin_only", False))
        show_in_menu = bool(item.get("show_in_menu", True))
        parsed.append(
            CommandConfig(
                command=command,
                description=description,
                order=order,
                admin_only=admin_only,
                show_in_menu=show_in_menu,
            )
        )

    if not parsed:
        return list(DEFAULT_COMMANDS)
    return sorted(parsed, key=lambda cmd: (cmd.order, cmd.command))
