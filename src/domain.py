"""便民行李接力履约账——领域核心。

纯函数式事件溯源：``decide_*`` 依据当前状态产出事件，``fold`` 把事件
归约为状态。所有业务时间显式传入，结算与超时检测不直接读系统墙钟。

保管责任规则：每段运输在交接点须由交出方与接收方分别扫描，双方齐备
才发出 CUSTODY_TRANSFERRED；任何一方缺失，保管责任不转移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .clock import CN_TZ, HolidayCalendar
from .events import (
    CUSTODY_TRANSFERRED, DELIVERY_COMPLETED, EXCEPTION_OPENED, EXCEPTION_UPGRADED,
    INQUIRY_OPENED, LIABILITY_DETERMINED, MANUAL_REVIEW_OPENED,
    MANUAL_REVIEW_RESOLVED, ORDER_ACCEPTED, REDISPATCH_ISSUED, REROUTE_APPLIED,
    REROUTE_REQUESTED, SCAN_RECORDED, SETTLEMENT_ISSUED, TEMP_STORAGE_OPENED,
    TEMP_STORAGE_RELEASED,
    AGG_EXCEPTION, AGG_ORDER, AGG_REROUTE, AGG_REVIEW, AGG_SEGMENT,
    AGG_SETTLEMENT, DomainError, Event,
)

# 查询立案后多久未结则升级
INQUIRY_ESCALATION = timedelta(hours=4)

# 分段基础服务费与附加费（元）。实际价表由购买服务方下发。
BASE_FEE = {"station_hotel": 12, "hotel_scenic": 10, "scenic_return": 10, "relay": 10}
CROSS_MIDNIGHT_FEE = 8
HOLIDAY_FEE = 6

PLANNED, IN_TRANSIT, COMPLETED = "PLANNED", "IN_TRANSIT", "COMPLETED"
START, END = "start", "end"


# ---- 命令 --------------------------------------------------------------------

@dataclass(frozen=True)
class SegmentPlan:
    seq: int
    seg_type: str                      # station_hotel / hotel_scenic / scenic_return / relay
    carrier: str                       # 承运商标识
    from_party: str                    # 交出方
    to_party: str                      # 接收方（非交付给游客的服务节点）
    from_node: str
    to_node: str
    delivers_to_tourist: bool = False  # 末段：接收方须为授权收件人
    expected_by: datetime | None = None


@dataclass(frozen=True)
class AcceptOrder:
    event_id: str
    order_id: str
    tourist_ref: str
    piece_count: int
    seal_ids: tuple[str, ...]
    appearance: str
    privacy_hints: str                 # 隐私化物品提示，不做逐件清点
    window_start: datetime
    window_end: datetime
    authorized_recipients: frozenset[str]
    route: tuple[SegmentPlan, ...]
    occurred_at: datetime
    command_id: str | None = None


@dataclass(frozen=True)
class SubmitScan:
    order_id: str
    seq: int
    endpoint: str                      # start / end
    party: str
    receipt_id: str                    # 承运商/酒店/游客中心各自的本地回执号
    scanned_at: datetime
    seal_ids: tuple[str, ...]
    destination: str
    piece_count: int


@dataclass(frozen=True)
class RequestReroute:
    order_id: str
    case_id: str
    new_destination_node: str
    new_window_end: datetime
    reason: str
    at: datetime


@dataclass(frozen=True)
class ReportDamage:
    order_id: str
    exception_id: str
    seq: int
    observed_by: str
    note: str
    at: datetime


@dataclass(frozen=True)
class DetermineLiability:
    order_id: str
    exception_id: str
    responsible_party: str
    failed_handover: str               # 认定所依据的交接点（不可脱离事实）
    reviewer_id: str
    basis: str
    at: datetime


@dataclass(frozen=True)
class IssueCompensation:
    order_id: str
    exception_id: str
    amount: float
    reviewer_id: str
    at: datetime


@dataclass(frozen=True)
class ResolveReview:
    order_id: str
    review_id: str
    resolution: str                    # confirmed_duplicate / data_corrected / rejected
    note: str
    at: datetime


@dataclass(frozen=True)
class ReleaseStorage:
    order_id: str
    exception_id: str
    recipient: str
    at: datetime


# ---- 状态 --------------------------------------------------------------------

@dataclass
class SegmentState:
    plan: SegmentPlan
    state: str = PLANNED
    start_scans: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    end_scans: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    departed_at: datetime | None = None
    arrived_at: datetime | None = None
    migrated_from: dict[str, Any] | None = None   # 改签迁移前的目的地
    redispatch_of: int | None = None              # 改派单所接续的在途段
    fee_settled: dict[str, Any] | None = None


@dataclass
class ExceptionState:
    exception_id: str
    kind: str                          # timeout / damage / unclaimed
    seq: int | None
    opened_at: datetime
    inquiry: bool = False
    escalated: bool = False
    liability: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None
    storage: bool = False
    closed: bool = False


@dataclass
class OrderState:
    order_id: str | None = None
    accepted: bool = False
    frozen: dict[str, Any] = field(default_factory=dict)
    segments: dict[int, SegmentState] = field(default_factory=dict)
    current_custodian: str | None = None
    delivered: bool = False
    delivered_at: datetime | None = None
    reviews: list[dict[str, Any]] = field(default_factory=list)
    exceptions: dict[str, ExceptionState] = field(default_factory=dict)
    reroutes: list[dict[str, Any]] = field(default_factory=list)
    block_reason: str | None = None   # 人工核对未决时冻结交接

    @property
    def active_seq(self) -> int | None:
        for seq in sorted(self.segments):
            if self.segments[seq].state == IN_TRANSIT:
                return seq
        return None

    def segment_after(self, seq: int) -> list[int]:
        return [s for s in sorted(self.segments) if s > seq]


# ---- 归约 --------------------------------------------------------------------

def fold(state: OrderState | None, event: Event) -> OrderState:
    state = state or OrderState()
    p = event.payload
    et = event.event_type

    if et == ORDER_ACCEPTED:
        state.order_id = p["order_id"]
        state.accepted = True
        state.current_custodian = p.get("initial_custodian")
        state.frozen = {
            "piece_count": p["piece_count"],
            "seal_ids": tuple(p["seal_ids"]),
            "destination": p["final_destination"],
            "appearance": p["appearance"],
            "privacy_hints": p["privacy_hints"],
            "window_start": _dt(p["window_start"]),
            "window_end": _dt(p["window_end"]),
            "authorized_recipients": frozenset(p["authorized_recipients"]),
        }
        for raw in p["route"]:
            plan = SegmentPlan(
                seq=raw["seq"], seg_type=raw["seg_type"], carrier=raw["carrier"],
                from_party=raw["from_party"], to_party=raw["to_party"],
                from_node=raw["from_node"], to_node=raw["to_node"],
                delivers_to_tourist=raw.get("delivers_to_tourist", False),
                expected_by=_dt(raw["expected_by"]) if raw.get("expected_by") else None,
            )
            state.segments[plan.seq] = SegmentState(plan=plan)

    elif et == MANUAL_REVIEW_OPENED:
        state.reviews.append({"review_id": event.aggregate_id, **p, "resolved": False})
        state.block_reason = event.aggregate_id

    elif et == MANUAL_REVIEW_RESOLVED:
        for review in state.reviews:
            if review["review_id"] == p["review_id"]:
                review["resolved"] = True
                review["resolution"] = p["resolution"]
        if state.block_reason == p["review_id"]:
            state.block_reason = None

    elif et == SCAN_RECORDED:
        seg = state.segments[p["seq"]]
        bucket = seg.start_scans if p["endpoint"] == START else seg.end_scans
        bucket[p["party"]] = (p["receipt_id"], event.occurred_at)

    elif et == CUSTODY_TRANSFERRED:
        seg = state.segments[p["seq"]]
        if p["endpoint"] == START:
            seg.state = IN_TRANSIT
            seg.departed_at = event.occurred_at
        else:
            seg.state = COMPLETED
            seg.arrived_at = event.occurred_at
        state.current_custodian = p["to_party"]

    elif et == DELIVERY_COMPLETED:
        seg = state.segments[p["seq"]]
        seg.state = COMPLETED
        seg.arrived_at = event.occurred_at
        state.current_custodian = p["recipient"]
        state.delivered = True
        state.delivered_at = event.occurred_at

    elif et == REROUTE_APPLIED:
        for change in p["changes"]:
            seg = state.segments[change["seq"]]
            seg.migrated_from = {"to_node": change["from_node"], "to_party": change["from_party"]}
            seg.plan = SegmentPlan(
                seq=seg.plan.seq, seg_type=seg.plan.seg_type, carrier=seg.plan.carrier,
                from_party=seg.plan.from_party, to_party=change["to_party"],
                from_node=seg.plan.from_node, to_node=change["to_node"],
                delivers_to_tourist=seg.plan.delivers_to_tourist, expected_by=seg.plan.expected_by,
            )
        # 迁移到的最后一段即新最终目的地；旧目的地保留在各段 migrated_from 中。
        last_seq = max(change["seq"] for change in p["changes"])
        state.frozen = {
            **state.frozen,
            "destination": state.segments[last_seq].plan.to_node,
            "window_end": _dt(p["new_window_end"]),
        }
        state.reroutes.append({"case_id": event.aggregate_id, "kind": "migrated", **p})

    elif et == REDISPATCH_ISSUED:
        seq = p["new_seq"]
        plan = SegmentPlan(
            seq=seq, seg_type="relay", carrier=p["carrier"],
            from_party=p["from_party"], to_party=p["to_party"],
            from_node=p["from_node"], to_node=p["new_destination_node"],
            delivers_to_tourist=p.get("delivers_to_tourist", False),
        )
        state.segments[seq] = SegmentState(plan=plan, redispatch_of=p["in_transit_seq"])
        state.reroutes.append({"case_id": event.aggregate_id, "kind": "redispatch", **p})

    elif et == EXCEPTION_OPENED:
        state.exceptions[p["exception_id"]] = ExceptionState(
            exception_id=p["exception_id"], kind=p["kind"], seq=p.get("seq"),
            opened_at=event.occurred_at,
        )

    elif et == INQUIRY_OPENED:
        state.exceptions[p["exception_id"]].inquiry = True

    elif et == EXCEPTION_UPGRADED:
        state.exceptions[p["exception_id"]].escalated = True

    elif et == TEMP_STORAGE_OPENED:
        exc = state.exceptions[p["exception_id"]]
        exc.storage = True

    elif et == TEMP_STORAGE_RELEASED:
        exc = state.exceptions[p["exception_id"]]
        exc.storage = False
        exc.closed = True

    elif et == LIABILITY_DETERMINED:
        state.exceptions[p["exception_id"]].liability = {
            "responsible_party": p["responsible_party"],
            "failed_handover": p["failed_handover"],
            "reviewer_id": p["reviewer_id"],
            "basis": p["basis"],
            "at": event.occurred_at,
        }

    elif et == SETTLEMENT_ISSUED:
        if p["kind"] == "compensation":
            state.exceptions[p["exception_id"]].compensation = {
                "amount": p["amount"], "reviewer_id": p["reviewer_id"],
                "liability_ref": p["liability_ref"], "at": event.occurred_at,
            }
        elif p["kind"] == "segment_fee":
            state.segments[p["seq"]].fee_settled = p

    return state


def replay(events: list[Event]) -> OrderState:
    state: OrderState | None = None
    for event in events:
        state = fold(state, event)
    return state or OrderState()


# ---- 决策 --------------------------------------------------------------------

class IdFactory:
    """从流内既有标识派生确定性的新标识，跨命令不撞号。"""

    _PATTERN = re.compile(r"^(.+?)-(\d+)$")

    def __init__(self, events: list[Event]):
        self._max: dict[str, int] = {}
        # event_id 与 aggregate_id 都可能承载序号（如 transfer-001 / exc-001）。
        for event in events:
            for candidate in (event.event_id, event.aggregate_id):
                match = self._PATTERN.match(candidate)
                if match:
                    prefix, number = match.groups()
                    self._max[prefix] = max(self._max.get(prefix, 0), int(number))

    def new(self, prefix: str) -> str:
        number = self._max.get(prefix, 0) + 1
        self._max[prefix] = number
        return f"{prefix}-{number:03d}"


def decide_accept(cmd: AcceptOrder, state: OrderState | None) -> list[Event]:
    """受理：冻结受理要素。

    同一凭证完全重传 → 返回空（不产生第二件行李）；
    编号相同而封签/目的地/件数变化 → 立即进入人工核对。
    """
    state = state or OrderState()
    if state.accepted:
        frozen = state.frozen
        same = (
            frozen["piece_count"] == cmd.piece_count
            and tuple(frozen["seal_ids"]) == tuple(cmd.seal_ids)
            and frozen["destination"] == cmd.route[-1].to_node
        )
        if same:
            return []  # 幂等重传
        review_id = f"review-{cmd.order_id}-{len(state.reviews) + 1}"
        return [Event(
            event_id=f"{review_id}:opened", event_type=MANUAL_REVIEW_OPENED,
            aggregate_type=AGG_REVIEW, aggregate_id=review_id,
            occurred_at=cmd.occurred_at, version=0,
            summary="凭证编号重复但封签/目的地/件数不一致，转人工核对",
            payload={
                "order_id": cmd.order_id,
                "differences": _diff_frozen(frozen, cmd),
                "incoming": {
                    "piece_count": cmd.piece_count, "seal_ids": list(cmd.seal_ids),
                    "destination": cmd.route[-1].to_node,
                },
            },
        )]

    if cmd.piece_count <= 0:
        raise DomainError("受理件数必须为正整数")
    if not cmd.seal_ids:
        raise DomainError("每件行李必须有封签编号")
    if cmd.window_end <= cmd.window_start:
        raise DomainError("期望送达窗结束时间必须晚于开始时间")
    if not cmd.authorized_recipients:
        raise DomainError("至少登记一名授权收件人")
    if not cmd.route:
        raise DomainError("至少规划一段运输")

    route = [
        {
            "seq": seg.seq, "seg_type": seg.seg_type, "carrier": seg.carrier,
            "from_party": seg.from_party, "to_party": seg.to_party,
            "from_node": seg.from_node, "to_node": seg.to_node,
            "delivers_to_tourist": seg.delivers_to_tourist,
            "expected_by": seg.expected_by.isoformat() if seg.expected_by else None,
        }
        for seg in cmd.route
    ]
    final = cmd.route[-1]
    return [Event(
        event_id=cmd.event_id, event_type=ORDER_ACCEPTED,
        aggregate_type=AGG_ORDER, aggregate_id=cmd.order_id,
        occurred_at=cmd.occurred_at, version=0,
        summary=f"受理 {cmd.piece_count} 件行李，冻结送达窗与授权收件人",
        payload={
            "order_id": cmd.order_id,
            "tourist_ref": cmd.tourist_ref,
            "piece_count": cmd.piece_count,
            "seal_ids": list(cmd.seal_ids),
            "appearance": cmd.appearance,
            "privacy_hints": cmd.privacy_hints,
            "final_destination": final.to_node,
            "window_start": cmd.window_start.isoformat(),
            "window_end": cmd.window_end.isoformat(),
            "authorized_recipients": sorted(cmd.authorized_recipients),
            "initial_custodian": cmd.route[0].from_party,
            "route": route,
        },
        command_id=cmd.command_id,
    )]


def _diff_frozen(frozen: dict, cmd: AcceptOrder) -> list[str]:
    differences = []
    if frozen["piece_count"] != cmd.piece_count:
        differences.append("piece_count")
    if tuple(frozen["seal_ids"]) != tuple(cmd.seal_ids):
        differences.append("seal_ids")
    if frozen["destination"] != cmd.route[-1].to_node:
        differences.append("destination")
    return differences


def decide_scan(cmd: SubmitScan, state: OrderState, ids: IdFactory) -> list[Event]:
    """登记扫描回执；双方齐备才转移保管责任。

    封签/目的地/件数与受理冻结值不符的扫描立即转人工核对，且不转移责任。
    """
    if not state.accepted:
        raise DomainError("凭证尚未受理")
    if state.block_reason:
        raise DomainError(f"行李处于人工核对冻结（{state.block_reason}），暂停交接")
    seg = state.segments.get(cmd.seq)
    if seg is None:
        raise DomainError(f"段 {cmd.seq} 不存在")
    if cmd.endpoint not in (START, END):
        raise DomainError("endpoint 必须为 start 或 end")

    # 编号相同但封签/目的地/件数变化 → 人工核对，责任不转移。
    mismatch = []
    if set(cmd.seal_ids) != set(state.frozen["seal_ids"]):
        mismatch.append("seal_ids")
    if cmd.piece_count != state.frozen["piece_count"]:
        mismatch.append("piece_count")
    expected_destination = seg.plan.to_node
    if cmd.destination != expected_destination:
        mismatch.append("destination")
    if mismatch:
        review_id = ids.new("review")
        return [Event(
            event_id=f"{cmd.receipt_id}:review", event_type=MANUAL_REVIEW_OPENED,
            aggregate_type=AGG_REVIEW, aggregate_id=review_id,
            occurred_at=cmd.scanned_at, version=0,
            summary=f"扫描与冻结信息不一致（{','.join(mismatch)}），转人工核对",
            payload={
                "order_id": cmd.order_id, "seq": cmd.seq,
                "receipt_id": cmd.receipt_id, "party": cmd.party,
                "differences": mismatch,
                "scanned": {"seal_ids": list(cmd.seal_ids), "piece_count": cmd.piece_count,
                            "destination": cmd.destination},
            },
        )]

    fixed_parties, any_of = _endpoint_parties(seg, cmd.endpoint, state)
    allowed = fixed_parties | any_of
    if cmd.party not in allowed:
        raise DomainError(f"{cmd.endpoint} 交接只允许 {sorted(allowed)} 扫描，收到 {cmd.party}")

    bucket = seg.start_scans if cmd.endpoint == START else seg.end_scans
    if cmd.party in bucket:
        # 同一本地回执完全重传不产生第二条扫描。
        if bucket[cmd.party][0] == cmd.receipt_id:
            return []
        raise DomainError(f"{cmd.party} 已在该交接点扫描过")

    # 段序约束：前一段完成才能开始本段。
    if cmd.endpoint == START and cmd.seq > 1:
        prev = state.segments.get(cmd.seq - 1)
        if prev is None or prev.state != COMPLETED:
            raise DomainError("前序运输段尚未完成，不能发运")
    if cmd.endpoint == END and seg.state != IN_TRANSIT:
        raise DomainError("该段尚未发运，不能进行到达交接")

    events = [Event(
        event_id=cmd.receipt_id, event_type=SCAN_RECORDED,
        aggregate_type=AGG_SEGMENT,
        aggregate_id=f"{cmd.order_id}-seg{cmd.seq:02d}",
        occurred_at=cmd.scanned_at, version=0,
        summary=f"{cmd.party} 在 {cmd.endpoint} 交接点扫描",
        payload={
            "order_id": cmd.order_id, "seq": cmd.seq, "endpoint": cmd.endpoint,
            "party": cmd.party, "receipt_id": cmd.receipt_id,
            "local_ref": cmd.receipt_id,
        },
    )]

    updated = dict(bucket)
    updated[cmd.party] = (cmd.receipt_id, cmd.scanned_at)
    scanned = set(updated)
    both_confirmed = fixed_parties <= scanned and (not any_of or bool(scanned & any_of))
    if both_confirmed:
        at = max(item[1] for party, item in updated.items() if party in allowed)
        if cmd.endpoint == END and seg.plan.delivers_to_tourist:
            recipient = next(iter(scanned & any_of))
            events.append(Event(
                event_id=ids.new("delivery"), event_type=DELIVERY_COMPLETED,
                aggregate_type=AGG_SEGMENT,
                aggregate_id=f"{cmd.order_id}-seg{cmd.seq:02d}",
                occurred_at=at, version=0,
                summary="双方确认，行李交付授权收件人",
                payload={
                    "order_id": cmd.order_id, "seq": cmd.seq,
                    "carrier": seg.plan.carrier, "recipient": recipient,
                    "handover": _handover_id(cmd.order_id, cmd.seq, END),
                },
            ))
        else:
            to_party = seg.plan.carrier if cmd.endpoint == START else seg.plan.to_party
            events.append(Event(
                event_id=ids.new("transfer"), event_type=CUSTODY_TRANSFERRED,
                aggregate_type=AGG_SEGMENT,
                aggregate_id=f"{cmd.order_id}-seg{cmd.seq:02d}",
                occurred_at=at, version=0,
                summary="双方扫描齐备，保管责任转移",
                payload={
                    "order_id": cmd.order_id, "seq": cmd.seq, "endpoint": cmd.endpoint,
                    "from_party": seg.plan.from_party if cmd.endpoint == START else seg.plan.carrier,
                    "to_party": to_party,
                    "handover": _handover_id(cmd.order_id, cmd.seq, cmd.endpoint),
                },
            ))
    return events


def _endpoint_parties(seg: SegmentState, endpoint: str, state: OrderState) -> tuple[set[str], set[str]]:
    """返回（必须扫描方集合, 至少扫描一方的集合）。"""
    if endpoint == START:
        return {seg.plan.from_party, seg.plan.carrier}, set()
    if seg.plan.delivers_to_tourist:
        # 承运商必须扫描，且授权收件人中至少一人扫描。
        return {seg.plan.carrier}, set(state.frozen["authorized_recipients"])
    return {seg.plan.carrier, seg.plan.to_party}, set()


def _handover_id(order_id: str, seq: int, endpoint: str) -> str:
    return f"{order_id}-seg{seq:02d}-{endpoint}"


def decide_reroute(cmd: RequestReroute, state: OrderState, ids: IdFactory) -> list[Event]:
    """改签：只迁移尚未发运的后续段；在途件通过追加改派单处理。

    * 在途段之后仍有未发运段 → 迁移这些段即可，在途段运抵原节点后自然接续；
    * 在途段已是最后一段，或行李已滞留节点（无在途段、无未发运段）
      → 追加一张改派单把行李接往新目的地。
    """
    if not state.accepted:
        raise DomainError("凭证尚未受理")
    if state.delivered:
        raise DomainError("行李已交付，不能改签")
    active = state.active_seq
    changes = []
    for seq in sorted(state.segments):
        seg = state.segments[seq]
        if seg.state == PLANNED and (active is None or seq > active):
            changes.append({
                "seq": seq,
                "from_node": seg.plan.to_node,
                "from_party": seg.plan.to_party,
                # 交付给游客的末段迁移目的地时，收件方（授权游客）不变。
                "to_node": cmd.new_destination_node,
                "to_party": seg.plan.to_party if seg.plan.delivers_to_tourist
                else f"node:{cmd.new_destination_node}",
            })
    events: list[Event] = [Event(
        event_id=ids.new("reroute-req"), event_type=REROUTE_REQUESTED,
        aggregate_type=AGG_REROUTE, aggregate_id=cmd.case_id,
        occurred_at=cmd.at, version=0, summary="游客改签，请求变更目的地",
        payload={
            "order_id": cmd.order_id,
            "new_destination_node": cmd.new_destination_node,
            "new_window_end": cmd.new_window_end.isoformat(),
            "reason": cmd.reason,
        },
    )]
    if changes:
        events.append(Event(
            event_id=ids.new("reroute-apply"), event_type=REROUTE_APPLIED,
            aggregate_type=AGG_REROUTE, aggregate_id=cmd.case_id,
            occurred_at=cmd.at, version=0,
            summary=f"改签迁移 {len(changes)} 个尚未发运段",
            payload={
                "order_id": cmd.order_id, "changes": changes,
                "new_window_end": cmd.new_window_end.isoformat(),
            },
        ))
    else:
        # 没有可迁移的未发运段：在途件或滞留件追加改派单。
        if active is not None:
            seg_ref = state.segments[active]
            in_transit_seq, from_party, from_node = (
                active, seg_ref.plan.to_party, seg_ref.plan.to_node)
            summary = "行李已在途，待运抵原目的地节点后按改派单接续"
        else:
            in_transit_seq, from_party, from_node = 0, state.current_custodian, state.current_custodian
            summary = "行李滞留节点，追加改派单接往新目的地"
        new_seq = max(state.segments) + 1
        events.append(Event(
            event_id=ids.new("redispatch"), event_type=REDISPATCH_ISSUED,
            aggregate_type=AGG_REROUTE, aggregate_id=cmd.case_id,
            occurred_at=cmd.at, version=0, summary=summary,
            payload={
                "order_id": cmd.order_id, "in_transit_seq": in_transit_seq,
                "new_seq": new_seq, "carrier": "carrier:relay",
                "from_party": from_party, "from_node": from_node,
                "to_party": f"node:{cmd.new_destination_node}",
                "new_destination_node": cmd.new_destination_node,
            },
        ))
    return events


def decide_damage(cmd: ReportDamage, state: OrderState, ids: IdFactory) -> list[Event]:
    if cmd.exception_id in state.exceptions:
        return []
    return [Event(
        event_id=ids.new("exception"), event_type=EXCEPTION_OPENED,
        aggregate_type=AGG_EXCEPTION, aggregate_id=cmd.exception_id,
        occurred_at=cmd.at, version=0, summary="行李破损异常立案",
        payload={
            "order_id": cmd.order_id, "exception_id": cmd.exception_id,
            "kind": "damage", "seq": cmd.seq,
            "observed_by": cmd.observed_by, "note": cmd.note,
        },
    )]


def decide_liability(cmd: DetermineLiability, state: OrderState, ids: IdFactory) -> list[Event]:
    exc = state.exceptions.get(cmd.exception_id)
    if exc is None:
        raise DomainError("异常尚未立案，不能认定责任")
    if exc.liability is not None:
        return []
    return [Event(
        event_id=ids.new("liability"), event_type=LIABILITY_DETERMINED,
        aggregate_type=AGG_SETTLEMENT, aggregate_id=cmd.exception_id,
        occurred_at=cmd.at, version=0,
        summary="责任认定完成，待独立赔付审核",
        payload={
            "order_id": cmd.order_id, "exception_id": cmd.exception_id,
            "responsible_party": cmd.responsible_party,
            "failed_handover": cmd.failed_handover,
            "reviewer_id": cmd.reviewer_id, "basis": cmd.basis,
        },
    )]


def decide_compensation(cmd: IssueCompensation, state: OrderState, ids: IdFactory) -> list[Event]:
    """赔付审核：必须先有责任认定，且审核人与认定人分离。

    赔付结案事件引用原交接事实标识，但绝不修改或覆盖交接事件本身。
    """
    exc = state.exceptions.get(cmd.exception_id)
    if exc is None:
        raise DomainError("异常尚未立案")
    if exc.liability is None:
        raise DomainError("尚未完成责任认定，不能先行赔付")
    if exc.liability["reviewer_id"] == cmd.reviewer_id:
        raise DomainError("赔付审核人与责任认定人必须相互分离")
    if exc.compensation is not None:
        return []
    if cmd.amount < 0:
        raise DomainError("赔付金额不能为负")
    return [Event(
        event_id=ids.new("settlement"), event_type=SETTLEMENT_ISSUED,
        aggregate_type=AGG_SETTLEMENT, aggregate_id=cmd.exception_id,
        occurred_at=cmd.at, version=0,
        summary="赔付审核通过并结案（原交接事实保持不变）",
        payload={
            "order_id": cmd.order_id, "exception_id": cmd.exception_id,
            "kind": "compensation", "amount": cmd.amount,
            "reviewer_id": cmd.reviewer_id,
            "liability_ref": {
                "responsible_party": exc.liability["responsible_party"],
                "failed_handover": exc.liability["failed_handover"],
                "determined_by": exc.liability["reviewer_id"],
            },
        },
    )]


def decide_resolve_review(cmd: ResolveReview, state: OrderState, ids: IdFactory) -> list[Event]:
    if not any(r["review_id"] == cmd.review_id for r in state.reviews):
        raise DomainError("人工核对单不存在")
    if any(r["review_id"] == cmd.review_id and r.get("resolved") for r in state.reviews):
        return []
    return [Event(
        event_id=ids.new("review-resolve"), event_type=MANUAL_REVIEW_RESOLVED,
        aggregate_type=AGG_REVIEW, aggregate_id=cmd.review_id,
        occurred_at=cmd.at, version=0,
        summary=f"人工核对结论：{cmd.resolution}，交接冻结解除",
        payload={"order_id": cmd.order_id, "review_id": cmd.review_id,
                 "resolution": cmd.resolution, "note": cmd.note},
    )]


def decide_release(cmd: ReleaseStorage, state: OrderState, ids: IdFactory) -> list[Event]:
    exc = state.exceptions.get(cmd.exception_id)
    if exc is None or not exc.storage:
        raise DomainError("该异常未处于临时保管")
    if cmd.recipient not in state.frozen["authorized_recipients"]:
        raise DomainError("认领人不在授权收件人名单")
    return [Event(
        event_id=ids.new("release"), event_type=TEMP_STORAGE_RELEASED,
        aggregate_type=AGG_EXCEPTION, aggregate_id=cmd.exception_id,
        occurred_at=cmd.at, version=0, summary="授权收件人认领，解除临时保管",
        payload={"order_id": cmd.order_id, "exception_id": cmd.exception_id,
                 "recipient": cmd.recipient},
    )]


# ---- 时间驱动的检测（可注入时钟；重启重放不重复、不漏升级）-------------------

def run_time_detection(state: OrderState, now: datetime, ids: IdFactory) -> list[Event]:
    """依据当前时间检测：在途超时→查询；到达后无人领取→临时保管；查询逾期→升级。

    全部转换由状态幂等守卫，服务重启后重复执行不会产生重复事件。
    """
    events: list[Event] = []
    if not state.accepted or state.delivered:
        return events

    active = state.active_seq
    window_end = state.frozen["window_end"]
    if now > window_end:
        if _all_arrived(state) and not _has_open(state, "unclaimed"):
            exc_id = ids.new("exc")
            events.append(_exception(state.order_id, exc_id, "unclaimed",
                                     max(state.segments), now,
                                     "已到达但授权收件人未领取，无人领取立案"))
            events.append(Event(
                event_id=ids.new("storage"), event_type=TEMP_STORAGE_OPENED,
                aggregate_type=AGG_EXCEPTION, aggregate_id=exc_id,
                occurred_at=now, version=0, summary="行李转入临时保管",
                payload={"order_id": state.order_id, "exception_id": exc_id,
                         "custodian": state.current_custodian},
            ))
        elif not _all_arrived(state) and not _has_open(state, "timeout"):
            # 在途或停滞在某节点未发运，均按超时查询处理。
            seq = active if active is not None else min(
                seq for seq, seg in state.segments.items() if seg.state != COMPLETED)
            exc_id = ids.new("exc")
            events.append(_exception(state.order_id, exc_id, "timeout", seq, now,
                                     "超过承诺送达窗仍未交付，超时立案"))
            events.append(Event(
                event_id=ids.new("inquiry"), event_type=INQUIRY_OPENED,
                aggregate_type=AGG_EXCEPTION, aggregate_id=exc_id,
                occurred_at=now, version=0, summary="超时触发查询",
                payload={"order_id": state.order_id, "exception_id": exc_id},
            ))

    for exc in state.exceptions.values():
        if exc.inquiry and not exc.escalated and not exc.closed and exc.compensation is None:
            if now - exc.opened_at >= INQUIRY_ESCALATION:
                events.append(Event(
                    event_id=ids.new("upgrade"), event_type=EXCEPTION_UPGRADED,
                    aggregate_type=AGG_EXCEPTION, aggregate_id=exc.exception_id,
                    occurred_at=now, version=0,
                    summary="查询逾期未结，升级处理",
                    payload={"order_id": state.order_id, "exception_id": exc.exception_id},
                ))
    return events


def _has_open(state: OrderState, kind: str) -> bool:
    return any(e.kind == kind and not e.closed for e in state.exceptions.values())


def _all_arrived(state: OrderState) -> bool:
    return bool(state.segments) and all(s.state == COMPLETED for s in state.segments.values())


def _exception(order_id: str, exc_id: str, kind: str, seq: int, at: datetime, summary: str) -> Event:
    return Event(
        event_id=f"{exc_id}-open", event_type=EXCEPTION_OPENED,
        aggregate_type=AGG_EXCEPTION, aggregate_id=exc_id,
        occurred_at=at, version=0, summary=summary,
        payload={"order_id": order_id, "exception_id": exc_id, "kind": kind, "seq": seq},
    )


# ---- 分段结算（跨午夜 / 节假日，可注入时钟与日历）----------------------------

def price_segment(seg: SegmentState, calendar: HolidayCalendar) -> dict[str, Any]:
    """依据该段实际交接事实重算服务费。"""
    if seg.state != COMPLETED or seg.departed_at is None or seg.arrived_at is None:
        raise DomainError("仅完成的运输段可结算")
    base = BASE_FEE.get(seg.plan.seg_type, BASE_FEE["relay"])
    cross_midnight = _midnights_between(seg.departed_at, seg.arrived_at)
    amount = base
    breakdown = {"base": base, "cross_midnight_count": cross_midnight,
                 "cross_midnight_fee": 0, "holiday_fee": 0}
    if cross_midnight:
        breakdown["cross_midnight_fee"] = CROSS_MIDNIGHT_FEE * cross_midnight
        amount += breakdown["cross_midnight_fee"]
    if _spans_holiday(seg.departed_at, seg.arrived_at, calendar):
        breakdown["holiday_fee"] = HOLIDAY_FEE
        amount += HOLIDAY_FEE
    breakdown["amount"] = amount
    return breakdown


def settle_segment_event(order_id: str, seq: int, seg: SegmentState, calendar: HolidayCalendar,
                         ids: IdFactory, settled_at: datetime) -> Event:
    breakdown = price_segment(seg, calendar)
    return Event(
        event_id=ids.new("fee"), event_type=SETTLEMENT_ISSUED,
        aggregate_type=AGG_SETTLEMENT, aggregate_id=f"{order_id}-seg{seq:02d}-fee",
        occurred_at=settled_at, version=0,
        summary=f"第 {seq} 段服务费结算 {breakdown['amount']} 元",
        payload={
            "order_id": order_id, "seq": seq, "kind": "segment_fee",
            "amount": breakdown["amount"], "breakdown": breakdown,
            "departed_at": seg.departed_at.isoformat(),
            "arrived_at": seg.arrived_at.isoformat(),
        },
    )


def _midnights_between(start: datetime, end: datetime) -> int:
    start_local = _zoned(start).astimezone(CN_TZ)
    end_local = _zoned(end).astimezone(CN_TZ)
    start_day = start_local.date()
    end_day = end_local.date()
    return max((end_day - start_day).days, 0)


def _spans_holiday(start: datetime, end: datetime, calendar: HolidayCalendar) -> bool:
    from datetime import timedelta as td
    start_local = _zoned(start).astimezone(CN_TZ)
    end_local = _zoned(end).astimezone(CN_TZ)
    day = start_local.date()
    while day <= end_local.date():
        if calendar.is_holiday(datetime.combine(day, datetime.min.time(), CN_TZ)):
            return True
        day += td(days=1)
    return False


def _zoned(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=CN_TZ)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
