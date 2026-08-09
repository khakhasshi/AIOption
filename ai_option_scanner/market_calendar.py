from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .time_utils import now_et


ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
MARKET_EARLY_CLOSE = time(13, 0)


def market_clock() -> dict[str, object]:
    return market_environment()


def market_environment(current: datetime | None = None) -> dict[str, object]:
    current = (current or now_et()).astimezone(ET)
    trading_day, reason = is_nyse_trading_day(current.date())
    open_at = _session_datetime(current.date(), MARKET_OPEN) if trading_day else None
    close_time, early_close_reason = _nyse_close_time(current.date())
    close_at = _session_datetime(current.date(), close_time) if trading_day else None
    is_open = bool(trading_day and open_at and close_at and open_at <= current < close_at)
    session_state = _session_state(current, trading_day=trading_day, open_at=open_at, close_at=close_at, reason=reason)
    next_open = next_regular_open_after(current)
    next_close = close_at if is_open else _next_regular_close_after(current)
    regular_session = f"{MARKET_OPEN.strftime('%H:%M')}-{close_time.strftime('%H:%M')} ET"
    return {
        "timezone": "America/New_York",
        "now_et": current.isoformat(),
        "date_et": current.date().isoformat(),
        "is_trading_day": trading_day,
        "trading_day_reason": reason,
        "is_market_open_regular": is_open,
        "market_state": _legacy_market_state(session_state),
        "session_state": session_state,
        "regular_session": regular_session,
        "regular_open_time": MARKET_OPEN.strftime("%H:%M"),
        "regular_close_time": close_time.strftime("%H:%M"),
        "regular_open_at_et": open_at.isoformat() if open_at else None,
        "regular_close_at_et": close_at.isoformat() if close_at else None,
        "regular_open_at_utc": open_at.astimezone(timezone.utc).isoformat() if open_at else None,
        "regular_close_at_utc": close_at.astimezone(timezone.utc).isoformat() if close_at else None,
        "next_regular_open_at_et": next_open.isoformat(),
        "next_regular_open_at_utc": next_open.astimezone(timezone.utc).isoformat(),
        "next_regular_close_at_et": next_close.isoformat(),
        "next_regular_close_at_utc": next_close.astimezone(timezone.utc).isoformat(),
        "seconds_to_next_open": max(0, int((next_open - current).total_seconds())),
        "seconds_to_next_close": max(0, int((next_close - current).total_seconds())),
        "is_early_close": bool(early_close_reason),
        "early_close_reason": early_close_reason,
    }


def is_nyse_trading_day(day: date | None = None) -> tuple[bool, str]:
    target = day or now_et().date()
    if target.weekday() >= 5:
        return False, "Weekend"
    holiday = nyse_holiday_name(target)
    if holiday:
        return False, holiday
    return True, "NYSE regular trading day"


def next_nyse_trading_day(day: date) -> date:
    target = day
    while not is_nyse_trading_day(target)[0]:
        target += timedelta(days=1)
    return target


def previous_nyse_trading_day(day: date) -> date:
    target = day
    while not is_nyse_trading_day(target)[0]:
        target -= timedelta(days=1)
    return target


def next_regular_open_after(current: datetime | None = None, *, grace_minutes: int = 0) -> datetime:
    now_value = (current or now_et()).astimezone(ET)
    candidate_day = now_value.date()
    open_at = _session_datetime(candidate_day, MARKET_OPEN) + timedelta(minutes=max(0, grace_minutes))
    if not is_nyse_trading_day(candidate_day)[0] or now_value >= open_at:
        candidate_day = next_nyse_trading_day(candidate_day + timedelta(days=1))
        open_at = _session_datetime(candidate_day, MARKET_OPEN) + timedelta(minutes=max(0, grace_minutes))
    return open_at


def _next_regular_close_after(current: datetime) -> datetime:
    candidate_day = current.date()
    while True:
        trading_day, _ = is_nyse_trading_day(candidate_day)
        close_time, _reason = _nyse_close_time(candidate_day)
        close_at = _session_datetime(candidate_day, close_time)
        if trading_day and close_at > current:
            return close_at
        candidate_day += timedelta(days=1)


def _session_state(
    current: datetime,
    *,
    trading_day: bool,
    open_at: datetime | None,
    close_at: datetime | None,
    reason: str,
) -> str:
    if not trading_day:
        return "weekend" if "weekend" in str(reason or "").lower() else "holiday"
    if open_at and current < open_at:
        return "premarket"
    if open_at and close_at and open_at <= current < close_at:
        return "regular_open"
    return "afterhours"


def _legacy_market_state(session_state: str) -> str:
    if session_state == "regular_open":
        return "regular_open"
    if session_state in {"weekend", "holiday"}:
        return session_state
    return "closed_today"


def _session_datetime(day: date, value: time) -> datetime:
    return datetime.combine(day, value, tzinfo=ET)


def _nyse_close_time(day: date) -> tuple[time, str | None]:
    early_close = nyse_early_close_name(day)
    return (MARKET_EARLY_CLOSE if early_close else MARKET_CLOSE, early_close)


def nyse_early_close_name(day: date) -> str | None:
    if not is_nyse_trading_day(day)[0]:
        return None
    if day == _nth_weekday(day.year, 11, 3, 4) + timedelta(days=1):
        return "Day after Thanksgiving"
    if day == _independence_day_early_close(day.year):
        return "Independence Day early close"
    if day.month == 12 and day.day == 24:
        return "Christmas Eve"
    return None


def _independence_day_early_close(year: int) -> date:
    actual = date(year, 7, 4)
    if actual.weekday() == 5:
        observed = actual - timedelta(days=1)
    elif actual.weekday() == 6:
        observed = actual + timedelta(days=1)
    else:
        observed = actual
    return previous_nyse_trading_day(observed - timedelta(days=1))


@lru_cache(maxsize=256)
def nyse_holiday_name(day: date) -> str | None:
    holidays = _nyse_holidays(day.year)
    previous_year_holidays = _nyse_holidays(day.year - 1)
    return holidays.get(day) or previous_year_holidays.get(day)


def _nyse_holidays(year: int) -> dict[date, str]:
    holidays: dict[date, str] = {}

    _add_observed(holidays, date(year, 1, 1), "New Year's Day")
    holidays[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    holidays[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    holidays[_easter_sunday(year) - timedelta(days=2)] = "Good Friday"
    holidays[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        _add_observed(holidays, date(year, 6, 19), "Juneteenth National Independence Day")
    _add_observed(holidays, date(year, 7, 4), "Independence Day")
    holidays[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    holidays[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    _add_observed(holidays, date(year, 12, 25), "Christmas Day")
    return holidays


def _add_observed(holidays: dict[date, str], actual: date, name: str) -> None:
    observed = actual
    if actual.weekday() == 5:
        observed = actual - timedelta(days=1)
    elif actual.weekday() == 6:
        observed = actual + timedelta(days=1)
    holidays[observed] = name


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    current = next_month - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)
