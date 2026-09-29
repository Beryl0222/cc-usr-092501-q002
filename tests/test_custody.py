"""保管责任：双方扫描齐备才转移，单方扫描不转移；授权收件、段序、幂等。"""

from __future__ import annotations

import unittest

from src.domain import COMPLETED, IN_TRANSIT, PLANNED
from src.events import (
    CUSTODY_TRANSFERRED, DELIVERY_COMPLETED, MANUAL_REVIEW_OPENED, SCAN_RECORDED,
)
from tests.fixtures import (
    CARRIER, HOTEL, STATION, TOURIST, accept, at, complete_segment_one,
    make_service, scan, two_segment_order,
)


class CustodyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        accept(self.service)

    def test_single_scan_does_not_transfer_custody(self) -> None:
        scan(self.service, "ORD-1", 1, "start", STATION, "r-1", at(9))
        _, state, _ = self.service._load("ORD-1")
        seg = state.segments[1]
        self.assertEqual(seg.state, PLANNED)
        self.assertEqual(state.current_custodian, "node:station")
        self.assertIn("node:station", seg.start_scans)
        self.assertNotIn("carrier:A", seg.start_scans)

    def test_both_scans_transfer_custody(self) -> None:
        scan(self.service, "ORD-1", 1, "start", STATION, "r-1", at(9))
        events = scan(self.service, "ORD-1", 1, "start", CARRIER, "r-2", at(10))
        self.assertEqual([e.event_type for e in events],
                         [SCAN_RECORDED, CUSTODY_TRANSFERRED])
        _, state, _ = self.service._load("ORD-1")
        self.assertEqual(state.segments[1].state, IN_TRANSIT)
        self.assertEqual(state.current_custodian, "carrier:A")
        transfer = events[1]
        self.assertEqual(transfer.payload["from_party"], "node:station")
        self.assertEqual(transfer.payload["to_party"], "carrier:A")
        self.assertEqual(transfer.payload["handover"], "ORD-1-seg01-start")

    def test_second_segment_cannot_depart_before_first_completes(self) -> None:
        with self.assertRaisesRegex(ValueError, "前序运输段"):
            scan(self.service, "ORD-1", 2, "start", HOTEL, "r-x", at(10))

    def test_end_scan_before_departure_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "尚未发运"):
            scan(self.service, "ORD-1", 1, "end", CARRIER, "r-x", at(10))

    def test_full_delivery_to_authorized_recipient(self) -> None:
        complete_segment_one(self.service)
        scan(self.service, "ORD-1", 2, "start", HOTEL, "h2", at(12))
        scan(self.service, "ORD-1", 2, "start", CARRIER, "c2", at(12))
        scan(self.service, "ORD-1", 2, "end", CARRIER, "c3", at(14))
        events = scan(self.service, "ORD-1", 2, "end", TOURIST, "t1", at(14))
        self.assertEqual([e.event_type for e in events],
                         [SCAN_RECORDED, DELIVERY_COMPLETED])
        _, state, _ = self.service._load("ORD-1")
        self.assertTrue(state.delivered)
        self.assertEqual(state.segments[2].state, COMPLETED)
        self.assertEqual(state.current_custodian, "tourist:li")

    def test_identical_scan_retransmission_is_idempotent(self) -> None:
        scan(self.service, "ORD-1", 1, "start", STATION, "r-1", at(9))
        again = scan(self.service, "ORD-1", 1, "start", STATION, "r-1", at(9))
        self.assertEqual(again, [])

    def test_wrong_destination_scan_opens_review_without_transfer(self) -> None:
        events = scan(self.service, "ORD-1", 1, "start", STATION, "r-1", at(9),
                      destination="node:wrong")
        self.assertEqual([e.event_type for e in events], [MANUAL_REVIEW_OPENED])
        _, state, _ = self.service._load("ORD-1")
        self.assertEqual(state.segments[1].state, PLANNED)
        self.assertIsNotNone(state.block_reason)

    def test_unauthorized_party_cannot_scan_handover(self) -> None:
        from src.service import Actor
        stranger = Actor("node:other-hotel", "hotel")
        with self.assertRaisesRegex(ValueError, "只能"):
            scan(self.service, "ORD-1", 1, "start", stranger, "r-x", at(9))

    def test_offline_late_receipt_completes_handover_by_occurrence_time(self) -> None:
        # 承运商先扫 10:00，车站离线回执 09:30 后补；齐备后责任按 10:00 转移。
        scan(self.service, "ORD-1", 1, "start", CARRIER, "c-1", at(10))
        events = scan(self.service, "ORD-1", 1, "start", STATION, "s-1", at(9, 30))
        self.assertEqual([e.event_type for e in events],
                         [SCAN_RECORDED, CUSTODY_TRANSFERRED])
        self.assertEqual(events[1].occurred_at, at(10))


if __name__ == "__main__":
    unittest.main()
