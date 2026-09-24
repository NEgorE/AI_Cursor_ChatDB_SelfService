from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def build_engine(database_url: str):
    if database_url.startswith("sqlite:///"):
        db_file = database_url.replace("sqlite:///", "", 1)
        db_path = Path(db_file)
        if db_path.parent:
            db_path.parent.mkdir(parents=True, exist_ok=True)

    return create_engine(database_url, future=True)


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = build_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
