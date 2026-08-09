from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
TIMEZONE_LABEL = "America/New_York"


def now_et() -> datetime:
    return datetime.now(EASTERN)


def now_et_iso() -> str:
    return now_et().isoformat()


def et_today() -> date:
    return now_et().date()


def to_et_iso(value: object, assume_tz: ZoneInfo | timezone = UTC) -> str:
    parsed = parse_datetime(value, assume_tz=assume_tz)
    if parsed is None:
        return str(value or "")
    return parsed.astimezone(EASTERN).isoformat()


def to_et_label(value: object, assume_tz: ZoneInfo | timezone = UTC) -> str:
    parsed = parse_datetime(value, assume_tz=assume_tz)
    if parsed is None:
        return str(value or "")
    return parsed.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S ET")


def parse_datetime(value: object, assume_tz: ZoneInfo | timezone = UTC) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    return parsed


def normalize_time_fields(row: dict | None, fields: tuple[str, ...] = ("time", "timestamp", "published_at", "trade_done_at")) -> dict:
    if not isinstance(row, dict):
        return {}
    output = dict(row)
    for field in fields:
        if field in output and output[field]:
            output[field] = to_et_iso(output[field])
            output[f"{field}_et"] = output[field]
    return output


def time_context() -> dict[str, str]:
    current = now_et()
    return {
        "timezone": TIMEZONE_LABEL,
        "timezone_short": current.tzname() or "ET",
        "now": current.isoformat(),
        "today": current.date().isoformat(),
        "instruction": "All dates and times in this payload are normalized to America/New_York exchange time unless explicitly noted.",
    }
