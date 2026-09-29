"""改签：只迁移尚未发运的后续段；在途件追加改派单。"""

from __future__ import annotations

import unittest

from src.domain import IN_TRANSIT, PLANNED, RequestReroute
from src.events import REDISPATCH_ISSUED, REROUTE_APPLIED, REROUTE_REQUESTED
from tests.fixtures import STAFF
from tests.fixtures import (
    CARRIER, HOTEL, STATION, accept, at, make_service, scan, two_segment_order,
)


class RerouteTest(unittest.TestCase):
    def _in_transit_segment_one(self, service, order_id="ORD-1"):
        accept(service, two_segment_order(order_id, tourist_delivery=False))
        scan(service, order_id, 1, "start", STATION, "s1", at(9))
        scan(service, order_id, 1, "start", CARRIER, "c1", at(10))

    def test_reroute_migrates_only_undispatched_segments(self) -> None:
        service = make_service()
        self._in_transit_segment_one(service)
        events = service.request_reroute(
            RequestReroute("ORD-1", "case-1", "node:new-spot", at(18),
                           "景区临时关闭", at(10)), STAFF)
        types = [e.event_type for e in events]
        self.assertEqual(types, [REROUTE_REQUESTED, REROUTE_APPLIED])
        applied = events[1]
        self.assertEqual([c["seq"] for c in applied.payload["changes"]], [2])
        _, state, _ = service._load("ORD-1")
        seg1, seg2 = state.segments[1], state.segments[2]
        self.assertEqual(seg1.state, IN_TRANSIT)          # 在途段不动
        self.assertEqual(seg2.state, PLANNED)
        self.assertEqual(seg2.plan.to_node, "node:new-spot")
        self.assertIsNotNone(seg2.migrated_from)         # 保留迁移前事实

    def test_in_transit_last_segment_gets_redispatch_order(self) -> None:
        service = make_service()
        from tests.fixtures import single_segment_order
        accept(service, single_segment_order("ORD-2"))
        scan(service, "ORD-2", 1, "start", STATION, "s1", at(9))
        scan(service, "ORD-2", 1, "start", CARRIER, "c1", at(10))
        events = service.request_reroute(
            RequestReroute("ORD-2", "case-2", "node:new-spot", at(18),
                           "行程改签", at(10)), STAFF)
        self.assertEqual([e.event_type for e in events],
                         [REROUTE_REQUESTED, REDISPATCH_ISSUED])
        redispatch = events[1]
        self.assertEqual(redispatch.payload["in_transit_seq"], 1)
        self.assertEqual(redispatch.payload["new_seq"], 2)
        _, state, _ = service._load("ORD-2")
        self.assertEqual(state.segments[2].redispatch_of, 1)
        self.assertEqual(state.segments[2].plan.to_node, "node:new-spot")

    def test_reroute_before_departure_migrates_all(self) -> None:
        service = make_service()
        accept(service, two_segment_order("ORD-3", tourist_delivery=False))
        events = service.request_reroute(
            RequestReroute("ORD-3", "case-3", "node:new-spot", at(18),
                           "改签", at(8)), STAFF)
        types = [e.event_type for e in events]
        self.assertIn(REROUTE_APPLIED, types)
        applied = events[1]
        self.assertEqual([c["seq"] for c in applied.payload["changes"]], [1, 2])
        self.assertNotIn(REDISPATCH_ISSUED, types)

    def test_delivered_order_cannot_reroute(self) -> None:
        service = make_service()
        accept(service)
        from tests.fixtures import TOURIST, complete_segment_one
        complete_segment_one(service)
        scan(service, "ORD-1", 2, "start", HOTEL, "h1", at(12))
        scan(service, "ORD-1", 2, "start", CARRIER, "c1", at(12))
        scan(service, "ORD-1", 2, "end", CARRIER, "c2", at(14))
        scan(service, "ORD-1", 2, "end", TOURIST, "t1", at(14))
        with self.assertRaisesRegex(ValueError, "已交付"):
            service.request_reroute(
                RequestReroute("ORD-1", "case-x", "node:x", at(18), "x", at(15)),
                TOURIST)


if __name__ == "__main__":
    unittest.main()
