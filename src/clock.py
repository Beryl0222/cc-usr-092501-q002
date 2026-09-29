"""结算用可注入时钟与节假日日历。

业务时间（承诺窗判定、跨午夜计费、节假日服务费）全部取自该抽象，
测试与离线回执恢复时可注入固定/手动时钟，而不依赖系统墙钟。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

CN_TZ = timezone(timedelta(hours=8))


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """按 UTC+8 返回当前时间。"""

    def now(self) -> datetime:
        return datetime.now(CN_TZ)


def _zoned(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=CN_TZ)
    return moment


@dataclass(frozen=True)
class FixedClock:
    """测试与确定性结算用固定时钟。"""

    moment: datetime

    def now(self) -> datetime:
        # 返回副本，避免调用方就地修改污染时钟。
        return _zoned(self.moment).replace()


@dataclass(frozen=True)
class ManualClock:
    """可推进的手动时钟，用于超时与逾期升级测试。"""

    moment: datetime

    def now(self) -> datetime:
        return _zoned(self.moment).replace()

    def advance(self, delta: timedelta) -> "ManualClock":
        return ManualClock(self.now() + delta)


class HolidayCalendar:
    """节假日日历（按 UTC+8 当地日期判定）。

    实际部署可由县文旅局下发的节假日安排构建，测试可注入任意日期集合。
    """

    def __init__(self, holidays: frozenset[date] | set[date] | None = None):
        self.holidays = frozenset(holidays or set())

    def is_holiday(self, moment: datetime) -> bool:
        return _zoned(moment).astimezone(CN_TZ).date() in self.holidays

    @staticmethod
    def ranged(start: date, days: int) -> "HolidayCalendar":
        return HolidayCalendar({start + timedelta(days=i) for i in range(days)})
