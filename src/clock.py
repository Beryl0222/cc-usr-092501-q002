"""可注入时钟与 ISO 8601 时间工具。

跨午夜承诺、节假日服务费和逾期升级都通过注入的时钟读取当前时刻，
监管重放与测试可以用固定时钟复现同一结算结果。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """履约账依赖的时间来源。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产环境使用的系统时钟（UTC）。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试与监管重放使用的固定时钟，可手动推进。"""

    def __init__(self, moment: datetime):
        if moment.tzinfo is None:
            raise ValueError("FixedClock 需要带时区的时间")
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, delta: timedelta) -> None:
        self._moment += delta


def parse_iso(text: str) -> datetime:
    """解析带时区的 ISO 8601 时间，缺时区时拒绝。"""
    parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return moment.isoformat()
