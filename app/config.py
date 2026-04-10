from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    initial_superuser_name: str
    database_url: str


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

    return AppConfig(
        bot_token=bot_token,
        initial_superuser_name=initial_superuser_name,
        database_url=database_url,
    )
