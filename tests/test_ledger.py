from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.clock import FixedClock, parse_iso
from src.envelope import validate_event
from src.ledger import (
    CLAIM_SETTLED,
    CUSTODY_TRANSFERRED,
    EXCEPTION_OPENED,
    HANDOVER_SCAN_RECORDED,
    MANUAL_REVIEW_OPENED,
    ORDER_ACCEPTED,
    REROUTE_APPLIED,
    REROUTE_ORDER_APPENDED,
    SETTLEMENT_ISSUED,
    STORAGE_OPENED,
    DomainError,
    RelayLedger,
)
from src.store import EventStore

ROOT = Path(__file__).parents[1]
T0 = "2026-09-30T08:30:00+08:00"


def base_accept(**overrides):
    payload = {
        "voucher_id": "V-001",
        "seal_no": "SEAL-001",
        "destination": "scenic",
        "piece_count": 2,
        "appearance_summary": "黑色20寸登机箱，外观完好",
        "content_hints": ["易碎品"],
        "window_start": "2026-09-30T09:00:00+08:00",
        "window_end": "2026-09-30T18:00:00+08:00",
        "authorized_recipients": ["tok-alice", "tok-bob"],
        "occurred_at": T0,
        "legs": [
            {
                "carrier_id": "carrier-1",
                "from_point": "rail-station",
                "to_point": "hotel-a",
                "window_start": "2026-09-30T09:00:00+08:00",
                "window_end": "2026-09-30T11:00:00+08:00",
            },
            {
                "carrier_id": "carrier-2",
                "from_point": "hotel-a",
                "to_point": "scenic",
                "window_start": "2026-09-30T14:00:00+08:00",
                "window_end": "2026-09-30T16:00:00+08:00",
            },
        ],
    }
    payload.update(overrides)
    return payload


def run_segment(ledger, segment_id, carrier_id, at):
    """走完整段交接：发运、取件双扫、送达双扫。"""
    ledger.dispatch_segment(segment_id, occurred_at=at)
    ledger.record_scan(segment_id, phase="pickup", actor_role="sender", actor_id="staff-out", occurred_at=at)
    ledger.record_scan(segment_id, phase="pickup", actor_role="carrier", actor_id=carrier_id, occurred_at=at)
    ledger.record_scan(segment_id, phase="delivery", actor_role="carrier", actor_id=carrier_id, occurred_at=at)
    ledger.record_scan(segment_id, phase="delivery", actor_role="receiver", actor_id="staff-in", occurred_at=at)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "ledger.jsonl"
        self.clock = FixedClock(parse_iso(T0))
        self.ledger = self.open_ledger()

    def open_ledger(self, **kwargs):
        kwargs.setdefault("holidays", {date(2026, 10, 1)})
        return RelayLedger(EventStore(self.store_path), self.clock, **kwargs)

    def events(self, event_type=None):
        events = EventStore(self.store_path).load()
        if event_type is not None:
            events = [event for event in events if event["event_type"] == event_type]
        return events

    def accept(self, **overrides):
        return self.ledger.accept_order(**base_accept(**overrides))


class AcceptTest(LedgerTestCase):
    def test_accept_freezes_order_facts(self):
        order_id = self.accept()
        order = self.ledger.orders[order_id]
        self.assertEqual(order.piece_count, 2)
        self.assertEqual(order.appearance_summary, "黑色20寸登机箱，外观完好")
        self.assertEqual(order.content_hints, ["易碎品"])
        self.assertEqual(order.authorized_recipients, ["tok-alice", "tok-bob"])
        self.assertEqual(order.current_holder, "rail-station")
        self.assertEqual(len(order.segment_ids), 2)

    def test_full_retransmit_does_not_create_second_luggage(self):
        first = self.accept()
        second = self.accept()
        self.assertEqual(first, second)
        self.assertEqual(len(self.events(ORDER_ACCEPTED)), 1)
        self.assertEqual(len(self.ledger.orders), 1)

    def test_conflicting_voucher_goes_to_manual_review(self):
        self.accept()
        for field, value in (("seal_no", "SEAL-999"), ("destination", "return-point"), ("piece_count", 3)):
            with self.subTest(field=field):
                with self.assertRaises(DomainError):
                    self.accept(**{field: value})
        reviews = self.events(MANUAL_REVIEW_OPENED)
        self.assertEqual(len(reviews), 1)  # 人工核对只开一单
        self.assertEqual(len(self.ledger.orders), 1)

    def test_accept_rejects_broken_itinerary(self):
        with self.assertRaises(DomainError):
            self.accept(destination="elsewhere")  # 行程终点与目的地不一致
        with self.assertRaises(DomainError):
            self.accept(legs=[])
        with self.assertRaises(DomainError):
            self.accept(piece_count=0)


class CustodyTest(LedgerTestCase):
    def test_custody_moves_only_after_both_scans(self):
        order_id = self.accept()
        seg1 = f"{order_id}-S1"
        self.ledger.dispatch_segment(seg1, occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="sender", actor_id="staff-out", occurred_at=T0)
        # 只有一方扫描：保管责任不转移
        self.assertEqual(self.ledger.orders[order_id].current_holder, "rail-station")
        self.assertEqual(self.events(CUSTODY_TRANSFERRED), [])
        self.ledger.record_scan(seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1", occurred_at=T0)
        self.assertEqual(self.ledger.orders[order_id].current_holder, "carrier-1")
        self.assertEqual(self.ledger.segments[seg1].status, "IN_TRANSIT")

    def test_full_delivery_flow(self):
        order_id = self.accept()
        run_segment(self.ledger, f"{order_id}-S1", "carrier-1", T0)
        self.assertEqual(self.ledger.orders[order_id].current_point, "hotel-a")
        run_segment(self.ledger, f"{order_id}-S2", "carrier-2", T0)
        order = self.ledger.orders[order_id]
        self.assertEqual(order.status, "DELIVERED")
        self.assertEqual(order.current_holder, "scenic")

    def test_cannot_dispatch_out_of_turn(self):
        order_id = self.accept()
        with self.assertRaises(DomainError):
            self.ledger.dispatch_segment(f"{order_id}-S2", occurred_at=T0)

    def test_offline_receipts_merge_by_occurred_at(self):
        order_id = self.accept()
        seg1 = f"{order_id}-S1"
        self.clock.advance(timedelta(hours=4))  # 运营方恢复联网时已是 12:30
        self.ledger.dispatch_segment(seg1, occurred_at="2026-09-30T10:00:00+08:00")
        self.ledger.record_scan(
            seg1, phase="pickup", actor_role="sender", actor_id="staff-out",
            occurred_at="2026-09-30T10:05:00+08:00", event_id="receipt-001",
        )
        self.ledger.record_scan(
            seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1",
            occurred_at="2026-09-30T10:10:00+08:00", event_id="receipt-002",
        )
        transfers = self.events(CUSTODY_TRANSFERRED)
        self.assertEqual(len(transfers), 1)
        # 交接生效时间取双方扫描中较晚的发生时间，而非回执上传时间
        self.assertEqual(transfers[0]["occurred_at"], "2026-09-30T10:10:00+08:00")
        # 同一离线回执重传不产生第二条事实
        self.ledger.record_scan(
            seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1",
            occurred_at="2026-09-30T10:10:00+08:00", event_id="receipt-002",
        )
        self.assertEqual(len(self.events(HANDOVER_SCAN_RECORDED)), 2)
        self.assertEqual(len(self.events(CUSTODY_TRANSFERRED)), 1)
        # 监管重放按发生时间排序
        chain = self.ledger.regulator_replay(order_id)["chain"]
        occurred = [entry["occurred_at"] for entry in chain]
        self.assertEqual(occurred, sorted(occurred))


class RerouteTest(LedgerTestCase):
    def accept_three_legs(self):
        payload = base_accept(
            destination="return-point",
            legs=[
                {"carrier_id": "carrier-1", "from_point": "rail-station", "to_point": "hotel-a",
                 "window_start": "2026-09-30T09:00:00+08:00", "window_end": "2026-09-30T11:00:00+08:00"},
                {"carrier_id": "carrier-2", "from_point": "hotel-a", "to_point": "scenic",
                 "window_start": "2026-09-30T14:00:00+08:00", "window_end": "2026-09-30T16:00:00+08:00"},
                {"carrier_id": "carrier-3", "from_point": "scenic", "to_point": "return-point",
                 "window_start": "2026-09-30T18:00:00+08:00", "window_end": "2026-09-30T20:00:00+08:00"},
            ],
        )
        return self.ledger.accept_order(**payload)

    def new_tail(self):
        return [
            {"carrier_id": "carrier-2", "from_point": "hotel-a", "to_point": "hotel-b",
             "window_start": "2026-09-30T14:00:00+08:00", "window_end": "2026-09-30T15:00:00+08:00"},
            {"carrier_id": "carrier-4", "from_point": "hotel-b", "to_point": "return-point",
             "window_start": "2026-09-30T16:00:00+08:00", "window_end": "2026-09-30T19:00:00+08:00"},
        ]

    def test_reroute_before_dispatch_migrates_planned_tail(self):
        order_id = self.accept_three_legs()
        new_tail = [
            {"carrier_id": "carrier-1", "from_point": "rail-station", "to_point": "hotel-b",
             "window_start": "2026-09-30T10:00:00+08:00", "window_end": "2026-09-30T12:00:00+08:00"},
            {"carrier_id": "carrier-4", "from_point": "hotel-b", "to_point": "return-point",
             "window_start": "2026-09-30T16:00:00+08:00", "window_end": "2026-09-30T19:00:00+08:00"},
        ]
        case_id = self.ledger.request_reroute(
            order_id, new_legs=new_tail, reason="列车改签", occurred_at=T0
        )
        self.assertEqual(len(self.events(REROUTE_APPLIED)), 1)
        self.assertEqual(self.events(REROUTE_ORDER_APPENDED), [])
        self.assertEqual(self.ledger.segments[f"{order_id}-S1"].status, "CANCELLED")
        self.assertEqual(self.ledger.segments[f"{order_id}-S2"].status, "CANCELLED")
        self.assertEqual(self.ledger.segments[f"{order_id}-S3"].status, "CANCELLED")
        self.assertEqual(self.ledger.segments[f"{case_id}-S1"].status, "PLANNED")
        self.assertEqual(self.ledger.orders[order_id].destination, "return-point")

    def test_reroute_in_transit_appends_reroute_order(self):
        order_id = self.accept_three_legs()
        seg1 = f"{order_id}-S1"
        self.ledger.dispatch_segment(seg1, occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="sender", actor_id="staff-out", occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1", occurred_at=T0)
        handover_facts_before = self.events(CUSTODY_TRANSFERRED) + self.events(HANDOVER_SCAN_RECORDED)
        case_id = self.ledger.request_reroute(
            order_id, new_legs=self.new_tail(), reason="景区临时关闭",
            new_window_end="2026-09-30T21:00:00+08:00", occurred_at=T0,
        )
        # 在途段不可改线：追加改派单；未发运段取消
        self.assertEqual(len(self.events(REROUTE_ORDER_APPENDED)), 1)
        self.assertEqual(self.ledger.segments[seg1].status, "IN_TRANSIT")
        self.assertEqual(self.ledger.segments[f"{order_id}-S2"].status, "CANCELLED")
        self.assertEqual(self.ledger.segments[f"{order_id}-S3"].status, "CANCELLED")
        self.assertEqual(self.ledger.segments[f"{case_id}-S1"].from_point, "hotel-a")
        self.assertEqual(self.ledger.orders[order_id].window_end, "2026-09-30T21:00:00+08:00")
        # 在途段继续送达后，新段可以接续，全单仍可送达
        self.ledger.record_scan(seg1, phase="delivery", actor_role="carrier", actor_id="carrier-1", occurred_at=T0)
        self.ledger.record_scan(seg1, phase="delivery", actor_role="receiver", actor_id="staff-in", occurred_at=T0)
        run_segment(self.ledger, f"{case_id}-S1", "carrier-2", T0)
        run_segment(self.ledger, f"{case_id}-S2", "carrier-4", T0)
        self.assertEqual(self.ledger.orders[order_id].status, "DELIVERED")
        # 改签没有改写任何已发生的交接事实
        handover_facts_after = self.events(CUSTODY_TRANSFERRED) + self.events(HANDOVER_SCAN_RECORDED)
        self.assertTrue(all(fact in handover_facts_after for fact in handover_facts_before))

    def test_reroute_rejects_misaligned_new_legs(self):
        order_id = self.accept_three_legs()
        bad_tail = [dict(self.new_tail()[0], from_point="somewhere-else")]
        with self.assertRaises(DomainError):
            self.ledger.request_reroute(order_id, new_legs=bad_tail, reason="测试", occurred_at=T0)


class EscalationTest(LedgerTestCase):
    def test_timeout_escalation_survives_restart_without_duplicates(self):
        order_id = self.accept()
        seg1 = f"{order_id}-S1"
        self.ledger.dispatch_segment(seg1, occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="sender", actor_id="staff-out", occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1", occurred_at=T0)
        self.clock.advance(timedelta(hours=3))  # 越过 11:00 承诺窗
        opened = self.ledger.escalate()
        self.assertEqual(opened, [f"EX-{seg1}-TMO"])
        self.assertEqual(self.ledger.exceptions[opened[0]].action, "inquiry")
        event_count = len(self.events())
        # 服务重启：重放账簿后再次升级，既不重复开单也不漏单
        restarted = self.open_ledger()
        self.assertEqual(restarted.escalate(), [])
        self.clock.advance(timedelta(hours=2))
        self.assertEqual(restarted.escalate(), [])
        self.assertEqual(len(self.events()), event_count)
        self.assertEqual(restarted.segments[seg1].status, "IN_TRANSIT")

    def test_restart_does_not_repeat_transfer(self):
        order_id = self.accept()
        seg1 = f"{order_id}-S1"
        self.ledger.dispatch_segment(seg1, occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="sender", actor_id="staff-out", occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1", occurred_at=T0)
        restarted = self.open_ledger()
        restarted.record_scan(seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1", occurred_at=T0)
        self.assertEqual(len(self.events(CUSTODY_TRANSFERRED)), 1)

    def test_unclaimed_goes_to_temporary_storage(self):
        order_id = self.accept()
        run_segment(self.ledger, f"{order_id}-S1", "carrier-1", T0)
        run_segment(self.ledger, f"{order_id}-S2", "carrier-2", T0)
        self.clock.advance(timedelta(hours=25))  # 超过 24 小时领取宽限
        opened = self.ledger.escalate()
        self.assertEqual(opened, [f"EX-{order_id}-UNC"])
        self.assertEqual(len(self.events(STORAGE_OPENED)), 1)
        self.assertEqual(self.ledger.orders[order_id].status, "IN_STORAGE")
        # 游客仍可从临时保管中领取
        self.ledger.collect(order_id, recipient_token="tok-alice")
        self.assertEqual(self.ledger.orders[order_id].status, "COLLECTED")

    def test_collect_requires_authorized_recipient(self):
        order_id = self.accept()
        run_segment(self.ledger, f"{order_id}-S1", "carrier-1", T0)
        run_segment(self.ledger, f"{order_id}-S2", "carrier-2", T0)
        with self.assertRaises(DomainError):
            self.ledger.collect(order_id, recipient_token="tok-stranger")


class ClaimTest(LedgerTestCase):
    def test_damage_claim_with_separation_of_duties(self):
        order_id = self.accept()
        run_segment(self.ledger, f"{order_id}-S1", "carrier-1", T0)
        facts_before = self.events(CUSTODY_TRANSFERRED) + self.events(HANDOVER_SCAN_RECORDED)
        claim_id = self.ledger.report_damage(
            f"{order_id}-S1", reporter_id="hotel-staff", description="箱体侧面划伤", occurred_at=T0
        )
        self.ledger.assess_liability(
            claim_id, assessor_id="assessor-a", liable_segment_id=f"{order_id}-S1",
            amount_cents=5000, occurred_at=T0,
        )
        # 赔付审核人与责任认定人必须分离
        with self.assertRaises(DomainError):
            self.ledger.approve_claim(claim_id, approver_id="assessor-a", occurred_at=T0)
        self.ledger.approve_claim(claim_id, approver_id="approver-b", occurred_at=T0)
        self.assertEqual(self.ledger.claims[claim_id].status, "SETTLED")
        self.assertEqual(len(self.events(CLAIM_SETTLED)), 1)
        # 赔付结案不覆盖原交接事实
        facts_after = self.events(CUSTODY_TRANSFERRED) + self.events(HANDOVER_SCAN_RECORDED)
        self.assertEqual(facts_before, facts_after[: len(facts_before)])
        self.assertEqual(len(facts_after), len(facts_before))


class SettlementTest(LedgerTestCase):
    def test_holiday_and_overnight_fees_with_injected_clock(self):
        payload = base_accept(
            destination="hotel-a",
            window_start="2026-09-30T20:00:00+08:00",
            window_end="2026-10-01T02:00:00+08:00",
            legs=[
                {"carrier_id": "carrier-1", "from_point": "rail-station", "to_point": "hotel-a",
                 "window_start": "2026-09-30T20:00:00+08:00", "window_end": "2026-10-01T02:00:00+08:00"},
            ],
        )
        order_id = self.ledger.accept_order(**payload)
        seg1 = f"{order_id}-S1"
        run_segment(self.ledger, seg1, "carrier-1", T0)
        settlement_id = self.ledger.settle_segment(seg1)
        issued = self.events(SETTLEMENT_ISSUED)
        self.assertEqual(len(issued), 1)
        # 基础 1200 + 节假日加收 600（10-01 在注入节假日表内）+ 跨午夜 800
        self.assertEqual(issued[0]["base_cents"], 1200)
        self.assertEqual(issued[0]["holiday_cents"], 600)
        self.assertEqual(issued[0]["overnight_cents"], 800)
        self.assertEqual(issued[0]["total_cents"], 2600)
        # 重复结算幂等
        self.assertEqual(self.ledger.settle_segment(seg1), settlement_id)
        self.assertEqual(len(self.events(SETTLEMENT_ISSUED)), 1)
        # 监管重放逐段核对付款
        replay = self.ledger.regulator_replay(order_id)
        self.assertTrue(replay["all_payments_match"])
        payment = replay["payments"][0]
        self.assertEqual(payment["issued_total_cents"], 2600)
        self.assertEqual(payment["recomputed_total_cents"], 2600)
        self.assertTrue(payment["match"])

    def test_unsettled_segment_cannot_settle_before_delivery(self):
        order_id = self.accept()
        with self.assertRaises(DomainError):
            self.ledger.settle_segment(f"{order_id}-S1")


class ViewTest(LedgerTestCase):
    def test_tourist_tracking_requires_authorized_token(self):
        order_id = self.accept()
        seg1 = f"{order_id}-S1"
        self.ledger.dispatch_segment(seg1, occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="sender", actor_id="staff-out", occurred_at=T0)
        self.ledger.record_scan(seg1, phase="pickup", actor_role="carrier", actor_id="carrier-1", occurred_at=T0)
        with self.assertRaises(DomainError):
            self.ledger.track_for_tourist("V-001", "tok-stranger")
        with self.assertRaises(DomainError):
            self.ledger.track_for_tourist("V-404", "tok-alice")
        view = self.ledger.track_for_tourist("V-001", "tok-alice")
        self.assertEqual(view["status"], "IN_TRANSIT")
        self.assertEqual(view["custody_holder"], "carrier-1")
        self.assertEqual(view["current_point"], "rail-station")
        self.assertEqual(view["eta"], "2026-09-30T18:00:00+08:00")

    def test_carrier_view_shows_only_current_segment(self):
        order_id = self.accept()
        seg1 = f"{order_id}-S1"
        self.ledger.dispatch_segment(seg1, occurred_at=T0)
        view = self.ledger.carrier_view("carrier-1")
        self.assertEqual(len(view), 1)
        entry = view[0]
        self.assertEqual(entry["segment_id"], seg1)
        self.assertEqual(entry["to_point"], "hotel-a")
        self.assertNotIn("authorized_recipients", entry)
        self.assertNotIn("legs", entry)
        # 另一承运商的段尚未发运，且看不到别人的段
        self.assertEqual(self.ledger.carrier_view("carrier-2"), [])

    def test_hotel_view_cannot_read_full_itinerary(self):
        order_id = self.accept()
        view = self.ledger.hotel_view("hotel-a")
        self.assertEqual(len(view), 1)
        entry = view[0]
        self.assertEqual(entry["segment_id"], f"{order_id}-S1")
        self.assertNotIn("from_point", entry)
        self.assertNotIn("authorized_recipients", entry)
        self.assertNotIn("legs", entry)


class ContractTest(LedgerTestCase):
    def test_all_emitted_events_match_envelope_and_schema_enums(self):
        order_id = self.accept()
        run_segment(self.ledger, f"{order_id}-S1", "carrier-1", T0)
        run_segment(self.ledger, f"{order_id}-S2", "carrier-2", T0)
        claim_id = self.ledger.report_damage(
            f"{order_id}-S1", reporter_id="hotel-staff", description="划痕", occurred_at=T0
        )
        self.ledger.assess_liability(
            claim_id, assessor_id="assessor-a", liable_segment_id=f"{order_id}-S1",
            amount_cents=1000, occurred_at=T0,
        )
        self.ledger.approve_claim(claim_id, approver_id="approver-b", occurred_at=T0)
        self.ledger.settle_segment(f"{order_id}-S1")
        self.ledger.settle_segment(f"{order_id}-S2")
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        event_types = set(schema["properties"]["event_type"]["enum"])
        aggregate_types = set(schema["properties"]["aggregate_type"]["enum"])
        events = self.events()
        self.assertGreater(len(events), 10)
        for event in events:
            self.assertEqual(validate_event(event), [], event)
            self.assertIn(event["event_type"], event_types, event)
            self.assertIn(event["aggregate_type"], aggregate_types, event)


class RelayCliTest(unittest.TestCase):
    def test_cli_accept_and_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "ledger.jsonl"
            accept = subprocess.run(
                [sys.executable, "-m", "src.relay_cli", "accept", str(store), "data/accept_sample.json"],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(accept.returncode, 0, accept.stderr)
            self.assertIn("LO-V-20260930-001", accept.stdout)
            # 同一凭证完全重传不产生第二件行李
            again = subprocess.run(
                [sys.executable, "-m", "src.relay_cli", "accept", str(store), "data/accept_sample.json"],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            track = subprocess.run(
                [sys.executable, "-m", "src.relay_cli", "track", str(store), "V-20260930-001", "tok-alice"],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(track.returncode, 0, track.stderr)
            self.assertIn("rail-station", track.stdout)
            replay = subprocess.run(
                [sys.executable, "-m", "src.relay_cli", "replay", str(store), "LO-V-20260930-001"],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertIn("ORDER_ACCEPTED", replay.stdout)


if __name__ == "__main__":
    unittest.main()
