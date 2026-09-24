"""Запуск только веб-интерфейса, без Telegram-бота: python -m app.web.

Расписания триггеров при этом не тикают — их подхватит бот на следующем старте.
Коды входа выдаёт бот (/weblogin); в этом режиме код можно создать вручную.
"""

from __future__ import annotations

import uvicorn
from sqlalchemy import text

from app.config import load_config
from app.db import build_engine
from app.models import Base
from app.web.server import create_web_app


def main() -> None:
    config = load_config()
    engine = build_engine(config.database_url)
    Base.metadata.create_all(engine)
    # create_all не добавляет колонки в существующие таблицы — повторяем
    # миграции из run_bot, чтобы web-only работал на старой базе.
    with engine.begin() as connection:
        for ddl in ("ALTER TABLE triggers ADD COLUMN message_thread_id INTEGER",):
            try:
                connection.execute(text(ddl))
            except Exception:
                pass
    app = create_web_app(
        engine=engine,
        config=config,
        bot_application=None,
    )
    uvicorn.run(
        app,
        host=config.web.host,
        port=config.web.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
