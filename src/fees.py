"""分段运费结算。

节假日服务费与跨午夜承诺附加费都是承诺送达窗与注入节假日表的纯函数，
结算时刻由可注入时钟给出（见 ``ledger.RelayLedger.settle_segment``），
因此监管重放可以复现同一份结算单。金额一律以分为单位的整数表示。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

BASE_CENTS = 1200  # 每段基础运费
HOLIDAY_SURCHARGE_NUM = 1  # 节假日加收基础费的 1/2
HOLIDAY_SURCHARGE_DEN = 2
OVERNIGHT_CENTS = 800  # 承诺窗跨午夜加收的夜间保管费


@dataclass(frozen=True)
class FeeBreakdown:
    base_cents: int
    holiday_cents: int
    overnight_cents: int

    @property
    def total_cents(self) -> int:
        return self.base_cents + self.holiday_cents + self.overnight_cents

    def as_dict(self) -> dict[str, int]:
        return {
            "base_cents": self.base_cents,
            "holiday_cents": self.holiday_cents,
            "overnight_cents": self.overnight_cents,
            "total_cents": self.total_cents,
        }


def segment_fee(
    *,
    window_start: datetime,
    window_end: datetime,
    holidays: Iterable[date],
) -> FeeBreakdown:
    """按承诺送达窗计算一段运费。

    - 承诺窗任一端落在注入的节假日表内：加收节假日服务费；
    - 承诺窗起点与终点不在同一自然日（跨午夜承诺）：加收夜间保管费。
    """
    holiday_set = frozenset(holidays)
    base = BASE_CENTS
    holiday = 0
    if window_start.date() in holiday_set or window_end.date() in holiday_set:
        holiday = base * HOLIDAY_SURCHARGE_NUM // HOLIDAY_SURCHARGE_DEN
    overnight = OVERNIGHT_CENTS if window_end.date() > window_start.date() else 0
    return FeeBreakdown(base, holiday, overnight)
