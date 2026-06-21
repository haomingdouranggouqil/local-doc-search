from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class QuotaDay:
    date: str
    timezone: str
    start_utc: str
    end_utc: str


def current_quota_day(timezone_name: str) -> QuotaDay:
    tz_name = timezone_name or "Asia/Shanghai"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
        tz_name = "UTC"

    today = datetime.now(tz).date()
    start = datetime.combine(today, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return QuotaDay(
        date=today.isoformat(),
        timezone=tz_name,
        start_utc=start.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_utc=end.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )
