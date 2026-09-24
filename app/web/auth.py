"""Авторизация веб-интерфейса: подписанные cookie-сессии и одноразовые коды входа.

Код выдаёт бот командой /weblogin. Telegram остаётся точкой первого входа:
без взаимодействия с ботом попасть в веб нельзя.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import User, WebLoginCode

SESSION_COOKIE = "chatdb_web_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600
LOGIN_CODE_TTL_MINUTES = 5


def make_signer(bot_token: str) -> URLSafeTimedSerializer:
    """Подпись сессий выводится из токена бота: отдельный секрет не нужен."""
    key = hashlib.sha256(("chatdb-web:" + bot_token).encode("utf-8")).hexdigest()
    return URLSafeTimedSerializer(key, salt="chatdb-web-session")


def encode_session(signer: URLSafeTimedSerializer, user_id: int) -> str:
    return signer.dumps({"u": user_id})


def decode_session(signer: URLSafeTimedSerializer, token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = signer.loads(token, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    user_id = data.get("u") if isinstance(data, dict) else None
    return int(user_id) if isinstance(user_id, int) else None


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generate_login_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def issue_login_code(session: Session, user: User) -> str:
    """Выдать новый код входа; прежние неиспользованные коды пользователя гасятся."""
    now = _naive_utc_now()
    session.execute(
        update(WebLoginCode)
        .where(WebLoginCode.user_id == user.id, WebLoginCode.used_at.is_(None))
        .values(used_at=now)
    )
    code = generate_login_code()
    session.add(
        WebLoginCode(
            code=code,
            user_id=user.id,
            expires_at=now + timedelta(minutes=LOGIN_CODE_TTL_MINUTES),
        )
    )
    session.commit()
    return code


def consume_login_code(session: Session, raw_code: str) -> User | None:
    """Погасить код и вернуть пользователя; None — код неверный/просроченный/использован."""
    code = raw_code.strip()
    if not code:
        return None
    row = session.scalar(select(WebLoginCode).where(WebLoginCode.code == code))
    if row is None or row.used_at is not None:
        return None
    if row.expires_at < _naive_utc_now():
        return None
    user = session.get(User, row.user_id)
    if user is None:
        return None
    row.used_at = _naive_utc_now()
    session.commit()
    return user
