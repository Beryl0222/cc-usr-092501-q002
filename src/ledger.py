"""便民行李接力履约账的领域服务。

所有事实以事件形式追加到账簿（见 ``store.EventStore``），内存状态由事件
折叠而成：服务重启后重放同一账簿即可恢复，既不重复转运，也不漏掉逾期升级。

核心规则：
- 受理时冻结件数、外观摘要、隐私化物品提示、期望送达窗和授权收件人；
- 每段运输只有交接双方扫描都登记后才转移保管责任；
- 同一凭证完全重传不产生第二件行李；编号相同而封签、目的地或件数变化
  立即转入人工核对；
- 改签只能迁移尚未发运的后续段，已在途行李通过追加改派单处理；
- 超时、破损、无人领取分别触发查询、赔付、临时保管；赔付结案只是新事件，
  不覆盖原交接事实；
- 承运商只能看到当前一段所需信息，酒店读不到完整行程，赔付审核人与责任
  认定人相互分离。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from .clock import Clock, iso, parse_iso
from .envelope import validate_event
from .fees import segment_fee
from .store import EventStore

# 事件类型（与 contracts/domain.schema.json 的枚举保持一致）
ORDER_ACCEPTED = "ORDER_ACCEPTED"
ORDER_DELIVERED = "ORDER_DELIVERED"
LUGGAGE_COLLECTED = "LUGGAGE_COLLECTED"
SEGMENT_PLANNED = "SEGMENT_PLANNED"
SEGMENT_DISPATCHED = "SEGMENT_DISPATCHED"
SEGMENT_CANCELLED = "SEGMENT_CANCELLED"
HANDOVER_SCAN_RECORDED = "HANDOVER_SCAN_RECORDED"
CUSTODY_TRANSFERRED = "CUSTODY_TRANSFERRED"
MANUAL_REVIEW_OPENED = "MANUAL_REVIEW_OPENED"
REROUTE_REQUESTED = "REROUTE_REQUESTED"
REROUTE_APPLIED = "REROUTE_APPLIED"
REROUTE_ORDER_APPENDED = "REROUTE_ORDER_APPENDED"
EXCEPTION_OPENED = "EXCEPTION_OPENED"
STORAGE_OPENED = "STORAGE_OPENED"
CLAIM_OPENED = "CLAIM_OPENED"
CLAIM_LIABILITY_ASSESSED = "CLAIM_LIABILITY_ASSESSED"
CLAIM_SETTLED = "CLAIM_SETTLED"
SETTLEMENT_ISSUED = "SETTLEMENT_ISSUED"

# 交接扫描的角色约定：取件由交出方与承运商确认，送达由承运商与接收方确认
_PHASE_ROLES = {
    "pickup": ("sender", "carrier"),
    "delivery": ("carrier", "receiver"),
}
_PHASE_REQUIRED_STATUS = {
    "pickup": "DISPATCHED",
    "delivery": "IN_TRANSIT",
}


class DomainError(Exception):
    """违反领域规则时抛出。"""


@dataclass
class ScanRecord:
    actor_role: str
    actor_id: str
    occurred_at: str


@dataclass
class SegmentState:
    segment_id: str
    order_id: str
    seq: int
    carrier_id: str
    from_point: str
    to_point: str
    window_start: str
    window_end: str
    reroute_case_id: str | None = None
    status: str = "PLANNED"  # PLANNED/DISPATCHED/IN_TRANSIT/DELIVERED/CANCELLED
    pickup_scans: dict[str, ScanRecord] = field(default_factory=dict)
    delivery_scans: dict[str, ScanRecord] = field(default_factory=dict)


@dataclass
class OrderState:
    order_id: str
    voucher_id: str
    fingerprint: str
    seal_no: str
    destination: str
    piece_count: int
    appearance_summary: str
    content_hints: list[str]
    window_start: str
    window_end: str
    authorized_recipients: list[str]
    current_holder: str
    current_point: str
    segment_ids: list[str] = field(default_factory=list)
    status: str = "ACCEPTED"  # ACCEPTED/IN_TRANSIT/DELIVERED/IN_STORAGE/COLLECTED
    delivered_at: str | None = None
    collected_by: str | None = None
    manual_review_open: bool = False


@dataclass
class ExceptionState:
    exception_id: str
    kind: str  # timeout/damage/unclaimed
    order_id: str
    opened_at: str
    segment_id: str | None = None
    action: str | None = None  # inquiry/claim/temporary_storage
    status: str = "OPEN"


@dataclass
class ClaimState:
    claim_id: str
    order_id: str
    exception_id: str
    segment_id: str
    status: str = "OPEN"  # OPEN/SETTLED
    assessor_id: str | None = None
    liable_segment_id: str | None = None
    amount_cents: int | None = None
    settled_by: str | None = None


def _norm_ts(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return iso(value)
    return iso(parse_iso(value))


def _normalize_legs(legs: Iterable[dict]) -> list[dict]:
    normalized = []
    for index, leg in enumerate(legs, start=1):
        try:
            item = {
                "carrier_id": str(leg["carrier_id"]),
                "from_point": str(leg["from_point"]),
                "to_point": str(leg["to_point"]),
                "window_start": _norm_ts(leg["window_start"]),
                "window_end": _norm_ts(leg["window_end"]),
            }
        except KeyError as error:
            raise DomainError(f"第 {index} 段缺少字段：{error}") from None
        if parse_iso(item["window_start"]) >= parse_iso(item["window_end"]):
            raise DomainError(f"第 {index} 段承诺送达窗无效")
        normalized.append(item)
    if not normalized:
        raise DomainError("至少规划一段运输")
    for previous, following in zip(normalized, normalized[1:]):
        if previous["to_point"] != following["from_point"]:
            raise DomainError("行程段必须首尾相接")
    return normalized


def _fingerprint(
    *,
    seal_no: str,
    destination: str,
    piece_count: int,
    appearance_summary: str,
    content_hints: list[str],
    window_start: str,
    window_end: str,
    authorized_recipients: list[str],
    legs: list[dict],
) -> str:
    canonical = json.dumps(
        {
            "seal_no": seal_no,
            "destination": destination,
            "piece_count": piece_count,
            "appearance_summary": appearance_summary,
            "content_hints": sorted(content_hints),
            "window_start": window_start,
            "window_end": window_end,
            "authorized_recipients": sorted(authorized_recipients),
            "legs": legs,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RelayLedger:
    """行李接力履约账：命令写事件，查询读折叠状态。"""

    def __init__(
        self,
        store: EventStore,
        clock: Clock,
        *,
        holidays: Iterable[date] = (),
        unclaimed_grace: timedelta = timedelta(hours=24),
    ):
        self._store = store
        self._clock = clock
        self._holidays = frozenset(holidays)
        self._unclaimed_grace = unclaimed_grace
        self.orders: dict[str, OrderState] = {}
        self.segments: dict[str, SegmentState] = {}
        self.exceptions: dict[str, ExceptionState] = {}
        self.claims: dict[str, ClaimState] = {}
        self.settlements: dict[str, dict] = {}  # segment_id -> SETTLEMENT_ISSUED 事件
        self._voucher_index: dict[str, str] = {}
        self._reroute_counts: dict[str, int] = {}
        self._event_count = 0
        for event in self._store.load():
            self._apply(event)

    # ------------------------------------------------------------------
    # 事件基础设施
    # ------------------------------------------------------------------

    def _now(self, occurred_at: datetime | str | None) -> str:
        return _norm_ts(occurred_at) if occurred_at is not None else iso(self._clock.now())

    def _next_event_id(self) -> str:
        candidate = self._event_count + 1
        while f"evt-{candidate:08d}" in self._store:
            candidate += 1
        return f"evt-{candidate:08d}"

    def _emit(self, event_type, aggregate_type, aggregate_id, occurred_at, summary, **payload):
        event_id = payload.pop("event_id", None) or self._next_event_id()
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": 1,
            "summary": summary,
            **payload,
        }
        errors = validate_event(event)
        if errors:
            raise DomainError("内部事件未通过信封校验：" + "；".join(errors))
        if self._store.append(event):
            self._apply(event)
        return event

    def _apply(self, event: dict) -> None:
        handler = getattr(self, f"_on_{event['event_type'].lower()}", None)
        if handler is not None:
            handler(event)
        self._event_count += 1

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def accept_order(
        self,
        *,
        voucher_id: str,
        seal_no: str,
        destination: str,
        piece_count: int,
        appearance_summary: str,
        content_hints: list[str],
        window_start: datetime | str,
        window_end: datetime | str,
        authorized_recipients: list[str],
        legs: list[dict],
        occurred_at: datetime | str | None = None,
    ) -> str:
        """受理行李接力委托，冻结件数、外观、物品提示、送达窗与授权收件人。

        同一凭证完全重传返回原单号；编号相同而封签、目的地或件数变化转入人工核对。
        """
        if not isinstance(piece_count, int) or isinstance(piece_count, bool) or piece_count < 1:
            raise DomainError("件数必须是正整数")
        if not authorized_recipients:
            raise DomainError("至少登记一名授权收件人")
        occurred = self._now(occurred_at)
        window_start = _norm_ts(window_start)
        window_end = _norm_ts(window_end)
        if parse_iso(window_start) >= parse_iso(window_end):
            raise DomainError("期望送达窗无效")
        legs = _normalize_legs(legs)
        if legs[-1]["to_point"] != destination:
            raise DomainError("行程终点必须与目的地一致")
        fingerprint = _fingerprint(
            seal_no=seal_no,
            destination=destination,
            piece_count=piece_count,
            appearance_summary=appearance_summary,
            content_hints=list(content_hints),
            window_start=window_start,
            window_end=window_end,
            authorized_recipients=list(authorized_recipients),
            legs=legs,
        )
        existing_id = self._voucher_index.get(voucher_id)
        if existing_id is not None:
            existing = self.orders[existing_id]
            if existing.fingerprint == fingerprint:
                return existing_id  # 完全重传：不产生第二件行李
            if (
                existing.seal_no != seal_no
                or existing.destination != destination
                or existing.piece_count != piece_count
            ):
                if not existing.manual_review_open:
                    self._emit(
                        MANUAL_REVIEW_OPENED,
                        "luggage_order",
                        existing_id,
                        occurred,
                        "凭证号相同但封签、目的地或件数变化，转入人工核对",
                        order_id=existing_id,
                        voucher_id=voucher_id,
                    )
                raise DomainError("凭证号相同但封签、目的地或件数不一致，已进入人工核对")
            raise DomainError("凭证号相同但登记内容不一致")
        order_id = f"LO-{voucher_id}"
        self._emit(
            ORDER_ACCEPTED,
            "luggage_order",
            order_id,
            occurred,
            "受理行李接力委托，冻结件数与期望送达窗",
            order_id=order_id,
            voucher_id=voucher_id,
            seal_no=seal_no,
            destination=destination,
            piece_count=piece_count,
            appearance_summary=appearance_summary,
            content_hints=list(content_hints),
            window_start=window_start,
            window_end=window_end,
            authorized_recipients=list(authorized_recipients),
            fingerprint=fingerprint,
            origin_point=legs[0]["from_point"],
        )
        for seq, leg in enumerate(legs, start=1):
            self._emit(
                SEGMENT_PLANNED,
                "custody_segment",
                f"{order_id}-S{seq}",
                occurred,
                f"规划第 {seq} 段运输",
                order_id=order_id,
                seq=seq,
                **leg,
            )
        return order_id

    def dispatch_segment(self, segment_id: str, *, occurred_at: datetime | str | None = None) -> None:
        """承运商接单发运；重复调用幂等。"""
        seg = self._segment(segment_id)
        if seg.status == "DISPATCHED":
            return
        if seg.status != "PLANNED":
            raise DomainError(f"运输段当前状态为 {seg.status}，不能发运")
        order = self.orders[seg.order_id]
        if order.current_point != seg.from_point:
            raise DomainError("保管责任尚未到达该段起点，不能跳段发运")
        self._emit(
            SEGMENT_DISPATCHED,
            "custody_segment",
            segment_id,
            self._now(occurred_at),
            "承运商接单发运",
            order_id=seg.order_id,
            carrier_id=seg.carrier_id,
        )

    def record_scan(
        self,
        segment_id: str,
        *,
        phase: str,
        actor_role: str,
        actor_id: str,
        occurred_at: datetime | str | None = None,
        event_id: str | None = None,
    ) -> None:
        """登记一次交接扫描；同一角色重复扫描（含离线回执重传）幂等忽略。

        双方扫描齐备时按较晚的扫描发生时间转移保管责任，因此离线回执
        恢复后仍按真实发生时间入账。
        """
        seg = self._segment(segment_id)
        if phase not in _PHASE_ROLES:
            raise DomainError("phase 必须是 pickup 或 delivery")
        if actor_role not in _PHASE_ROLES[phase]:
            raise DomainError(f"{phase} 交接不接受角色 {actor_role}")
        scans = seg.pickup_scans if phase == "pickup" else seg.delivery_scans
        if actor_role in scans:
            return  # 同一角色重复扫描：幂等忽略
        required = _PHASE_REQUIRED_STATUS[phase]
        if seg.status != required:
            raise DomainError(f"运输段状态为 {seg.status}，不能登记 {phase} 扫描")
        self._emit(
            HANDOVER_SCAN_RECORDED,
            "custody_segment",
            segment_id,
            self._now(occurred_at),
            "交接扫描已登记",
            order_id=seg.order_id,
            phase=phase,
            actor_role=actor_role,
            actor_id=actor_id,
            event_id=event_id,
        )
        self._maybe_transfer(seg.segment_id, phase)

    def _maybe_transfer(self, segment_id: str, phase: str) -> None:
        seg = self.segments[segment_id]
        order = self.orders[seg.order_id]
        if phase == "pickup":
            scans, ready, to_holder = seg.pickup_scans, seg.status == "DISPATCHED", seg.carrier_id
        else:
            scans, ready, to_holder = seg.delivery_scans, seg.status == "IN_TRANSIT", seg.to_point
        if not ready or not all(role in scans for role in _PHASE_ROLES[phase]):
            return
        effective = max(parse_iso(scan.occurred_at) for scan in scans.values())
        self._emit(
            CUSTODY_TRANSFERRED,
            "custody_segment",
            segment_id,
            iso(effective),
            "双方扫描确认，保管责任转移",
            order_id=seg.order_id,
            phase=phase,
            from_holder=order.current_holder,
            to_holder=to_holder,
        )
        if phase == "delivery":
            self._maybe_finish_order(seg.segment_id)

    def _maybe_finish_order(self, segment_id: str) -> None:
        seg = self.segments[segment_id]
        order = self.orders[seg.order_id]
        active = [
            self.segments[sid]
            for sid in order.segment_ids
            if self.segments[sid].status != "CANCELLED"
        ]
        if active and all(item.status == "DELIVERED" for item in active):
            effective = max(parse_iso(scan.occurred_at) for scan in seg.delivery_scans.values())
            self._emit(
                ORDER_DELIVERED,
                "luggage_order",
                order.order_id,
                iso(effective),
                "全部运输段完成交接，行李送达",
                order_id=order.order_id,
                final_point=order.current_point,
            )

    def request_reroute(
        self,
        order_id: str,
        *,
        new_legs: list[dict],
        reason: str,
        new_window_end: datetime | str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """改签：迁移尚未发运的后续段；在途行李追加改派单，原交接事实不变。"""
        order = self._order(order_id)
        if order.status in ("DELIVERED", "IN_STORAGE", "COLLECTED"):
            raise DomainError("行李已送达或已入库，不能改签")
        occurred = self._now(occurred_at)
        case_id = f"{order_id}-RR{self._reroute_counts.get(order_id, 0) + 1}"
        active = sorted(
            (self.segments[sid] for sid in order.segment_ids if self.segments[sid].status != "CANCELLED"),
            key=lambda item: item.seq,
        )
        in_transit = [item for item in active if item.status in ("DISPATCHED", "IN_TRANSIT")]
        anchor_point = in_transit[-1].to_point if in_transit else order.current_point
        legs = _normalize_legs(new_legs)
        if legs[0]["from_point"] != anchor_point:
            raise DomainError("新行程必须从在途段终点或当前保管点接续")
        new_destination = legs[-1]["to_point"]
        self._emit(
            REROUTE_REQUESTED,
            "reroute_case",
            case_id,
            occurred,
            "收到改签请求",
            order_id=order_id,
            reason=reason,
            new_destination=new_destination,
        )
        for seg in active:
            if seg.status == "PLANNED":
                self._emit(
                    SEGMENT_CANCELLED,
                    "custody_segment",
                    seg.segment_id,
                    occurred,
                    "改签取消尚未发运的后续段",
                    order_id=order_id,
                    reroute_case_id=case_id,
                )
        outcome_payload = dict(
            order_id=order_id,
            new_destination=new_destination,
            **({"new_window_end": _norm_ts(new_window_end)} if new_window_end is not None else {}),
        )
        if in_transit:
            self._emit(
                REROUTE_ORDER_APPENDED,
                "reroute_case",
                case_id,
                occurred,
                "在途行李不可改线，追加改派单接续新路线",
                anchor_segment_id=in_transit[-1].segment_id,
                **outcome_payload,
            )
        else:
            self._emit(
                REROUTE_APPLIED,
                "reroute_case",
                case_id,
                occurred,
                "未发运后续段已迁移",
                **outcome_payload,
            )
        base_seq = active[-1].seq
        for offset, leg in enumerate(legs, start=1):
            self._emit(
                SEGMENT_PLANNED,
                "custody_segment",
                f"{case_id}-S{offset}",
                occurred,
                "改签后规划新运输段",
                order_id=order_id,
                seq=base_seq + offset,
                reroute_case_id=case_id,
                **leg,
            )
        return case_id

    def report_damage(
        self,
        segment_id: str,
        *,
        reporter_id: str,
        description: str,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """登记破损：打开异常并开立赔付案件，返回赔付案件号。"""
        seg = self._segment(segment_id)
        if seg.status == "CANCELLED":
            raise DomainError("已取消的运输段不能登记破损")
        occurred = self._now(occurred_at)
        exception_id = f"EX-{segment_id}-DMG"
        claim_id = f"CL-{segment_id}"
        if exception_id not in self.exceptions:
            self._emit(
                EXCEPTION_OPENED,
                "exception_case",
                exception_id,
                occurred,
                "发现行李破损，启动赔付流程",
                kind="damage",
                order_id=seg.order_id,
                segment_id=segment_id,
                reporter_id=reporter_id,
                description=description,
                action="claim",
            )
            self._emit(
                CLAIM_OPENED,
                "claim_case",
                claim_id,
                occurred,
                "开立赔付案件",
                order_id=seg.order_id,
                exception_id=exception_id,
                segment_id=segment_id,
            )
        return claim_id

    def assess_liability(
        self,
        claim_id: str,
        *,
        assessor_id: str,
        liable_segment_id: str,
        amount_cents: int,
        occurred_at: datetime | str | None = None,
    ) -> None:
        """责任认定人依据交接链认定责任段与赔付金额。"""
        claim = self._claim(claim_id)
        if claim.status != "OPEN":
            raise DomainError("赔付案件已结案")
        segment = self._segment(liable_segment_id)
        if segment.order_id != claim.order_id:
            raise DomainError("责任段必须属于同一行李单")
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents < 0:
            raise DomainError("赔付金额必须是非负整数（分）")
        self._emit(
            CLAIM_LIABILITY_ASSESSED,
            "claim_case",
            claim_id,
            self._now(occurred_at),
            "责任认定完成",
            order_id=claim.order_id,
            assessor_id=assessor_id,
            liable_segment_id=liable_segment_id,
            amount_cents=amount_cents,
        )

    def approve_claim(
        self,
        claim_id: str,
        *,
        approver_id: str,
        occurred_at: datetime | str | None = None,
    ) -> None:
        """赔付审核人结案；审核人与责任认定人必须分离，结案不改动交接事实。"""
        claim = self._claim(claim_id)
        if claim.status != "OPEN":
            raise DomainError("赔付案件已结案")
        if claim.assessor_id is None:
            raise DomainError("尚未完成责任认定")
        if approver_id == claim.assessor_id:
            raise DomainError("赔付审核人与责任认定人必须相互分离")
        self._emit(
            CLAIM_SETTLED,
            "claim_case",
            claim_id,
            self._now(occurred_at),
            "赔付结案（原交接事实保持不变）",
            order_id=claim.order_id,
            approver_id=approver_id,
            amount_cents=claim.amount_cents,
        )

    def collect(
        self,
        order_id: str,
        *,
        recipient_token: str,
        occurred_at: datetime | str | None = None,
    ) -> None:
        """授权收件人领取行李（含从临时保管中领取）。"""
        order = self._order(order_id)
        if recipient_token not in order.authorized_recipients:
            raise DomainError("非授权收件人，不能领取")
        if order.status not in ("DELIVERED", "IN_STORAGE"):
            raise DomainError("行李尚未送达，不能领取")
        self._emit(
            LUGGAGE_COLLECTED,
            "luggage_order",
            order_id,
            self._now(occurred_at),
            "授权收件人已领取",
            order_id=order_id,
            recipient_token=recipient_token,
        )

    def escalate(self, *, now: datetime | str | None = None) -> list[str]:
        """逾期升级：超时启动查询，送达后无人领取转入临时保管。

        异常编号按段/单确定性生成，重复执行或服务重启后再执行都不会
        开出第二张异常单。
        """
        moment = parse_iso(_norm_ts(now)) if now is not None else self._clock.now()
        opened: list[str] = []
        for seg in sorted(self.segments.values(), key=lambda item: (item.order_id, item.seq)):
            if seg.status in ("DISPATCHED", "IN_TRANSIT") and parse_iso(seg.window_end) < moment:
                exception_id = f"EX-{seg.segment_id}-TMO"
                if exception_id not in self.exceptions:
                    self._emit(
                        EXCEPTION_OPENED,
                        "exception_case",
                        exception_id,
                        iso(moment),
                        "超过承诺送达窗，启动查询",
                        kind="timeout",
                        order_id=seg.order_id,
                        segment_id=seg.segment_id,
                        action="inquiry",
                    )
                    opened.append(exception_id)
        for order in self.orders.values():
            if order.status == "DELIVERED" and order.delivered_at is not None:
                deadline = parse_iso(order.delivered_at) + self._unclaimed_grace
                if deadline < moment:
                    exception_id = f"EX-{order.order_id}-UNC"
                    if exception_id not in self.exceptions:
                        self._emit(
                            EXCEPTION_OPENED,
                            "exception_case",
                            exception_id,
                            iso(moment),
                            "送达后无人领取，转入临时保管",
                            kind="unclaimed",
                            order_id=order.order_id,
                            action="temporary_storage",
                        )
                        self._emit(
                            STORAGE_OPENED,
                            "exception_case",
                            f"STO-{order.order_id}",
                            iso(moment),
                            "临时保管已入库",
                            order_id=order.order_id,
                            exception_id=exception_id,
                        )
                        opened.append(exception_id)
        return opened

    def settle_segment(self, segment_id: str, *, occurred_at: datetime | str | None = None) -> str:
        """开具分段结算单；重复调用返回原结算单，不重复入账。"""
        seg = self._segment(segment_id)
        if segment_id in self.settlements:
            return self.settlements[segment_id]["aggregate_id"]
        if seg.status != "DELIVERED":
            raise DomainError("运输段未完成交接，不能结算")
        fee = segment_fee(
            window_start=parse_iso(seg.window_start),
            window_end=parse_iso(seg.window_end),
            holidays=self._holidays,
        )
        settlement_id = f"ST-{segment_id}"
        self._emit(
            SETTLEMENT_ISSUED,
            "settlement_cycle",
            settlement_id,
            self._now(occurred_at),
            "开具分段结算单",
            order_id=seg.order_id,
            segment_id=segment_id,
            carrier_id=seg.carrier_id,
            **fee.as_dict(),
        )
        return settlement_id

    # ------------------------------------------------------------------
    # 查询与视图
    # ------------------------------------------------------------------

    def track_for_tourist(self, voucher_id: str, recipient_token: str) -> dict:
        """游客查询：当前位置、保管方与预计到达时间；需授权收件人令牌。"""
        order_id = self._voucher_index.get(voucher_id)
        if order_id is None:
            raise DomainError("凭证不存在")
        order = self.orders[order_id]
        if recipient_token not in order.authorized_recipients:
            raise DomainError("未授权的查询")
        eta = order.window_end
        if order.status in ("DELIVERED", "IN_STORAGE", "COLLECTED") and order.delivered_at:
            eta = order.delivered_at
        return {
            "order_id": order.order_id,
            "status": order.status,
            "current_point": order.current_point,
            "custody_holder": order.current_holder,
            "piece_count": order.piece_count,
            "expected_window": {"start": order.window_start, "end": order.window_end},
            "eta": eta,
        }

    def carrier_view(self, carrier_id: str) -> list[dict]:
        """承运商视图：只看得到自己当前在运的一段，看不到完整行程与收件人。"""
        view = []
        for seg in sorted(self.segments.values(), key=lambda item: (item.order_id, item.seq)):
            if seg.carrier_id == carrier_id and seg.status in ("DISPATCHED", "IN_TRANSIT"):
                order = self.orders[seg.order_id]
                view.append(
                    {
                        "segment_id": seg.segment_id,
                        "order_id": seg.order_id,
                        "status": seg.status,
                        "from_point": seg.from_point,
                        "to_point": seg.to_point,
                        "piece_count": order.piece_count,
                        "seal_no": order.seal_no,
                        "content_hints": list(order.content_hints),
                        "window": {"start": seg.window_start, "end": seg.window_end},
                        "handover_token": f"HO-{seg.segment_id}",
                    }
                )
        return view

    def hotel_view(self, hotel_id: str) -> list[dict]:
        """酒店视图：只看到送往本店的到达段，读不到完整行程与收件人。"""
        view = []
        for seg in sorted(self.segments.values(), key=lambda item: (item.order_id, item.seq)):
            if seg.to_point == hotel_id and seg.status != "CANCELLED":
                order = self.orders[seg.order_id]
                view.append(
                    {
                        "segment_id": seg.segment_id,
                        "order_id": seg.order_id,
                        "status": seg.status,
                        "piece_count": order.piece_count,
                        "expected_window": {"start": seg.window_start, "end": seg.window_end},
                        "handover_token": f"HO-{seg.segment_id}",
                    }
                )
        return view

    def regulator_replay(self, order_id: str) -> dict:
        """监管重放：按发生时间合并全链条事件，并逐段核对付款。"""
        order = self._order(order_id)
        events = [
            event
            for event in self._store.load()
            if event.get("order_id") == order_id or event.get("aggregate_id") == order_id
        ]
        events.sort(key=lambda event: (event["occurred_at"], event["event_id"]))
        chain = [
            {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "occurred_at": event["occurred_at"],
                "summary": event["summary"],
            }
            for event in events
        ]
        custody = [
            {
                "segment_id": event["aggregate_id"],
                "phase": event["phase"],
                "from_holder": event["from_holder"],
                "to_holder": event["to_holder"],
                "effective_at": event["occurred_at"],
            }
            for event in events
            if event["event_type"] == CUSTODY_TRANSFERRED
        ]
        payments = []
        for segment_id in order.segment_ids:
            seg = self.segments[segment_id]
            if seg.status == "CANCELLED":
                continue
            entry = {
                "segment_id": segment_id,
                "carrier_id": seg.carrier_id,
                "status": seg.status,
                "settled": segment_id in self.settlements,
            }
            if segment_id in self.settlements:
                issued = self.settlements[segment_id]
                recomputed = segment_fee(
                    window_start=parse_iso(seg.window_start),
                    window_end=parse_iso(seg.window_end),
                    holidays=self._holidays,
                )
                entry.update(
                    issued_total_cents=issued["total_cents"],
                    recomputed_total_cents=recomputed.total_cents,
                    match=issued["total_cents"] == recomputed.total_cents,
                )
            payments.append(entry)
        due = [entry for entry in payments if entry["status"] == "DELIVERED"]
        return {
            "order_id": order_id,
            "status": order.status,
            "chain": chain,
            "custody": custody,
            "payments": payments,
            "all_payments_match": all(
                entry.get("settled") and entry.get("match") for entry in due
            ),
        }

    # ------------------------------------------------------------------
    # 事件折叠
    # ------------------------------------------------------------------

    def _on_order_accepted(self, event: dict) -> None:
        self.orders[event["aggregate_id"]] = OrderState(
            order_id=event["aggregate_id"],
            voucher_id=event["voucher_id"],
            fingerprint=event["fingerprint"],
            seal_no=event["seal_no"],
            destination=event["destination"],
            piece_count=event["piece_count"],
            appearance_summary=event["appearance_summary"],
            content_hints=list(event["content_hints"]),
            window_start=event["window_start"],
            window_end=event["window_end"],
            authorized_recipients=list(event["authorized_recipients"]),
            current_holder=event["origin_point"],
            current_point=event["origin_point"],
        )
        self._voucher_index[event["voucher_id"]] = event["aggregate_id"]

    def _on_segment_planned(self, event: dict) -> None:
        seg = SegmentState(
            segment_id=event["aggregate_id"],
            order_id=event["order_id"],
            seq=event["seq"],
            carrier_id=event["carrier_id"],
            from_point=event["from_point"],
            to_point=event["to_point"],
            window_start=event["window_start"],
            window_end=event["window_end"],
            reroute_case_id=event.get("reroute_case_id"),
        )
        self.segments[seg.segment_id] = seg
        self.orders[seg.order_id].segment_ids.append(seg.segment_id)

    def _on_segment_dispatched(self, event: dict) -> None:
        self.segments[event["aggregate_id"]].status = "DISPATCHED"

    def _on_segment_cancelled(self, event: dict) -> None:
        self.segments[event["aggregate_id"]].status = "CANCELLED"

    def _on_handover_scan_recorded(self, event: dict) -> None:
        seg = self.segments[event["aggregate_id"]]
        scans = seg.pickup_scans if event["phase"] == "pickup" else seg.delivery_scans
        scans[event["actor_role"]] = ScanRecord(
            actor_role=event["actor_role"],
            actor_id=event["actor_id"],
            occurred_at=event["occurred_at"],
        )

    def _on_custody_transferred(self, event: dict) -> None:
        seg = self.segments[event["aggregate_id"]]
        order = self.orders[event["order_id"]]
        order.current_holder = event["to_holder"]
        if event["phase"] == "pickup":
            seg.status = "IN_TRANSIT"
            order.status = "IN_TRANSIT"
        else:
            seg.status = "DELIVERED"
            order.current_point = event["to_holder"]

    def _on_order_delivered(self, event: dict) -> None:
        order = self.orders[event["aggregate_id"]]
        order.status = "DELIVERED"
        order.delivered_at = event["occurred_at"]

    def _on_luggage_collected(self, event: dict) -> None:
        order = self.orders[event["aggregate_id"]]
        order.status = "COLLECTED"
        order.collected_by = event["recipient_token"]

    def _on_manual_review_opened(self, event: dict) -> None:
        self.orders[event["aggregate_id"]].manual_review_open = True

    def _on_reroute_requested(self, event: dict) -> None:
        order_id = event["order_id"]
        self._reroute_counts[order_id] = self._reroute_counts.get(order_id, 0) + 1

    def _on_reroute_applied(self, event: dict) -> None:
        self._apply_reroute_outcome(event)

    def _on_reroute_order_appended(self, event: dict) -> None:
        self._apply_reroute_outcome(event)

    def _apply_reroute_outcome(self, event: dict) -> None:
        order = self.orders[event["order_id"]]
        order.destination = event["new_destination"]
        if event.get("new_window_end"):
            order.window_end = event["new_window_end"]

    def _on_exception_opened(self, event: dict) -> None:
        self.exceptions[event["aggregate_id"]] = ExceptionState(
            exception_id=event["aggregate_id"],
            kind=event["kind"],
            order_id=event["order_id"],
            opened_at=event["occurred_at"],
            segment_id=event.get("segment_id"),
            action=event.get("action"),
        )

    def _on_storage_opened(self, event: dict) -> None:
        self.orders[event["order_id"]].status = "IN_STORAGE"

    def _on_claim_opened(self, event: dict) -> None:
        self.claims[event["aggregate_id"]] = ClaimState(
            claim_id=event["aggregate_id"],
            order_id=event["order_id"],
            exception_id=event["exception_id"],
            segment_id=event["segment_id"],
        )

    def _on_claim_liability_assessed(self, event: dict) -> None:
        claim = self.claims[event["aggregate_id"]]
        claim.assessor_id = event["assessor_id"]
        claim.liable_segment_id = event["liable_segment_id"]
        claim.amount_cents = event["amount_cents"]

    def _on_claim_settled(self, event: dict) -> None:
        claim = self.claims[event["aggregate_id"]]
        claim.status = "SETTLED"
        claim.settled_by = event["approver_id"]

    def _on_settlement_issued(self, event: dict) -> None:
        self.settlements[event["segment_id"]] = event

    # ------------------------------------------------------------------
    # 查找辅助
    # ------------------------------------------------------------------

    def _order(self, order_id: str) -> OrderState:
        try:
            return self.orders[order_id]
        except KeyError:
            raise DomainError(f"未知行李单：{order_id}") from None

    def _segment(self, segment_id: str) -> SegmentState:
        try:
            return self.segments[segment_id]
        except KeyError:
            raise DomainError(f"未知运输段：{segment_id}") from None

    def _claim(self, claim_id: str) -> ClaimState:
        try:
            return self.claims[claim_id]
        except KeyError:
            raise DomainError(f"未知赔付案件：{claim_id}") from None
