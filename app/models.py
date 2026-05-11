from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="role")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_users_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    work_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    role: Mapped[Role] = relationship(back_populates="users")
    visibility_groups: Mapped[list["VisibilityGroup"]] = relationship(
        secondary="user_visibility_groups", back_populates="users"
    )


class VisibilityGroup(Base):
    __tablename__ = "visibility_groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[list[User]] = relationship(
        secondary="user_visibility_groups", back_populates="visibility_groups"
    )
    connections: Mapped[list["Connection"]] = relationship(
        secondary="connection_visibility_groups", back_populates="visibility_groups"
    )


class UserVisibilityGroup(Base):
    __tablename__ = "user_visibility_groups"
    __table_args__ = (
        UniqueConstraint("user_id", "visibility_group_id", name="uq_user_visibility_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    visibility_group_id: Mapped[int] = mapped_column(
        ForeignKey("visibility_groups.id"), nullable=False
    )


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    database_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    created_by: Mapped[User] = relationship()
    visibility_groups: Mapped[list[VisibilityGroup]] = relationship(
        secondary="connection_visibility_groups", back_populates="connections"
    )


class ConnectionVisibilityGroup(Base):
    __tablename__ = "connection_visibility_groups"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "visibility_group_id",
            name="uq_connection_visibility_group",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    visibility_group_id: Mapped[int] = mapped_column(
        ForeignKey("visibility_groups.id"), nullable=False
    )
