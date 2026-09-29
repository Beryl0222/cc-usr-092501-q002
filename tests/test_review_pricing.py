"""人工核对解除与价表边界。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from src.clock import ManualClock
from src.domain import ResolveReview
from src.events import MANUAL_REVIEW_RESOLVED
from src.service import ROLE_REGULATOR, Actor
from tests.fixtures import (
    CARRIER, HOLIDAYS, HOTEL, STATION, STAFF, accept, at, make_service, scan,
    two_segment_order,
)


class ManualReviewResolutionTest(unittest.TestCase):
    def test_resolving_review_unblocks_handovers(self) -> None:
        service = make_service(clock=ManualClock(at(9)))
        accept(service)
        changed = replace(two_segment_order(), piece_count=3,
                          seal_ids=("seal-1", "seal-2", "seal-3"))
        opened = accept(service, changed)
        review_id = opened[0].aggregate_id
        # 核对期间交接冻结
        with self.assertRaisesRegex(ValueError, "人工核对冻结"):
            scan(service, "ORD-1", 1, "start", STATION, "r1", at(9),
                 seals=("seal-1", "seal-2", "seal-3"), count=3)
        resolved = service.resolve_review(
            ResolveReview("ORD-1", review_id, "data_corrected",
                          "前台补录时多勾一件，实为 2 件", at(9, 5)), STAFF)
        self.assertEqual(resolved[0].event_type, MANUAL_REVIEW_RESOLVED)
        # 解冻后按冻结的原始信息可正常交接
        events = scan(service, "ORD-1", 1, "start", STATION, "r1", at(9, 5))
        self.assertEqual(events[0].event_type, "SCAN_RECORDED")

    def test_same_day_holiday_free_segment_has_no_surcharge(self) -> None:
        # 9/29 非节假日、当日运抵：只有基础费
        service = make_service(clock=ManualClock(at(9)), calendar=HOLIDAYS)
        accept(service)
        scan(service, "ORD-1", 1, "start", STATION, "s1", at(9))
        scan(service, "ORD-1", 1, "start", CARRIER, "c1", at(9))
        scan(service, "ORD-1", 1, "end", CARRIER, "c2", at(11))
        scan(service, "ORD-1", 1, "end", HOTEL, "h1", at(11))
        service.settle_segment("ORD-1", 1, STAFF)
        _, state, _ = service._load("ORD-1")
        breakdown = state.segments[1].fee_settled["breakdown"]
        self.assertEqual(breakdown, {
            "base": 12, "cross_midnight_count": 0,
            "cross_midnight_fee": 0, "holiday_fee": 0, "amount": 12,
        })

    def test_regulator_can_see_privacy_hints_but_carrier_cannot(self) -> None:
        service = make_service()
        accept(service)
        chain = service.regulator_chain("ORD-1", Actor("reg-1", ROLE_REGULATOR))
        self.assertIn("移动电源", chain["frozen"]["privacy_hints"])
        view = service.carrier_view("ORD-1", CARRIER)
        self.assertNotIn("privacy_hints", str(view))


if __name__ == "__main__":
    unittest.main()
