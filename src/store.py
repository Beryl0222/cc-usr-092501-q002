"""追加式 JSONL 事件账簿。

事件一旦写入不可修改；按 event_id 去重，同一凭证完全重传不会产生第二条记录。
服务重启后重放整个账簿即可恢复状态。
"""

from __future__ import annotations

import json
from pathlib import Path


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._seen.add(json.loads(line)["event_id"])

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._seen

    def append(self, event: dict) -> bool:
        """写入事件；event_id 已存在时返回 False，不重复入账。"""
        event_id = event["event_id"]
        if event_id in self._seen:
            return False
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self._seen.add(event_id)
        return True

    def load(self) -> list[dict]:
        """按追加顺序返回全部事件。"""
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
