from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone

from sqlalchemy import create_engine, text


SCHEDULE_NONE_TOKENS = frozenset({"-", "none", "manual", ""})

# Приватные супергруппы/каналы: https://t.me/c/<внутренний_id>/<сообщение>
# (внутренний id + префикс -100 = chat_id). Публичные ссылки t.me/<username>
# не конвертируются без запроса к Telegram и не принимаются.
TELEGRAM_CHAT_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?(?:/\d+)*/?(?:\?.*)?", re.IGNORECASE
)


def parse_chat_ref(raw: str | None) -> tuple[int, int | None] | None:
    """Распознать чат: число или ссылка t.me.

    https://t.me/c/<внутренний_id>/<топик>[/<сообщение>] — второй сегмент после
    id чата считается топиком (веткой форума); возвращается
    (chat_id=-100<id>, message_thread_id=топик|None).
    Число — это chat_id без топика. None — распознать не удалось.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value), None
    # Формат "chat_id:топик" (так поле предзаполняется в веб-форме).
    colon = re.fullmatch(r"(-?\d+):(\d+)", value)
    if colon:
        return int(colon.group(1)), int(colon.group(2))
    match = TELEGRAM_CHAT_LINK_RE.fullmatch(value)
    if match:
        chat_id = int(f"-100{match.group(1)}")
        thread = int(match.group(2)) if match.group(2) else None
        return chat_id, thread
    return None


@dataclass(frozen=True)
class ParsedSchedule:
    kind: str  # interval | daily
    seconds: int | None = None
    daily_time: time | None = None
    display: str = ""


def normalize_schedule(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in SCHEDULE_NONE_TOKENS:
        return None
    return value


def parse_schedule(raw: str | None) -> ParsedSchedule | None:
    value = normalize_schedule(raw)
    if value is None:
        return None

    every_match = re.fullmatch(r"every:(\d+)([mh])", value)
    if every_match:
        amount = int(every_match.group(1))
        unit = every_match.group(2)
        if amount < 1:
            raise ValueError("Интервал расписания должен быть >= 1.")
        seconds = amount * 60 if unit == "m" else amount * 3600
        if seconds > 7 * 24 * 3600:
            raise ValueError("Интервал расписания слишком большой.")
        return ParsedSchedule(kind="interval", seconds=seconds, display=f"every:{amount}{unit}")

    daily_match = re.fullmatch(r"daily:(\d{1,2}):(\d{2})", value)
    if daily_match:
        hour = int(daily_match.group(1))
        minute = int(daily_match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("Некорректное время daily:HH:MM.")
        return ParsedSchedule(
            kind="daily",
            daily_time=time(hour=hour, minute=minute, tzinfo=timezone.utc),
            display=f"daily:{hour:02d}:{minute:02d}",
        )

    raise ValueError(
        "Некорректное расписание. Используйте `-` (без расписания), "
        "`every:5m`, `every:1h` или `daily:09:00`."
    )


def iter_schedule_occurrences(
    schedule: str | None,
    *,
    now: datetime | None = None,
    window: timedelta | None = None,
    first_run: datetime | None = None,
) -> list[datetime]:
    """Return fire times within the next window (default 24h, UTC)."""
    parsed = parse_schedule(schedule)
    if not parsed:
        return []

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    if window is None:
        window = timedelta(hours=24)
    end = now + window

    if first_run is not None:
        if first_run.tzinfo is None:
            first_run = first_run.replace(tzinfo=timezone.utc)
        else:
            first_run = first_run.astimezone(timezone.utc)

    runs: list[datetime] = []
    if parsed.kind == "interval" and parsed.seconds:
        interval = timedelta(seconds=parsed.seconds)
        if first_run is not None and now < first_run < end:
            cursor = first_run
        elif first_run is not None and first_run <= now:
            elapsed = (now - first_run).total_seconds()
            steps = int(elapsed // parsed.seconds) + 1
            cursor = first_run + timedelta(seconds=steps * parsed.seconds)
        else:
            cursor = now + interval
        while cursor < end:
            if cursor > now:
                runs.append(cursor)
            cursor += interval
        return runs

    if parsed.kind == "daily" and parsed.daily_time:
        if first_run is not None and now < first_run < end:
            return [first_run]
        candidate = datetime.combine(now.date(), parsed.daily_time)
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        if candidate <= now:
            candidate += timedelta(days=1)
        if candidate < end:
            runs.append(candidate)
        return runs

    return []


def validate_and_execute_scalar_sql(database_url: str, sql_query: str) -> tuple[bool, str, object | None]:
    query = sql_query.strip().rstrip(";")
    if not query:
        return False, "SQL-запрос не должен быть пустым.", None

    upper = query.lstrip().upper()
    if not upper.startswith("SELECT"):
        return False, "Триггер может содержать только SELECT-запрос.", None

    try:
        engine = create_engine(database_url, future=True)
        with engine.connect() as conn:
            result = conn.execute(text(query))
            rows = result.fetchmany(2)
        engine.dispose()
    except Exception as exc:
        return False, f"Ошибка выполнения SQL: {exc}", None

    if len(rows) == 0:
        return False, "Запрос не вернул строк. Нужна ровно 1 строка и 1 столбец.", None
    if len(rows) > 1:
        return False, "Запрос вернул больше 1 строки. Нужна ровно 1 строка и 1 столбец.", None
    if len(rows[0]) != 1:
        return False, "Запрос вернул больше 1 столбца. Нужна ровно 1 строка и 1 столбец.", None

    return True, "ok", rows[0][0]


def validate_and_execute_row_sql(
    database_url: str, sql_query: str
) -> tuple[bool, str, dict[str, object]]:
    """Выполнить SELECT, который должен вернуть ровно 1 строку.

    Столбцы именуются по номерам параметров ($1, $2, ...). Одиночный
    безымянный столбец считается параметром $1. Возвращает словарь
    {номер параметра: значение}.
    """
    query = sql_query.strip().rstrip(";")
    if not query:
        return False, "SQL-запрос не должен быть пустым.", {}

    upper = query.lstrip().upper()
    if not upper.startswith("SELECT"):
        return False, "Триггер может содержать только SELECT-запрос.", {}

    try:
        engine = create_engine(database_url, future=True)
        with engine.connect() as conn:
            result = conn.execute(text(query))
            rows = result.fetchmany(2)
            columns = list(result.keys())
        engine.dispose()
    except Exception as exc:
        return False, f"Ошибка выполнения SQL: {exc}", {}

    if len(rows) == 0:
        return False, "Запрос не вернул строк. Нужна ровно 1 строка.", {}
    if len(rows) > 1:
        return False, "Запрос вернул больше 1 строки. Нужна ровно 1 строка.", {}

    values = list(rows[0])
    if len(columns) == 1 and not str(columns[0]).isdigit():
        return True, "ok", {"1": values[0]}

    params: dict[str, object] = {}
    for name, value in zip(columns, values):
        if not str(name).isdigit():
            return (
                False,
                f"Столбец '{name}' назван не по номеру параметра. "
                "Именуйте столбцы числами: 1, 2, 3… ($1, $2 в тексте сообщения).",
                {},
            )
        params[str(name)] = value
    return True, "ok", params


def local_timezone():
    """Часовой пояс локальной машины (там, где запущен бот/клиент)."""
    return datetime.now().astimezone().tzinfo


def daily_input_to_utc(raw: str) -> str:
    """Интерпретировать `daily:HH:MM` как местное время и перевести в UTC."""
    match = re.fullmatch(r"daily:(\d{1,2}):(\d{2})", raw.strip())
    if not match:
        return raw
    hour, minute = int(match.group(1)), int(match.group(2))
    now_local = datetime.now(local_timezone())
    local = datetime(
        now_local.year, now_local.month, now_local.day, hour, minute,
        tzinfo=local_timezone(),
    )
    utc = local.astimezone(timezone.utc)
    return f"daily:{utc.hour:02d}:{utc.minute:02d}"


def format_schedule_label(schedule: str | None) -> str:
    if not schedule:
        return "ручная проверка"
    match = re.fullmatch(r"daily:(\d{1,2}):(\d{2})", schedule.strip())
    if match:
        tz = local_timezone()
        now_local = datetime.now(tz)
        utc_dt = datetime(
            now_local.year, now_local.month, now_local.day,
            int(match.group(1)), int(match.group(2)), tzinfo=timezone.utc,
        )
        local_dt = utc_dt.astimezone(tz)
        offset = local_dt.utcoffset() or timedelta()
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        offset_label = f"UTC{sign}{abs(total_minutes) // 60:02d}:{abs(total_minutes) % 60:02d}"
        return f"daily:{local_dt:%H:%M} ({offset_label})"
    return schedule
