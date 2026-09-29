"""领域事件信封与事件类型约定。

信封字段与 ``contracts/domain.schema.json`` 保持一致；事件只追加、不修改，
赔付结案等后续事件不得覆盖既有交接事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---- 事件类型 ----------------------------------------------------------------

ORDER_ACCEPTED = "ORDER_ACCEPTED"                    # 受理：冻结件数/外观/隐私提示/送达窗/授权收件人
MANUAL_REVIEW_OPENED = "MANUAL_REVIEW_OPENED"        # 同编号但封签/目的地/件数变化 → 人工核对
MANUAL_REVIEW_RESOLVED = "MANUAL_REVIEW_RESOLVED"    # 人工核对结论，解除交接冻结
SCAN_RECORDED = "SCAN_RECORDED"                      # 单方扫描回执（离线可后补）
CUSTODY_TRANSFERRED = "CUSTODY_TRANSFERRED"          # 双方扫描齐备后转移保管责任
DELIVERY_COMPLETED = "DELIVERY_COMPLETED"            # 末段交付授权收件人
REROUTE_REQUESTED = "REROUTE_REQUESTED"              # 改签请求
REROUTE_APPLIED = "REROUTE_APPLIED"                  # 改签仅迁移未发运的后续段
REDISPATCH_ISSUED = "REDISPATCH_ISSUED"              # 已在途行李追加改派单
EXCEPTION_OPENED = "EXCEPTION_OPENED"                # 超时/破损/无人领取等异常立案
INQUIRY_OPENED = "INQUIRY_OPENED"                    # 超时触发查询
EXCEPTION_UPGRADED = "EXCEPTION_UPGRADED"            # 查询逾期未结 → 升级
TEMP_STORAGE_OPENED = "TEMP_STORAGE_OPENED"          # 无人领取 → 临时保管
TEMP_STORAGE_RELEASED = "TEMP_STORAGE_RELEASED"      # 临时保管后认领
LIABILITY_DETERMINED = "LIABILITY_DETERMINED"        # 责任认定（认定人）
SETTLEMENT_ISSUED = "SETTLEMENT_ISSUED"              # 赔付审核 / 分段服务费结算（审核人）

# ---- 聚合主体 ----------------------------------------------------------------

AGG_ORDER = "luggage_order"
AGG_SEGMENT = "custody_segment"
AGG_REROUTE = "reroute_case"
AGG_EXCEPTION = "exception_case"
AGG_REVIEW = "manual_review"
AGG_SETTLEMENT = "settlement_cycle"

EVENT_TYPES = frozenset({
    ORDER_ACCEPTED, MANUAL_REVIEW_OPENED, MANUAL_REVIEW_RESOLVED, SCAN_RECORDED,
    CUSTODY_TRANSFERRED, DELIVERY_COMPLETED, REROUTE_REQUESTED, REROUTE_APPLIED,
    REDISPATCH_ISSUED, EXCEPTION_OPENED, INQUIRY_OPENED, EXCEPTION_UPGRADED,
    TEMP_STORAGE_OPENED, TEMP_STORAGE_RELEASED, LIABILITY_DETERMINED, SETTLEMENT_ISSUED,
})

AGGREGATE_TYPES = frozenset({
    AGG_ORDER, AGG_SEGMENT, AGG_REROUTE, AGG_EXCEPTION, AGG_REVIEW, AGG_SETTLEMENT,
})

# 事件主体 → 信封 aggregate_type
EVENT_AGGREGATE = {
    ORDER_ACCEPTED: AGG_ORDER,
    MANUAL_REVIEW_OPENED: AGG_REVIEW,
    MANUAL_REVIEW_RESOLVED: AGG_REVIEW,
    SCAN_RECORDED: AGG_SEGMENT,
    CUSTODY_TRANSFERRED: AGG_SEGMENT,
    DELIVERY_COMPLETED: AGG_SEGMENT,
    REROUTE_REQUESTED: AGG_REROUTE,
    REROUTE_APPLIED: AGG_REROUTE,
    REDISPATCH_ISSUED: AGG_REROUTE,
    EXCEPTION_OPENED: AGG_EXCEPTION,
    INQUIRY_OPENED: AGG_EXCEPTION,
    EXCEPTION_UPGRADED: AGG_EXCEPTION,
    TEMP_STORAGE_OPENED: AGG_EXCEPTION,
    TEMP_STORAGE_RELEASED: AGG_EXCEPTION,
    LIABILITY_DETERMINED: AGG_SETTLEMENT,
    SETTLEMENT_ISSUED: AGG_SETTLEMENT,
}


class DomainError(ValueError):
    """业务规则被违反。"""


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    aggregate_type: str = AGG_ORDER
    command_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "summary": self.summary,
            "payload": self.payload,
        }
        if self.command_id:
            record["command_id"] = self.command_id
        return record

    @staticmethod
    def from_dict(record: dict[str, Any]) -> "Event":
        occurred_at = datetime.fromisoformat(str(record["occurred_at"]).replace("Z", "+00:00"))
        return Event(
            event_id=record["event_id"],
            event_type=record["event_type"],
            aggregate_type=record.get("aggregate_type", AGG_ORDER),
            aggregate_id=record["aggregate_id"],
            occurred_at=occurred_at,
            version=record["version"],
            summary=record["summary"],
            payload=dict(record.get("payload", {})),
            command_id=record.get("command_id"),
        )
