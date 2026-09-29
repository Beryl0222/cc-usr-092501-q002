"""测试共用构造器。"""

from __future__ import annotations

from datetime import datetime

from src.clock import CN_TZ, FixedClock, HolidayCalendar, ManualClock
from src.domain import AcceptOrder, SegmentPlan, SubmitScan
from src.service import (
    Actor, LedgerService, ROLE_CARRIER, ROLE_HOTEL, ROLE_STAFF,
)
from src.store import EventStore

BASE_DAY = datetime(2026, 9, 29, tzinfo=CN_TZ)
HOLIDAYS = HolidayCalendar({datetime(2026, 9, 30).date()})

STATION = Actor("node:station", ROLE_HOTEL)
HOTEL = Actor("node:hotel-h", ROLE_HOTEL)
CARRIER = Actor("carrier:A", ROLE_CARRIER)
STAFF = Actor("staff-1", ROLE_STAFF)
TOURIST = Actor("tourist:li", "tourist")


def at(hour: int, minute: int = 0, day: int = 29) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=CN_TZ)


def single_segment_order(order_id: str = "ORD-1", *, window_end: datetime | None = None) -> AcceptOrder:
    """车站→酒店单段，到达节点而非游客本人，用于无人领取/临时保管场景。"""
    route = (
        SegmentPlan(1, "station_hotel", "carrier:A", "node:station", "node:hotel-h",
                    "node:station", "node:hotel-h", expected_by=at(11)),
    )
    return AcceptOrder(
        event_id=f"evt-{order_id}", order_id=order_id, tourist_ref="tourist:li",
        piece_count=2, seal_ids=("seal-1", "seal-2"),
        appearance="黑色28寸硬箱，外绑红绳",
        privacy_hints="提示：含移动电源（不开箱清点）",
        window_start=at(9), window_end=window_end or at(15),
        authorized_recipients=frozenset({"tourist:li"}),
        route=route, occurred_at=at(9),
    )


def two_segment_order(order_id: str = "ORD-1", *, tourist_delivery: bool = True,
                      window_end: datetime | None = None) -> AcceptOrder:
    route = (
        SegmentPlan(1, "station_hotel", "carrier:A", "node:station", "node:hotel-h",
                    "node:station", "node:hotel-h", expected_by=at(11)),
        SegmentPlan(2, "hotel_scenic", "carrier:A", "node:hotel-h",
                    "tourist:li" if tourist_delivery else "node:scenic",
                    "node:hotel-h",
                    "node:scenic-gate" if tourist_delivery else "node:scenic",
                    delivers_to_tourist=tourist_delivery, expected_by=at(14)),
    )
    return AcceptOrder(
        event_id=f"evt-{order_id}", order_id=order_id, tourist_ref="tourist:li",
        piece_count=2, seal_ids=("seal-1", "seal-2"),
        appearance="黑色28寸硬箱，外绑红绳",
        privacy_hints="提示：含移动电源（不开箱清点）",
        window_start=at(9), window_end=window_end or at(15),
        authorized_recipients=frozenset({"tourist:li"}),
        route=route, occurred_at=at(9),
    )


def make_service(path: str | None = None, clock=None, calendar: HolidayCalendar | None = None):
    return LedgerService(
        EventStore(path), clock or FixedClock(at(12)), calendar or HolidayCalendar(),
    )


def accept(service: LedgerService, cmd: AcceptOrder | None = None, **kwargs):
    cmd = cmd or two_segment_order(**kwargs)
    return service.accept_order(cmd, STAFF)


def scan(service: LedgerService, order_id: str, seq: int, endpoint: str, actor: Actor,
         receipt: str, when: datetime, *, destination: str | None = None,
         seals=("seal-1", "seal-2"), count: int = 2, command_id: str | None = None):
    if destination is None:
        destination = "node:hotel-h" if seq == 1 else "node:scenic-gate"
    return service.submit_scan(
        SubmitScan(order_id, seq, endpoint, actor.actor_id, receipt, when,
                   seals, destination, count),
        actor, command_id=command_id,
    )


def complete_segment_one(service: LedgerService, order_id: str = "ORD-1") -> None:
    scan(service, order_id, 1, "start", STATION, "st-s", at(9))
    scan(service, order_id, 1, "start", CARRIER, "ca-s", at(10))
    scan(service, order_id, 1, "end", CARRIER, "ca-e", at(11))
    scan(service, order_id, 1, "end", HOTEL, "ho-e", at(11))


def advanceable(service: LedgerService):
    return ManualClock(service.clock.now())
