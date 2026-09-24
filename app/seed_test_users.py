from __future__ import annotations

from app.config import load_config
from app.db import build_engine
from app.init_db import _get_or_create_role, init_db
from app.models import Base, User
from sqlalchemy import select
from sqlalchemy.orm import Session

TEST_USERS: list[dict[str, object]] = [
    {
        "full_name": "Тестовый Пользователь 01",
        "telegram_username": "test_user_01",
        "telegram_user_id": 900_000_001,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 02",
        "telegram_username": "test_user_02",
        "telegram_user_id": 900_000_002,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 03",
        "telegram_username": "test_user_03",
        "telegram_user_id": 900_000_003,
        "is_active": False,
    },
    {
        "full_name": "Тестовый Пользователь 04",
        "telegram_username": "test_user_04",
        "telegram_user_id": 900_000_004,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 05",
        "telegram_username": "test_user_05",
        "telegram_user_id": 900_000_005,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 06",
        "telegram_username": "test_user_06",
        "telegram_user_id": 900_000_006,
        "is_active": False,
    },
    {
        "full_name": "Тестовый Пользователь 07",
        "telegram_username": "test_user_07",
        "telegram_user_id": 900_000_007,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 08",
        "telegram_username": "test_user_08",
        "telegram_user_id": 900_000_008,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 09",
        "telegram_username": "test_user_09",
        "telegram_user_id": 900_000_009,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 10",
        "telegram_username": "test_user_10",
        "telegram_user_id": 900_000_010,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 11",
        "telegram_username": "test_user_11",
        "telegram_user_id": 900_000_011,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 12",
        "telegram_username": "test_user_12",
        "telegram_user_id": 900_000_012,
        "is_active": False,
    },
    {
        "full_name": "Тестовый Пользователь 13",
        "telegram_username": "test_user_13",
        "telegram_user_id": 900_000_013,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 14",
        "telegram_username": "test_user_14",
        "telegram_user_id": 900_000_014,
        "is_active": True,
    },
    {
        "full_name": "Тестовый Пользователь 15",
        "telegram_username": "test_user_15",
        "telegram_user_id": 900_000_015,
        "is_active": True,
    },
]


def seed_test_users(config_path: str = "config.yaml") -> int:
    init_db(config_path)
    config = load_config(config_path)
    engine = build_engine(config.database_url)
    Base.metadata.create_all(engine)

    created = 0
    with Session(engine) as session:
        default_role = _get_or_create_role(session, "DefaultUser")

        for payload in TEST_USERS:
            full_name = str(payload["full_name"])
            existing = session.scalar(select(User).where(User.full_name == full_name))
            if existing:
                continue

            user = User(
                full_name=full_name,
                work_email=f"{payload['telegram_username']}@example.test",
                telegram_user_id=int(payload["telegram_user_id"]),  # type: ignore[arg-type]
                telegram_username=str(payload["telegram_username"]),
                roles=[default_role],
                is_active=bool(payload["is_active"]),
            )
            session.add(user)
            created += 1

        session.commit()

    return created


if __name__ == "__main__":
    added = seed_test_users()
    print(f"Test users added: {added} (skipped if already exist).")
