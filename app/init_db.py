from __future__ import annotations

from app.config import load_config
from app.db import build_engine
from app.models import Base, Role, User
from sqlalchemy import select
from sqlalchemy.orm import Session


def init_db(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    engine = build_engine(config.database_url)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        admin_role = _get_or_create_role(session, "Admin")
        _get_or_create_role(session, "DefaultUser")
        _get_or_create_superuser(session, config.initial_superuser_name, admin_role.id)
        session.commit()


def _get_or_create_role(session: Session, role_name: str) -> Role:
    role = session.scalar(select(Role).where(Role.name == role_name))
    if role:
        return role

    role = Role(name=role_name)
    session.add(role)
    session.flush()
    return role


def _get_or_create_superuser(session: Session, initial_name: str, admin_role_id: int) -> User:
    admin_role = session.get(Role, admin_role_id)
    user = session.scalar(select(User).where(User.full_name == initial_name))
    if user:
        if initial_name.startswith("@"):
            username = initial_name[1:]
            if username and not user.telegram_username:
                user.telegram_username = username
        if admin_role and not any(r.id == admin_role.id for r in user.roles):
            user.roles.append(admin_role)
        return user

    telegram_username = initial_name[1:] if initial_name.startswith("@") else None
    user = User(
        full_name=initial_name,
        work_email=None,
        telegram_username=telegram_username,
        roles=[admin_role] if admin_role else [],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


if __name__ == "__main__":
    init_db()
    print("Database initialized.")
