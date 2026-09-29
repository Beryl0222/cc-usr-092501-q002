# 普惠文旅委托履约账 · 便民行李接力

假日高铁到站后，县文旅局购买承运商的行李接力服务（车站 → 酒店 → 景区 → 返程交通点）。
本仓库维护该服务的**领域事件契约与事件溯源参考实现**：承运商、酒店前台、游客中心
各有自己的本地编号，但每件行李在履约账中拥有统一凭证链，使保管责任、改签改派、
异常赔付与服务费结算都能逐段追溯。

## 核心业务规则

- **受理冻结**：受理时冻结件数、封签、外观摘要、隐私化物品提示（不开箱清点）、
  期望送达窗与授权收件人。
- **凭证幂等 / 人工核对**：同一凭证完全重传不产生第二件行李；编号相同而
  封签、目的地或件数变化，立即开 `MANUAL_REVIEW_OPENED` 并冻结交接，
  核对结论 `MANUAL_REVIEW_RESOLVED` 后才解冻。
- **双方扫描转移责任**：每段在交接点须交出方与接收方分别扫描
  （`SCAN_RECORDED`），双方齐备才发出 `CUSTODY_TRANSFERRED`；
  单方扫描不转移保管责任。末段须交付授权收件人。
- **改签与改派分离**：改签（`REROUTE_APPLIED`）只迁移尚未发运的后续段，
  已在途（或已滞留节点）的行李通过追加改派单（`REDISPATCH_ISSUED`）接续。
- **三类异常分流**：超时 → 查询（`INQUIRY_OPENED`），逾期 4 小时未结 →
  升级（`EXCEPTION_UPGRADED`）；破损 → 责任认定后赔付；无人领取 →
  临时保管（`TEMP_STORAGE_OPENED`），授权收件人认领后解除。
- **赔付不覆盖事实**：赔付（`SETTLEMENT_ISSUED`）必须引用真实存在的交接点
  标识，且责任认定人与赔付审核人强制为两个不同账号；原交接事件永不修改。
- **最小可见**：承运商只看到当前一段所需信息；酒店只看到与本节点相关的交接，
  读不到完整行程；游客可查当前位置与预计到达；监管可重放整链并核对每段付款。
- **离线与重启**：离线回执恢复后按 `occurred_at` 合并并重排版本；
  `command_id` 提供命令幂等并随旁车文件落盘，服务重启不重复转运、不漏逾期升级。
- **可注入时钟结算**：跨午夜附加费与节假日服务费按交接事实时间和注入的
  节假日日历结算，不读系统墙钟。

## 目录

- `contracts/domain.schema.json`：领域事件信封、事件与聚合主体枚举。
- `data/sample.json`：一条含两段接力路线的受理事件示例。
- `src/events.py`：事件类型、聚合类型与不可变事件信封。
- `src/clock.py`：时钟协议（系统/固定/手动）与节假日日历。
- `src/store.py`：只追加 JSONL 存储、按发生时间合并、命令幂等与重启恢复。
- `src/domain.py`：命令、状态归约与全部业务决策（纯函数，不依赖墙钟）。
- `src/service.py`：应用编排、角色门禁、游客/承运商/酒店视图与监管重放。
- `src/envelope.py` / `src/cli.py`：事件信封校验与命令行入口。
- `tests/`：覆盖上述每条规则的回归测试。

## 本地运行

检查示例事件：

```bash
python3 -m src.cli data/sample.json
```

运行测试：

```bash
python3 -m unittest discover -s tests
```

编译检查：

```bash
python3 -m compileall -q src tests
```

只使用 Python 标准库（≥ 3.11），不需要数据库或其他服务；
存储默认在内存，传入文件路径即启用 JSONL 持久化与重启恢复。

## 事件清单

| 事件 | 含义 |
| --- | --- |
| `ORDER_ACCEPTED` | 受理并冻结受理要素 |
| `MANUAL_REVIEW_OPENED` / `MANUAL_REVIEW_RESOLVED` | 编号冲突转人工核对 / 核对结论解冻 |
| `SCAN_RECORDED` | 交接点单方扫描回执（离线可后补） |
| `CUSTODY_TRANSFERRED` | 双方齐备，段内保管责任转移 |
| `DELIVERY_COMPLETED` | 末段交付授权收件人 |
| `REROUTE_REQUESTED` / `REROUTE_APPLIED` | 改签请求 / 未发运段迁移 |
| `REDISPATCH_ISSUED` | 在途或滞留行李的追加改派单 |
| `EXCEPTION_OPENED` | 超时 / 破损 / 无人领取立案 |
| `INQUIRY_OPENED` / `EXCEPTION_UPGRADED` | 超时查询 / 查询逾期升级 |
| `TEMP_STORAGE_OPENED` / `TEMP_STORAGE_RELEASED` | 临时保管 / 认领解除 |
| `LIABILITY_DETERMINED` | 责任认定（认定人，引用失败交接点） |
| `SETTLEMENT_ISSUED` | 赔付审核（审核人）或分段服务费结算 |
