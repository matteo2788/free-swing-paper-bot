from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal


class MarketClock:
    def __init__(self, calendar_name: str = "NYSE", timezone: str = "America/New_York") -> None:
        self.calendar = mcal.get_calendar(calendar_name)
        self.timezone = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(self.timezone)

    def schedule_for(self, value: datetime | None = None) -> pd.DataFrame:
        current = value or self.now()
        day = current.date()
        return self.calendar.schedule(start_date=day, end_date=day)

    def is_market_day(self, value: datetime | None = None) -> bool:
        return not self.schedule_for(value).empty

    def session_bounds(self, value: datetime | None = None) -> tuple[datetime, datetime] | None:
        schedule = self.schedule_for(value)
        if schedule.empty:
            return None
        market_open = schedule.iloc[0]["market_open"].tz_convert(self.timezone).to_pydatetime()
        market_close = schedule.iloc[0]["market_close"].tz_convert(self.timezone).to_pydatetime()
        return market_open, market_close

    def is_open(self, value: datetime | None = None) -> bool:
        current = value or self.now()
        bounds = self.session_bounds(current)
        if bounds is None:
            return False
        market_open, market_close = bounds
        return market_open <= current <= market_close

    def is_after_close_window(self, value: datetime | None = None) -> bool:
        current = value or self.now()
        bounds = self.session_bounds(current)
        if bounds is None:
            return False
        _, market_close = bounds
        return market_close + timedelta(minutes=5) <= current <= market_close + timedelta(hours=2)

    def latest_closed_bar_cutoff(self, interval_minutes: int, value: datetime | None = None) -> datetime | None:
        current = value or self.now()
        bounds = self.session_bounds(current)
        if bounds is None:
            return None
        market_open, market_close = bounds
        effective = min(current, market_close)
        if effective < market_open + timedelta(minutes=interval_minutes):
            return None
        minutes = int((effective - market_open).total_seconds() // 60)
        completed = minutes // interval_minutes
        return market_open + timedelta(minutes=completed * interval_minutes)

    def trading_days_between(self, start: datetime, end: datetime) -> int:
        if end < start:
            return 0
        schedule = self.calendar.schedule(start_date=start.date(), end_date=end.date())
        return int(len(schedule))
