"""最小可见视图、监管整链重放、离线合并、重启幂等与逾期升级不丢失。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.clock import ManualClock
from src.events import CUSTODY_TRANSFERRED, DELIVERY_COMPLETED
from src.service import (
    Actor, ROLE_CARRIER, ROLE_HOTEL, ROLE_REGULATOR, ROLE_STAFF, ROLE_TOURIST,
)
from src.store import ConflictError, EventStore
from tests.fixtures import (
    CARRIER, HOLIDAYS, HOTEL, STATION, STAFF, TOURIST, accept, at,
    complete_segment_one, make_service, scan, two_segment_order,
)

REGULATOR = Actor("reg-1", ROLE_REGULATOR)
OTHER_CARRIER = Actor("carrier:B", ROLE_CARRIER)
OTHER_HOTEL = Actor("node:hotel-other", ROLE_HOTEL)


class ViewPrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        accept(self.service)
        complete_segment_one(self.service)

    def test_tourist_sees_location_and_eta(self) -> None:
        view = self.service.tourist_view("ORD-1", TOURIST)
        self.assertIn("current_location", view)
        self.assertIn("eta", view)
        self.assertFalse(view["delivered"])

    def test_carrier_sees_only_current_segment(self) -> None:
        # 段1 已完成，段2 未发运：只能看到段2，且不含段1或其他订单信息。
        view = self.service.carrier_view("ORD-1", CARRIER)
        self.assertEqual(view["segment"]["seq"], 2)
        self.assertNotIn("route", view)
        self.assertNotIn("privacy_hints", view["segment"])

    def test_carrier_without_assignment_sees_nothing(self) -> None:
        with self.assertRaisesRegex(ValueError, "不属于该承运商"):
            self.service.carrier_view("ORD-1", OTHER_CARRIER)

    def test_hotel_cannot_read_full_itinerary(self) -> None:
        view = self.service.hotel_view("ORD-1", HOTEL)
        # 只暴露与本节点有关的交接，没有其他段的起讫节点全貌。
        for handover in view["handovers"]:
            self.assertNotIn("from_node", handover)
            self.assertNotIn("to_node", handover)
        with self.assertRaisesRegex(ValueError, "只能扫描"):
            scan(self.service, "ORD-1", 1, "start", OTHER_HOTEL, "x", at(9))

    def test_role_gate_on_views(self) -> None:
        with self.assertRaisesRegex(ValueError, "tourist"):
            self.service.tourist_view("ORD-1", CARRIER)
        with self.assertRaisesRegex(ValueError, "carrier"):
            self.service.carrier_view("ORD-1", TOURIST)
        with self.assertRaisesRegex(ValueError, "regulator"):
            self.service.regulator_chain("ORD-1", STAFF)


class RegulatorReplayTest(unittest.TestCase):
    def test_chain_replay_lists_every_handover_and_payment(self) -> None:
        service = make_service(clock=ManualClock(at(12)), calendar=HOLIDAYS)
        accept(service)
        complete_segment_one(service)
        scan(service, "ORD-1", 2, "start", HOTEL, "h2s", at(12))
        scan(service, "ORD-1", 2, "start", CARRIER, "c2s", at(12))
        scan(service, "ORD-1", 2, "end", CARRIER, "c2e", at(14))
        scan(service, "ORD-1", 2, "end", TOURIST, "t2e", at(14))
        service.settle_segment("ORD-1", 1, STAFF)
        service.settle_segment("ORD-1", 2, STAFF)

        chain = service.regulator_chain("ORD-1", REGULATOR)
        self.assertEqual(len(chain["segments"]), 2)
        # 每段都有 start/end 双方扫描记录
        for segment in chain["segments"]:
            self.assertEqual(len(segment["start"]["scans"]), 2)
            self.assertEqual(len(segment["end"]["scans"]), 2)
        handover_types = [e["event_type"] for e in chain["events"]]
        self.assertEqual(handover_types.count(CUSTODY_TRANSFERRED), 3)  # 段1起/止 + 段2起
        self.assertEqual(handover_types.count(DELIVERY_COMPLETED), 1)
        report = service.verify_payments("ORD-1", REGULATOR)
        self.assertTrue(report["ok"], report["problems"])
        fees = chain["payments"]["segment_fees"]
        self.assertEqual(len(fees), 2)

    def test_missing_payment_for_completed_segment_is_flagged(self) -> None:
        service = make_service()
        accept(service)
        complete_segment_one(service)
        report = service.verify_payments("ORD-1", REGULATOR)
        self.assertFalse(report["ok"])
        self.assertTrue(any("缺少服务费结算" in p for p in report["problems"]))


class OfflineAndRestartTest(unittest.TestCase):
    def test_late_offline_receipt_merges_by_occurrence_time(self) -> None:
        service = make_service()
        accept(service)
        scan(service, "ORD-1", 1, "start", CARRIER, "c-1000", at(10))
        scan(service, "ORD-1", 1, "start", STATION, "s-0930", at(9, 30))
        events = service.store.order_stream("ORD-1")
        scans = [e for e in events if e.payload.get("receipt_id")]
        # 晚到的 09:30 回执排在 10:00 之前
        self.assertEqual(scans[0].payload["receipt_id"], "s-0930")
        self.assertEqual(scans[1].payload["receipt_id"], "c-1000")
        # 受理 + 两条扫描 + 双方齐备的责任转移，版本按发生时间从 1 连续编号
        self.assertEqual([e.version for e in events], [1, 2, 3, 4])

    def test_restart_recovers_and_command_stays_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            service = make_service(path, clock=ManualClock(at(9)))
            service.accept_order(two_segment_order(), STAFF, command_id="cmd-accept")
            scan(service, "ORD-1", 1, "start", STATION, "r1", at(9), command_id="cmd-scan1")

            # 重启：新服务从磁盘重放
            restarted = make_service(path, clock=ManualClock(at(9)))
            again = restarted.accept_order(
                two_segment_order(), STAFF, command_id="cmd-accept")
            self.assertEqual(len(again), 1)            # 返回首次已记录事件
            self.assertEqual(again[0].event_id, "evt-ORD-1")
            _, state, _ = restarted._load("ORD-1")
            self.assertIn("node:station", state.segments[1].start_scans)
            # 同 command_id 不同请求体应冲突
            from dataclasses import replace
            changed = replace(two_segment_order(), piece_count=1)
            with self.assertRaises(ConflictError):
                restarted.accept_order(changed, STAFF, command_id="cmd-accept")

    def test_restart_neither_duplicates_nor_misses_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            service = make_service(path, clock=ManualClock(at(9)))
            accept(service)
            service.clock = ManualClock(at(16))
            service.run_due_detections("ORD-1")       # 超时 + 查询
            # 在升级发生前重启
            restarted = make_service(path, clock=ManualClock(at(20)))
            events = restarted.run_due_detections("ORD-1")
            self.assertEqual([e.event_type for e in events], ["EXCEPTION_UPGRADED"])
            # 再次重启，升级不重复
            restarted_again = make_service(path, clock=ManualClock(at(21)))
            self.assertEqual(restarted_again.run_due_detections("ORD-1"), [])


if __name__ == "__main__":
    unittest.main()
