from __future__ import annotations

from app.config import load_config
from app.db import build_engine
from app.init_db import init_db
from app.models import Base, Connection, Role, User
from sqlalchemy import select
from sqlalchemy.orm import Session

TEST_CONNECTIONS: list[dict[str, object]] = [
    {
        "name": "test_conn_01",
        "database_url": "postgresql://dbuser01:secret@10.0.0.1:5432/analytics_01",
        "allowed_roles": "",
        "is_active": True,
    },
    {
        "name": "test_conn_02",
        "database_url": "postgresql://dbuser02:secret@10.0.0.2:5432/analytics_02",
        "allowed_roles": "",
        "is_active": True,
    },
    {
        "name": "test_conn_03",
        "database_url": "postgresql://dbuser03:secret@10.0.0.3:5432/analytics_03",
        "allowed_roles": "",
        "is_active": False,
    },
    {
        "name": "test_conn_04",
        "database_url": "postgresql://dbuser04:secret@10.0.0.4:5432/analytics_04",
        "allowed_roles": "Admin",
        "is_active": True,
    },
    {
        "name": "test_conn_05",
        "database_url": "postgresql://dbuser05:secret@10.0.0.5:5432/analytics_05",
        "allowed_roles": "DefaultUser, Admin",
        "is_active": True,
    },
    {
        "name": "test_conn_06",
        "database_url": "postgresql://dbuser06:secret@10.0.0.6:5432/analytics_06",
        "allowed_roles": "",
        "is_active": False,
    },
    {
        "name": "test_conn_07",
        "database_url": "postgresql://dbuser07:secret@10.0.0.7:5432/analytics_07",
        "allowed_roles": "",
        "is_active": True,
    },
    {
        "name": "test_conn_08",
        "database_url": "postgresql://dbuser08:secret@10.0.0.8:5432/analytics_08",
        "allowed_roles": "",
        "is_active": True,
    },
    {
        "name": "test_conn_09",
        "database_url": "sqlite:///data/test_conn_09.db",
        "allowed_roles": "",
        "is_active": True,
    },
    {
        "name": "test_conn_10",
        "database_url": "postgresql://dbuser10:secret@10.0.0.10:5432/analytics_10",
        "allowed_roles": "Admin",
        "is_active": True,
    },
    {
        "name": "test_conn_11",
        "database_url": "postgresql://dbuser11:secret@10.0.0.11:5432/analytics_11",
        "allowed_roles": "",
        "is_active": True,
    },
    {
        "name": "test_conn_12",
        "database_url": "postgresql://dbuser12:secret@10.0.0.12:5432/analytics_12",
        "allowed_roles": "",
        "is_active": False,
    },
    {
        "name": "test_conn_13",
        "database_url": "postgresql://dbuser13:secret@10.0.0.13:5432/analytics_13",
        "allowed_roles": "DefaultUser, Admin",
        "is_active": True,
    },
    {
        "name": "test_conn_14",
        "database_url": "postgresql://dbuser14:secret@10.0.0.14:5432/analytics_14",
        "allowed_roles": "",
        "is_active": True,
    },
    {
        "name": "test_conn_15",
        "database_url": "postgresql://dbuser15:secret@10.0.0.15:5432/analytics_15",
        "allowed_roles": "",
        "is_active": True,
    },
]


def seed_test_connections(config_path: str = "config.yaml") -> int:
    init_db(config_path)
    config = load_config(config_path)
    engine = build_engine(config.database_url)
    Base.metadata.create_all(engine)

    created = 0
    with Session(engine) as session:
        admin_user = session.scalar(
            select(User).join(Role, User.role_id == Role.id).where(Role.name == "Admin")
        )
        if not admin_user:
            raise RuntimeError("Admin user not found. Run init_db first.")

        for payload in TEST_CONNECTIONS:
            name = str(payload["name"])
            existing = session.scalar(select(Connection).where(Connection.name == name))
            if existing:
                continue

            connection = Connection(
                name=name,
                database_url=str(payload["database_url"]),
                created_by_user_id=admin_user.id,
                allowed_roles=str(payload["allowed_roles"]),
                is_active=bool(payload["is_active"]),
            )
            session.add(connection)
            created += 1

        session.commit()

    return created


if __name__ == "__main__":
    added = seed_test_connections()
    print(f"Test connections added: {added} (skipped if already exist).")
