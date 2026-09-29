"""超时/破损/无人领取三类异常、逾期升级、责任与赔付分离、临时保管、分段结算。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from src.clock import ManualClock
from src.domain import (
    DetermineLiability, IssueCompensation, ReleaseStorage, ReportDamage,
)
from src.events import (
    EXCEPTION_OPENED, EXCEPTION_UPGRADED, INQUIRY_OPENED,
    LIABILITY_DETERMINED, SETTLEMENT_ISSUED, TEMP_STORAGE_OPENED,
    TEMP_STORAGE_RELEASED,
)
from src.service import (
    Actor, ROLE_COMPENSATION_AUDITOR, ROLE_LIABILITY_OFFICER,
)
from tests.fixtures import (
    CARRIER, HOLIDAYS, HOTEL, STATION, STAFF, TOURIST, accept, at,
    complete_segment_one, make_service, scan, single_segment_order,
)

LIABILITY_OFFICER = Actor("officer-li", ROLE_LIABILITY_OFFICER)
AUDITOR = Actor("auditor-wang", ROLE_COMPENSATION_AUDITOR)


class ExceptionFlowTest(unittest.TestCase):
    def test_overdue_in_transit_opens_inquiry_and_escalates(self) -> None:
        service = make_service(clock=ManualClock(at(9)))
        accept(service)
        service.clock = ManualClock(at(16))      # 超过 15:00 送达窗
        first = service.run_due_detections("ORD-1")
        self.assertEqual([e.event_type for e in first],
                         [EXCEPTION_OPENED, INQUIRY_OPENED])
        # 同一时刻重跑不重复立案
        self.assertEqual(service.run_due_detections("ORD-1"), [])
        service.clock = ManualClock(at(16) + timedelta(hours=4))
        upgraded = service.run_due_detections("ORD-1")
        self.assertEqual([e.event_type for e in upgraded], [EXCEPTION_UPGRADED])
        # 升级只发生一次
        service.clock = ManualClock(at(23))
        self.assertEqual(service.run_due_detections("ORD-1"), [])

    def test_unclaimed_luggage_goes_to_temp_storage_then_released(self) -> None:
        service = make_service(clock=ManualClock(at(9)))
        accept(service, single_segment_order())
        complete_segment_one(service)
        service.clock = ManualClock(at(16))
        opened = service.run_due_detections("ORD-1")
        self.assertEqual([e.event_type for e in opened],
                         [EXCEPTION_OPENED, TEMP_STORAGE_OPENED])
        exc_id = opened[0].aggregate_id
        # 非授权人不能认领
        with self.assertRaises(ValueError):
            service.release_storage(
                ReleaseStorage("ORD-1", exc_id, "tourist:other", at(17)), HOTEL)
        released = service.release_storage(
            ReleaseStorage("ORD-1", exc_id, "tourist:li", at(17)), HOTEL)
        self.assertEqual([e.event_type for e in released], [TEMP_STORAGE_RELEASED])

    def test_damage_liability_and_compensation_are_separated(self) -> None:
        service = make_service(clock=ManualClock(at(9)))
        accept(service)
        scan(service, "ORD-1", 1, "start", STATION, "s1", at(9))
        scan(service, "ORD-1", 1, "start", CARRIER, "c1", at(9))
        service.report_damage(
            ReportDamage("ORD-1", "exc-d1", 1, "carrier:A", "箱体开裂", at(12)), CARRIER)
        # 责任认定前不能赔付
        with self.assertRaisesRegex(ValueError, "责任认定"):
            service.issue_compensation(
                IssueCompensation("ORD-1", "exc-d1", 100, "auditor-wang", at(13)),
                AUDITOR)
        liability = service.determine_liability(DetermineLiability(
            "ORD-1", "exc-d1", "carrier:A", "ORD-1-seg01-start",
            "officer-li", "发运交接时封签破损", at(12)), LIABILITY_OFFICER)
        self.assertEqual(liability[0].event_type, LIABILITY_DETERMINED)
        # 认定人本人不能审核赔付
        with self.assertRaisesRegex(ValueError, "compensation_auditor"):
            service.issue_compensation(
                IssueCompensation("ORD-1", "exc-d1", 100, "officer-li", at(13)),
                LIABILITY_OFFICER)
        paid = service.issue_compensation(
            IssueCompensation("ORD-1", "exc-d1", 100, "auditor-wang", at(13)), AUDITOR)
        self.assertEqual(paid[0].event_type, SETTLEMENT_ISSUED)
        # 赔付事件引用但不覆盖原交接事实：起点交接事件仍在
        chain = service.store.order_stream("ORD-1")
        handovers = [e for e in chain if e.payload.get("handover") == "ORD-1-seg01-start"]
        self.assertTrue(handovers)
        self.assertEqual(paid[0].payload["liability_ref"]["failed_handover"],
                         "ORD-1-seg01-start")

    def test_compensation_cannot_reference_nonexistent_handover(self) -> None:
        service = make_service(clock=ManualClock(at(9)))
        accept(service)
        service.report_damage(
            ReportDamage("ORD-1", "exc-d2", 1, "carrier:A", "裂", at(12)), CARRIER)
        service.determine_liability(DetermineLiability(
            "ORD-1", "exc-d2", "carrier:A", "ORD-1-seg99-end",
            "officer-li", "错误引用不存在的交接点", at(12)), LIABILITY_OFFICER)
        service.issue_compensation(
            IssueCompensation("ORD-1", "exc-d2", 50, "auditor-wang", at(13)), AUDITOR)
        regulator = Actor("reg-1", "regulator")
        report = service.verify_payments("ORD-1", regulator)
        self.assertFalse(report["ok"])
        self.assertTrue(any("seg99" in p for p in report["problems"]))

    def test_cross_midnight_and_holiday_pricing_with_injected_clock(self) -> None:
        service = make_service(clock=ManualClock(at(9)), calendar=HOLIDAYS)
        accept(service)
        dep, arr = at(23, 30), at(0, 30, day=30)   # 跨午夜且 9/30 为节假日
        scan(service, "ORD-1", 1, "start", STATION, "s1", dep)
        scan(service, "ORD-1", 1, "start", CARRIER, "c1", dep)
        scan(service, "ORD-1", 1, "end", CARRIER, "c2", arr)
        scan(service, "ORD-1", 1, "end", HOTEL, "h1", arr)
        service.settle_segment("ORD-1", 1, STAFF)
        _, state, _ = service._load("ORD-1")
        breakdown = state.segments[1].fee_settled["breakdown"]
        self.assertEqual(breakdown["base"], 12)
        self.assertEqual(breakdown["cross_midnight_count"], 1)
        self.assertEqual(breakdown["cross_midnight_fee"], 8)
        self.assertEqual(breakdown["holiday_fee"], 6)
        self.assertEqual(breakdown["amount"], 26)
        # 重复结算不产生第二笔
        self.assertEqual(service.settle_segment("ORD-1", 1, STAFF), [])

    def test_unfinished_segment_cannot_be_paid(self) -> None:
        service = make_service(clock=ManualClock(at(9)), calendar=HOLIDAYS)
        accept(service)
        with self.assertRaisesRegex(ValueError, "仅完成"):
            service.settle_segment("ORD-1", 1, STAFF)


if __name__ == "__main__":
    unittest.main()
