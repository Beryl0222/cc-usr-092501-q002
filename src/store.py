"""事件存储：追加写、按发生时间合并、命令幂等与重启恢复。

设计要点：

* 一件行李（一个凭证）的全部事实落入同一条事件流，便于监管整链重放；
  事件仍以 ``aggregate_type`` / ``aggregate_id`` 区分段、改签单等子主体。
* 磁盘为只追加的 JSONL，原始回执（含离线后补）逐行保留，便于审计；
  内存规范流按 ``occurred_at`` 稳定合并并重排 ``version``，
  使离线恢复的晚到回执仍落在其实际发生的位置。
* ``event_id`` 全局唯一：完全相同的回执重传被忽略，不产生第二件行李。
* 命令幂等索引随旁车文件持久化：服务重启后重放同一命令不会重复转运，
  也不会漏掉已确认的逾期升级。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .events import Event


class ConflictError(ValueError):
    """同一 command_id 携带了不同请求体。"""


class DuplicateEventError(ValueError):
    """event_id 已存在且内容不一致。"""


def stream_of(event: Event) -> str:
    order_id = event.payload.get("order_id")
    return f"order:{order_id}" if order_id else f"{event.aggregate_type}:{event.aggregate_id}"


@dataclass
class CommandRecord:
    command_id: str
    request_key: str
    stream_id: str
    event_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "request_key": self.request_key,
            "stream_id": self.stream_id,
            "event_ids": self.event_ids,
        }

    @staticmethod
    def from_dict(record: dict) -> "CommandRecord":
        return CommandRecord(
            command_id=record["command_id"],
            request_key=record["request_key"],
            stream_id=record["stream_id"],
            event_ids=list(record.get("event_ids", [])),
        )


class EventStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.commands_path = Path(str(path) + ".commands.jsonl") if path else None
        self._streams: dict[str, list[Event]] = {}
        self._event_ids: dict[str, Event] = {}
        self._commands: dict[str, CommandRecord] = {}
        if self.path and self.path.exists():
            self._load()

    # ---- 读取 -------------------------------------------------------------

    def stream(self, stream_id: str) -> list[Event]:
        """规范顺序（发生时间）的事件流，版本号已重排。"""
        return list(self._streams.get(stream_id, []))

    def order_stream(self, order_id: str) -> list[Event]:
        return self.stream(f"order:{order_id}")

    def has_event(self, event_id: str) -> bool:
        return event_id in self._event_ids

    def command(self, command_id: str) -> CommandRecord | None:
        return self._commands.get(command_id)

    def all_streams(self) -> dict[str, list[Event]]:
        return {key: list(events) for key, events in self._streams.items()}

    def all_events(self) -> list[Event]:
        """全库事件（按追加顺序），用于全局 ID 播种与去重判断。"""
        return list(self._event_ids.values())

    # ---- 写入 -------------------------------------------------------------

    def append(
        self,
        events: list[Event],
        *,
        command_id: str | None = None,
        request_key: str = "",
    ) -> list[Event]:
        """追加事件；带 command_id 时提供幂等保证。

        同 command_id 重放且请求体一致：返回已记录的事件，不产生副作用；
        同 command_id 但请求体不同：抛 ConflictError。
        """
        if command_id is not None and command_id in self._commands:
            recorded = self._commands[command_id]
            if recorded.request_key != request_key:
                raise ConflictError(f"命令 {command_id} 的请求体与首次提交不一致")
            return [self._event_ids[eid] for eid in recorded.event_ids if eid in self._event_ids]

        accepted: list[Event] = []
        for event in events:
            if event.event_id in self._event_ids:
                # 完全相同的回执重传：忽略，不产生第二件/第二次交接。
                if self._same_event(self._event_ids[event.event_id], event):
                    continue
                raise DuplicateEventError(f"事件标识 {event.event_id} 已用于不同内容")
            accepted.append(event)

        for event in accepted:
            self._insert(stream_of(event), event)
            if self.path:
                # 写入版本重排后的规范实例（version 从 1 起）。
                stored = self._event_ids[event.event_id]
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(stored.to_dict(), ensure_ascii=False) + "\n")

        if command_id is not None:
            record = CommandRecord(
                command_id=command_id,
                request_key=request_key,
                stream_id=stream_of(accepted[0]) if accepted else "",
                event_ids=[event.event_id for event in accepted],
            )
            self._commands[command_id] = record
            if self.commands_path:
                with self.commands_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return accepted

    # ---- 内部 -------------------------------------------------------------

    def _insert(self, stream_id: str, event: Event) -> None:
        chain = self._streams.setdefault(stream_id, [])
        chain.append(event)
        self._event_ids[event.event_id] = event
        # 离线晚到回执：按发生时间稳定排序（Python 排序稳定，同时刻先到者在前）。
        chain.sort(key=lambda item: item.occurred_at)
        for position, item in enumerate(chain, start=1):
            if item.version != position:
                # Event 不可变，版本重排需替换实例。
                reordered = Event(
                    event_id=item.event_id,
                    event_type=item.event_type,
                    aggregate_id=item.aggregate_id,
                    occurred_at=item.occurred_at,
                    version=position,
                    summary=item.summary,
                    payload=item.payload,
                    aggregate_type=item.aggregate_type,
                    command_id=item.command_id,
                )
                chain[position - 1] = reordered
                self._event_ids[reordered.event_id] = reordered

    @staticmethod
    def _same_event(left: Event, right: Event) -> bool:
        return (
            left.event_type == right.event_type
            and left.aggregate_id == right.aggregate_id
            and left.occurred_at == right.occurred_at
            and left.payload == right.payload
        )

    def _load(self) -> None:
        assert self.path is not None
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            event = Event.from_dict(json.loads(raw))
            if event.event_id in self._event_ids:
                if not self._same_event(self._event_ids[event.event_id], event):
                    raise DuplicateEventError(f"磁盘事件 {event.event_id} 内容冲突")
                continue
            self._insert(stream_of(event), event)
        if self.commands_path and self.commands_path.exists():
            for raw in self.commands_path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                record = CommandRecord.from_dict(json.loads(raw))
                self._commands[record.command_id] = record
