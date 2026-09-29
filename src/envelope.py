"""领域事件信封的基础校验。"""

from __future__ import annotations

from datetime import datetime

from .events import AGGREGATE_TYPES, EVENT_TYPES

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")


def validate_event(record: object) -> list[str]:
    if not isinstance(record, dict):
        return ["事件必须是 JSON 对象"]
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if "event_type" in record and record["event_type"] not in EVENT_TYPES:
        errors.append(f"event_type 不在约定枚举内：{record['event_type']}")
    if "aggregate_type" in record and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"aggregate_type 不在约定枚举内：{record['aggregate_type']}")
    if "occurred_at" in record:
        try:
            parsed = datetime.fromisoformat(str(record["occurred_at"]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                errors.append("occurred_at 必须包含时区")
        except ValueError:
            errors.append("occurred_at 必须是 ISO 8601 时间")
    if "event_id" in record and (not isinstance(record["event_id"], str) or not record["event_id"]):
        errors.append("event_id 不能为空")
    if "payload" in record and not isinstance(record["payload"], dict):
        errors.append("payload 必须是对象")
    return errors
