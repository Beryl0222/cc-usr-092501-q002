"""应用服务：命令编排、角色授权、最小可见视图与监管整链重放。

承运商只看到当前一段所需信息；酒店（交接节点）看不到完整行程；
游客可查询当前位置与预计到达；监管可重放全链条并核对每段付款。
赔付审核人与责任认定人的相互分离在领域层强制，这里再加角色门禁。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .clock import Clock, HolidayCalendar
from .domain import (
    COMPLETED, PLANNED, AcceptOrder, DetermineLiability, IdFactory,
    IssueCompensation, ReleaseStorage, ReportDamage, RequestReroute, ResolveReview,
    SegmentState, SubmitScan, decide_accept, decide_compensation, decide_damage,
    decide_liability, decide_reroute, decide_release, decide_resolve_review,
    decide_scan, price_segment, replay, run_time_detection, settle_segment_event,
)
from .events import (
    CUSTODY_TRANSFERRED, DELIVERY_COMPLETED, DomainError, Event,
)
from .store import EventStore

ROLE_TOURIST = "tourist"
ROLE_CARRIER = "carrier"
ROLE_HOTEL = "hotel"            # 酒店前台、游客中心等交接节点
ROLE_LIABILITY_OFFICER = "liability_officer"
ROLE_COMPENSATION_AUDITOR = "compensation_auditor"
ROLE_STAFF = "staff"           # 受理、改签登记、人工核对
ROLE_REGULATOR = "regulator"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str


class LedgerService:
    def __init__(self, store: EventStore, clock: Clock, calendar: HolidayCalendar | None = None):
        self.store = store
        self.clock = clock
        self.calendar = calendar or HolidayCalendar()

    # ---- 内部编排 ----------------------------------------------------------

    def _load(self, order_id: str):
        events = self.store.order_stream(order_id)
        # ID 序号需在全库范围内播种：event_id 全局唯一，跨凭证不得撞号。
        return events, replay(events), IdFactory(self.store.all_events())

    # ---- 命令 --------------------------------------------------------------

    def accept_order(self, cmd: AcceptOrder, actor: Actor, command_id: str | None = None) -> list[Event]:
        self._require(actor, ROLE_STAFF)
        return self.store.append(
            decide_accept(cmd, replay(self.store.order_stream(cmd.order_id))),
            command_id=command_id, request_key=_key(("accept", cmd)),
        )

    def submit_scan(self, cmd: SubmitScan, actor: Actor, command_id: str | None = None) -> list[Event]:
        events, state, _ = self._load(cmd.order_id)
        seg = state.segments[cmd.seq]
        if actor.role == ROLE_CARRIER:
            if actor.actor_id != seg.plan.carrier:
                raise DomainError("承运商只能扫描分配给本司的运输段")
        elif actor.role == ROLE_HOTEL:
            if actor.actor_id not in (seg.plan.from_party, seg.plan.to_party):
                raise DomainError("节点只能扫描与本节点交接的运输段")
        elif actor.role == ROLE_TOURIST:
            # 游客只在末段交付时作为接收方扫描，授权名单由领域层校验。
            if not (cmd.endpoint == "end" and seg.plan.delivers_to_tourist):
                raise DomainError("游客只能在行李交付时扫描签收")
        else:
            raise DomainError("扫描交接仅限承运商、交接节点或收件游客")
        _, _, ids = self._load(cmd.order_id)
        produced = decide_scan(cmd, state, ids)
        return self.store.append(produced, command_id=command_id, request_key=_key(("scan", cmd)))

    def request_reroute(self, cmd: RequestReroute, actor: Actor) -> list[Event]:
        self._require_any(actor, (ROLE_STAFF, ROLE_TOURIST))
        _, state, ids = self._load(cmd.order_id)
        return self.store.append(decide_reroute(cmd, state, ids))

    def report_damage(self, cmd: ReportDamage, actor: Actor) -> list[Event]:
        self._require_any(actor, (ROLE_CARRIER, ROLE_HOTEL, ROLE_STAFF))
        _, state, ids = self._load(cmd.order_id)
        return self.store.append(decide_damage(cmd, state, ids))

    def determine_liability(self, cmd: DetermineLiability, actor: Actor) -> list[Event]:
        self._require(actor, ROLE_LIABILITY_OFFICER)
        # reviewer_id 必须就是操作者本人，便于后续与赔付审核人比对。
        if cmd.reviewer_id != actor.actor_id:
            raise DomainError("责任认定人必须为当前操作者")
        _, state, ids = self._load(cmd.order_id)
        return self.store.append(decide_liability(cmd, state, ids))

    def issue_compensation(self, cmd: IssueCompensation, actor: Actor) -> list[Event]:
        self._require(actor, ROLE_COMPENSATION_AUDITOR)
        if cmd.reviewer_id != actor.actor_id:
            raise DomainError("赔付审核人必须为当前操作者")
        _, state, ids = self._load(cmd.order_id)
        return self.store.append(decide_compensation(cmd, state, ids))

    def resolve_review(self, cmd: ResolveReview, actor: Actor) -> list[Event]:
        self._require(actor, ROLE_STAFF)
        _, state, ids = self._load(cmd.order_id)
        return self.store.append(decide_resolve_review(cmd, state, ids))

    def release_storage(self, cmd: ReleaseStorage, actor: Actor) -> list[Event]:
        self._require_any(actor, (ROLE_STAFF, ROLE_HOTEL))
        _, state, ids = self._load(cmd.order_id)
        return self.store.append(decide_release(cmd, state, ids))

    def settle_segment(self, order_id: str, seq: int, actor: Actor) -> list[Event]:
        self._require(actor, ROLE_STAFF)
        _, state, ids = self._load(order_id)
        seg = state.segments[seq]
        if seg.fee_settled is not None:
            return []
        event = settle_segment_event(order_id, seq, seg, self.calendar, ids, self.clock.now())
        return self.store.append([event])

    def run_due_detections(self, order_id: str) -> list[Event]:
        """定时扫描：超时立案/查询、无人领取临时保管、逾期升级。

        转换由状态幂等守卫，事件标识由全库事件重播种的序号确定性派生；
        服务崩溃重启后重跑，已发生的转换跳过（不重复转运/升级），
        到点而未记录的转换照常补出（不漏逾期升级），无需额外命令去重。
        """
        _, state, ids = self._load(order_id)
        return self.store.append(run_time_detection(state, self.clock.now(), ids))

    # ---- 角色门禁 ----------------------------------------------------------

    @staticmethod
    def _require(actor: Actor, role: str) -> None:
        if actor.role != role:
            raise DomainError(f"该操作需要 {role} 角色")

    @staticmethod
    def _require_any(actor: Actor, roles: tuple[str, ...]) -> None:
        if actor.role not in roles:
            raise DomainError(f"该操作需要以下角色之一：{roles}")

    # ---- 查询视图（按角色裁剪）--------------------------------------------

    def tourist_view(self, order_id: str, actor: Actor) -> dict[str, Any]:
        self._require(actor, ROLE_TOURIST)
        _, state, _ = self._load(order_id)
        self._ensure_loaded(state)
        location, eta = _locate(state)
        return {
            "order_id": order_id,
            "status": _overall_status(state),
            "current_location": location,
            "eta": eta.isoformat() if eta else None,
            "window_end": state.frozen["window_end"].isoformat(),
            "current_custodian": state.current_custodian,
            "delivered": state.delivered,
            "delivered_at": state.delivered_at.isoformat() if state.delivered_at else None,
            "open_exceptions": [
                {"exception_id": e.exception_id, "kind": e.kind,
                 "escalated": e.escalated, "in_storage": e.storage}
                for e in state.exceptions.values() if not e.closed
            ],
            "pending_manual_review": bool(state.block_reason),
        }

    def carrier_view(self, order_id: str, actor: Actor) -> dict[str, Any]:
        """承运商视图：仅当前一段（在途段或下一段）所需信息。"""
        self._require(actor, ROLE_CARRIER)
        _, state, _ = self._load(order_id)
        self._ensure_loaded(state)
        target_seq = state.active_seq or _next_planned_seq(state)
        if target_seq is None:
            return {"order_id": order_id, "segment": None}
        seg = state.segments[target_seq]
        if seg.plan.carrier != actor.actor_id:
            raise DomainError("当前运输段不属于该承运商，无可显示信息")
        return {
            "order_id": order_id,
            "segment": {
                "seq": seg.plan.seq,
                "state": seg.state,
                "from_node": seg.plan.from_node,
                "to_node": seg.plan.to_node,
                "expected_by": seg.plan.expected_by.isoformat() if seg.plan.expected_by else None,
                "piece_count": state.frozen["piece_count"],
                "seal_ids": list(state.frozen["seal_ids"]),
                "start_confirmed": sorted(seg.start_scans),
                "end_confirmed": sorted(seg.end_scans),
            },
        }

    def hotel_view(self, order_id: str, actor: Actor) -> dict[str, Any]:
        """节点视图：仅与本节点有关的交接，不含完整行程。"""
        self._require(actor, ROLE_HOTEL)
        _, state, _ = self._load(order_id)
        self._ensure_loaded(state)
        visible = []
        for seq in sorted(state.segments):
            seg = state.segments[seq]
            if actor.actor_id not in (seg.plan.from_party, seg.plan.to_party):
                continue
            visible.append({
                "seq": seg.plan.seq,
                "state": seg.state,
                "node_role": "from" if actor.actor_id == seg.plan.from_party else "to",
                "piece_count": state.frozen["piece_count"],
                "seal_ids": list(state.frozen["seal_ids"]),
                "confirmed_by_me": (
                    actor.actor_id in seg.start_scans or actor.actor_id in seg.end_scans
                ),
            })
        return {"order_id": order_id, "handovers": visible,
                "held_now": state.current_custodian == actor.actor_id}

    def regulator_chain(self, order_id: str, actor: Actor) -> dict[str, Any]:
        """监管命令：重放某件行李的全链条，逐段列交接事实与付款。"""
        self._require(actor, ROLE_REGULATOR)
        events, state, _ = self._load(order_id)
        self._ensure_loaded(state)
        chain = []
        for seq in sorted(state.segments):
            seg = state.segments[seq]
            chain.append(_segment_record(seq, seg))
        payments = {
            "segment_fees": [
                seg.fee_settled for seg in state.segments.values() if seg.fee_settled
            ],
            "compensations": [
                {"exception_id": e.exception_id, **e.compensation}
                for e in state.exceptions.values() if e.compensation
            ],
            "liabilities": [
                {"exception_id": e.exception_id, **e.liability}
                for e in state.exceptions.values() if e.liability
            ],
        }
        return {
            "order_id": order_id,
            "frozen": {
                "piece_count": state.frozen["piece_count"],
                "seal_ids": list(state.frozen["seal_ids"]),
                "appearance": state.frozen["appearance"],
                "privacy_hints": state.frozen["privacy_hints"],
                "destination": state.frozen["destination"],
                "window_start": state.frozen["window_start"].isoformat(),
                "window_end": state.frozen["window_end"].isoformat(),
                "authorized_recipients": sorted(state.frozen["authorized_recipients"]),
            },
            "segments": chain,
            "reroutes": state.reroutes,
            "exceptions": [vars(e) for e in state.exceptions.values()],
            "payments": payments,
            "events": [event.to_dict() for event in events],
        }

    def verify_payments(self, order_id: str, actor: Actor) -> dict[str, Any]:
        """核对每段付款与交接事实：

        * 已完成段必须恰好结算一次，金额按交接时间与注入日历重算后一致；
        * 未完成段不得提前付款；
        * 赔付引用的失败交接点必须真实存在；赔付不能改写交接事件。
        """
        self._require(actor, ROLE_REGULATOR)
        report = self.regulator_chain(order_id, actor)
        problems: list[str] = []
        events, state, _ = self._load(order_id)
        handovers = {
            event.payload.get("handover")
            for event in events
            if event.event_type in (CUSTODY_TRANSFERRED, DELIVERY_COMPLETED)
        }
        for seq, seg in state.segments.items():
            fee = seg.fee_settled
            if seg.state == COMPLETED:
                if fee is None:
                    problems.append(f"第 {seq} 段已完成但缺少服务费结算")
                    continue
                expected = price_segment(seg, self.calendar)
                if fee["amount"] != expected["amount"]:
                    problems.append(
                        f"第 {seq} 段付款 {fee['amount']} 与重算 {expected['amount']} 不一致")
            elif fee is not None:
                problems.append(f"第 {seq} 段未完成却已付款")
        for comp in report["payments"]["compensations"]:
            failed = comp["liability_ref"]["failed_handover"]
            if failed not in handovers:
                problems.append(f"赔付 {comp['exception_id']} 引用的交接点 {failed} 不存在")
            if comp["liability_ref"]["determined_by"] == comp["reviewer_id"]:
                problems.append(f"赔付 {comp['exception_id']} 审核人与责任认定人未分离")
        return {"order_id": order_id, "ok": not problems, "problems": problems}

    @staticmethod
    def _ensure_loaded(state) -> None:
        if not state.accepted:
            raise DomainError("凭证尚未受理")


# ---- 视图辅助 ----------------------------------------------------------------

def _overall_status(state) -> str:
    if state.delivered:
        return "DELIVERED"
    if state.block_reason:
        return "MANUAL_REVIEW"
    if any(not e.closed for e in state.exceptions.values()):
        return "EXCEPTION"
    if state.active_seq is not None:
        return "IN_TRANSIT"
    return "ACCEPTED"


def _next_planned_seq(state) -> int | None:
    planned = [seq for seq, seg in state.segments.items() if seg.state == PLANNED]
    return min(planned) if planned else None


def _locate(state) -> tuple[str, datetime | None]:
    if state.delivered:
        return f"已交付：{state.current_custodian}", state.delivered_at
    active_seq = state.active_seq
    if active_seq is not None:
        seg = state.segments[active_seq]
        return (f"{seg.plan.from_node} → {seg.plan.to_node}（{seg.plan.carrier} 运送中）",
                seg.plan.expected_by or state.frozen["window_end"])
    nxt = _next_planned_seq(state)
    if nxt is not None:
        seg = state.segments[nxt]
        return f"暂存于 {state.current_custodian}，等待 {seg.plan.carrier} 发运", seg.plan.expected_by
    if state.segments and all(s.state == COMPLETED for s in state.segments.values()):
        return f"已到达 {state.current_custodian}，等待领取", state.frozen["window_end"]
    return state.current_custodian or "未知", state.frozen["window_end"]


def _segment_record(seq: int, seg: SegmentState) -> dict[str, Any]:
    return {
        "seq": seq,
        "seg_type": seg.plan.seg_type,
        "carrier": seg.plan.carrier,
        "from_node": seg.plan.from_node,
        "to_node": seg.plan.to_node,
        "state": seg.state,
        "start": {
            "scans": {party: rec[0] for party, rec in seg.start_scans.items()},
            "at": seg.departed_at.isoformat() if seg.departed_at else None,
        },
        "end": {
            "scans": {party: rec[0] for party, rec in seg.end_scans.items()},
            "at": seg.arrived_at.isoformat() if seg.arrived_at else None,
        },
        "migrated_from": seg.migrated_from,
        "redispatch_of": seg.redispatch_of,
    }


def _key(parts: tuple) -> str:
    def default(value):
        if hasattr(value, "__dataclass_fields__"):
            return {k: getattr(value, k) for k in value.__dataclass_fields__}
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    return json.dumps(parts, ensure_ascii=False, default=default, sort_keys=True)
