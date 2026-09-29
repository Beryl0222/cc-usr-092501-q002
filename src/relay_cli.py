"""便民行李接力履约账命令行入口。

用法：python3 -m src.relay_cli <命令> <账簿路径> [参数]

命令：
  accept   <账簿> <受理JSON>   受理行李接力委托（幂等）
  dispatch <账簿> <段号>       承运商接单发运
  scan     <账簿> <扫描JSON>   登记交接扫描（支持离线回执，含 event_id 去重）
  reroute  <账簿> <改签JSON>   改签：迁移未发运段，在途追加改派单
  collect  <账簿> <单号> <令牌> 授权收件人领取
  tick     <账簿>              逾期升级（超时查询、无人领取转临时保管）
  settle   <账簿> <段号>       开具分段结算单
  track    <账簿> <凭证> <令牌> 游客查询当前位置与预计到达
  replay   <账簿> <单号>       监管重放全链条并核对每段付款
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .clock import SystemClock
from .ledger import DomainError, RelayLedger
from .store import EventStore

USAGE = "用法：python3 -m src.relay_cli <accept|dispatch|scan|reroute|collect|tick|settle|track|replay> <账簿路径> [参数]"


def _read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    command, store_path = args[0], args[1]
    ledger = RelayLedger(EventStore(store_path), SystemClock())
    try:
        if command == "accept" and len(args) == 3:
            order_id = ledger.accept_order(**_read_json(args[2]))
            print(f"已受理：{order_id}")
        elif command == "dispatch" and len(args) == 3:
            ledger.dispatch_segment(args[2])
            print(f"已发运：{args[2]}")
        elif command == "scan" and len(args) == 3:
            payload = _read_json(args[2])
            ledger.record_scan(payload.pop("segment_id"), **payload)
            print("扫描已登记")
        elif command == "reroute" and len(args) == 3:
            payload = _read_json(args[2])
            case_id = ledger.request_reroute(payload.pop("order_id"), **payload)
            print(f"改签已处理：{case_id}")
        elif command == "collect" and len(args) == 4:
            ledger.collect(args[2], recipient_token=args[3])
            print("已领取")
        elif command == "tick" and len(args) == 2:
            opened = ledger.escalate()
            print(f"新升级 {len(opened)} 起")
        elif command == "settle" and len(args) == 3:
            print(f"结算单：{ledger.settle_segment(args[2])}")
        elif command == "track" and len(args) == 4:
            print(json.dumps(ledger.track_for_tourist(args[2], args[3]), ensure_ascii=False, indent=2))
        elif command == "replay" and len(args) == 3:
            print(json.dumps(ledger.regulator_replay(args[2]), ensure_ascii=False, indent=2))
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except (DomainError, KeyError, ValueError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
