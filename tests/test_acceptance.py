"""受理冻结、凭证幂等与人工核对。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from src.events import MANUAL_REVIEW_OPENED, ORDER_ACCEPTED
from src.store import DuplicateEventError
from tests.fixtures import (
    HOTEL, accept, at, make_service, scan, two_segment_order,
)


class AcceptanceTest(unittest.TestCase):
    def test_accept_freezes_intake_facts(self) -> None:
        service = make_service()
        events = accept(service)
        self.assertEqual([e.event_type for e in events], [ORDER_ACCEPTED])
        _, state, _ = service._load("ORD-1")
        self.assertEqual(state.frozen["piece_count"], 2)
        self.assertEqual(state.frozen["seal_ids"], ("seal-1", "seal-2"))
        self.assertEqual(state.frozen["authorized_recipients"], frozenset({"tourist:li"}))
        self.assertEqual(state.frozen["appearance"], "黑色28寸硬箱，外绑红绳")
        self.assertIn("移动电源", state.frozen["privacy_hints"])
        self.assertEqual(state.current_custodian, "node:station")

    def test_exact_retransmission_creates_no_second_piece(self) -> None:
        service = make_service()
        cmd = two_segment_order()
        accept(service, cmd)
        # 同一凭证、同一内容重传：返回空，不产生第二件行李。
        self.assertEqual(accept(service, cmd), [])

    def test_same_id_changed_piece_count_opens_manual_review(self) -> None:
        service = make_service()
        accept(service)
        changed = replace(two_segment_order(), piece_count=3,
                          seal_ids=("seal-1", "seal-2", "seal-3"))
        events = accept(service, changed)
        self.assertEqual([e.event_type for e in events], [MANUAL_REVIEW_OPENED])
        self.assertIn("piece_count", events[0].payload["differences"])

    def test_same_id_changed_seal_or_destination_opens_review(self) -> None:
        service = make_service()
        accept(service)
        changed_seal = replace(two_segment_order(), seal_ids=("seal-1", "seal-X"))
        self.assertEqual([e.event_type for e in accept(service, changed_seal)],
                         [MANUAL_REVIEW_OPENED])
        changed_dest = replace(
            two_segment_order(),
            route=(
                two_segment_order().route[0],
                replace(two_segment_order().route[1], to_node="node:other"),
            ),
        )
        self.assertEqual([e.event_type for e in accept(service, changed_dest)],
                         [MANUAL_REVIEW_OPENED])

    def test_pending_review_blocks_handover(self) -> None:
        service = make_service()
        accept(service)
        accept(service, replace(two_segment_order(), piece_count=3,
                                seal_ids=("seal-1", "seal-2", "seal-3")))
        with self.assertRaisesRegex(ValueError, "人工核对冻结"):
            scan(service, "ORD-1", 1, "start", HOTEL, "r-1", at(9), count=3,
                 seals=("seal-1", "seal-2", "seal-3"))

    def test_reject_non_positive_piece_count(self) -> None:
        service = make_service()
        with self.assertRaisesRegex(ValueError, "件数"):
            accept(service, replace(two_segment_order(), piece_count=0))


if __name__ == "__main__":
    unittest.main()
