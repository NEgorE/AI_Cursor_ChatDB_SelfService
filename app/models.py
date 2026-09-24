from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_users_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    work_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Историческая колонка первичной роли; актуальные роли — в user_roles.
    # SQLite не даёт удалить колонку с FK — оставлена, ORM её не читает.
    role_id: Mapped[int | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    roles: Mapped[list["Role"]] = relationship(
        secondary="user_roles", lazy="selectin", order_by="Role.id"
    )

    @property
    def role(self) -> Role | None:
        """Основная роль (для сообщений и проверок): Admin > DefaultUser > прочие."""
        by_name = {r.name: r for r in self.roles}
        if "Admin" in by_name:
            return by_name["Admin"]
        if "DefaultUser" in by_name:
            return by_name["DefaultUser"]
        return self.roles[0] if self.roles else None


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    database_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Пусто — подключение видят все; иначе список ролей через запятую.
    allowed_roles: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    created_by: Mapped[User] = relationship()
    triggers: Mapped[list["Trigger"]] = relationship(back_populates="connection")


class Trigger(Base):
    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    sql_query: Mapped[str] = mapped_column(Text, nullable=False)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)  # personal | group
    # chat_id чата Telegram для group-триггера; у personal всегда NULL.
    chat_id: Mapped[int | None] = mapped_column(nullable=True)
    # Топик (ветка) внутри форума-чата, если указан.
    message_thread_id: Mapped[int | None] = mapped_column(nullable=True)
    schedule: Mapped[str | None] = mapped_column(String(50), nullable=True)
    message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    connection: Mapped[Connection] = relationship(back_populates="triggers")
    created_by: Mapped[User] = relationship()


class WebLoginCode(Base):
    """Одноразовый код входа в веб-интерфейс (выдаёт бот командой /weblogin).

    Время хранится наивным UTC (SQLite отбрасывает tzinfo).
    """

    __tablename__ = "web_login_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
