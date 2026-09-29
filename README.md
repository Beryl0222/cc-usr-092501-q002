# 普惠文旅委托履约账

本仓库维护该服务跨模块交换时使用的领域事件信封、中文样例和基础校验入口，使不同业务组件能够用一致的对象标识、事件版本和发生时间传递事实。

在此之上，`src/ledger.py` 实现了县文旅局购买服务所用的便民行李接力履约账：游客到达县城后，行李从车站到酒店、景区和返程交通点分段寄送，每段保管责任、异常与赔付都以事件入账，可回放、可审计。

## 目录

- `contracts/domain.schema.json`：领域对象与事件名称约定。
- `data/sample.json`：一条可用于本地联调的示例事件。
- `data/accept_sample.json`：一条行李接力受理命令样例。
- `src/envelope.py`：事件信封的基础字段校验。
- `src/cli.py`：检查 JSON 事件文件的命令入口。
- `src/clock.py`：可注入时钟与 ISO 8601 时间工具。
- `src/store.py`：追加式 JSONL 事件账簿，按 event_id 去重。
- `src/fees.py`：分段运费结算（节假日服务费、跨午夜承诺附加费）。
- `src/ledger.py`：行李接力履约账领域服务（事件溯源）。
- `src/relay_cli.py`：履约账命令行入口。
- `tests/`：信封、命令入口与履约账的回归测试。

## 领域约定

当前交换协议覆盖服务受理、行李交接和改签改派。事件标识一旦接收不得原地复用为另一份内容，版本必须为正整数，时间采用带时区的 ISO 8601 格式。业务修订通过新的事件表达，原始记录继续用于追溯。

行李接力的业务规则：

- 受理时冻结件数、外观摘要、隐私化物品提示、期望送达窗和授权收件人；同一凭证完全重传不产生第二件行李，编号相同而封签、目的地或件数变化立即转入人工核对。
- 每段运输只有交接双方扫描都登记后才转移保管责任；离线回执恢复后按扫描发生时间入账，交接生效时间取双方扫描中较晚者。
- 改签只能迁移尚未发运的后续段；已在途行李通过追加改派单接续新路线，原交接事实不变。
- 超时、破损和无人领取分别触发查询、赔付和临时保管；赔付结案只是新事件，不覆盖原交接事实，且赔付审核人与责任认定人相互分离。
- 承运商只能看到当前一段所需信息，酒店读不到完整行程；游客凭授权令牌查询当前位置与预计到达。
- 跨午夜承诺和节假日服务费按注入的时钟与节假日表结算；监管命令可重放某件行李的全链条并逐段核对付款。
- 服务重启后重放账簿恢复状态，不重复转运，也不漏掉逾期升级。

## 本地运行

检查示例事件：

```bash
python3 -m src.cli data/sample.json
```

用命令行走一段履约流程（账簿保存在本地 JSONL 文件）：

```bash
python3 -m src.relay_cli accept /tmp/ledger.jsonl data/accept_sample.json
python3 -m src.relay_cli dispatch /tmp/ledger.jsonl LO-V-20260930-001-S1
python3 -m src.relay_cli track /tmp/ledger.jsonl V-20260930-001 tok-alice
python3 -m src.relay_cli replay /tmp/ledger.jsonl LO-V-20260930-001
```

运行测试：

```bash
python3 -m unittest discover -s tests
```

编译检查：

```bash
python3 -m compileall -q src tests
```

这些命令只使用 Python 标准库，不需要单独运行数据库或其他服务。
