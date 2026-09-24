from __future__ import annotations

from urllib.parse import unquote, urlparse


def parse_database_url(database_url: str) -> tuple[str, str, str]:
    parsed = urlparse(database_url)

    if parsed.scheme.startswith("sqlite"):
        host = parsed.netloc or "local"
        database = (parsed.path or "").lstrip("/") or "-"
        return host, database, "-"

    user = unquote(parsed.username) if parsed.username else "-"
    host = parsed.hostname or "-"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    database = (parsed.path or "").lstrip("/") or "-"
    return host, database, user
