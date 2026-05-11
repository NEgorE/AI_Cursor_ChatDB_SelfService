from __future__ import annotations

from app.config import load_config
from app.db import build_engine
from app.models import Base, Role, User, VisibilityGroup
from sqlalchemy import select
from sqlalchemy.orm import Session


def init_db(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    engine = build_engine(config.database_url)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        admin_role = _get_or_create_role(session, "Admin")
        _get_or_create_role(session, "DefaultUser")
        administrators_group = _get_or_create_visibility_group(session, "Administrators")
        superuser = _get_or_create_superuser(session, config.initial_superuser_name, admin_role.id)
        _ensure_user_in_visibility_group(superuser, administrators_group)
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
    user = session.scalar(select(User).where(User.full_name == initial_name))
    if user:
        if initial_name.startswith("@"):
            username = initial_name[1:]
            if username and not user.telegram_username:
                user.telegram_username = username
        return user

    telegram_username = initial_name[1:] if initial_name.startswith("@") else None
    user = User(
        full_name=initial_name,
        work_email=None,
        telegram_username=telegram_username,
        role_id=admin_role_id,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _get_or_create_visibility_group(session: Session, group_name: str) -> VisibilityGroup:
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


if __name__ == "__main__":
    init_db()
    print("Database initialized.")
