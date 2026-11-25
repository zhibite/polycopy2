# 链上 OrderFilled 解码与 RPC 信号源 PoC

结论：Polymarket CTF Exchange V2 的 `OrderFilled` 事件可以完整还原 data-api
的成交字段。50 笔真实成交逐字段对照全部通过；RPC 日志信号源已与现有
data-api 纸盘并行运行。

## 1. 合约与事件

最近成交全部走 V2 交易所（V1 交易所近 300 个区块只剩 6 条无关事件）：

| 合约 | 地址 |
| --- | --- |
| CTFExchangeV2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| NegRiskCtfExchangeV2 | `0xe2222d279d744050d28e00520010520000310F59` |

事件（实现见 `polycopy/onchain.py`）：

```solidity
OrderFilled(
    bytes32 indexed orderHash,
    address indexed maker,
    address indexed taker,
    uint8 side,                 // 0 = BUY, 1 = SELL（maker 视角）
    uint256 tokenId,
    uint256 makerAmountFilled,
    uint256 takerAmountFilled,
    uint256 fee,
    bytes32 builder,
    bytes32 metadata
)
```

关键点：**钱包自己的成交一定是 `maker == 钱包地址` 的事件**。这里的
钱包地址是 data-api 的 `proxy_wallet`（Polymarket 代理钱包），不是签名
EOA；配置里的 `[targets.*].address` 必须填这个代理地址。

- 市价单（taker 单）只发一条事件，`maker = 钱包`、`taker = 交易所`，
  金额为整笔聚合值，恰好对应一条 data-api activity。
- 限价单每次被吃单发一条事件，`maker = 钱包`、`taker = 对手方`。
- 钱包作为纯 taker 出现在别人的事件里时，不需要处理，那是对方成交的镜像。

字段映射（`decimals = 6`）：

| 字段 | BUY | SELL |
| --- | --- | --- |
| `side` | `BUY` | `SELL` |
| `size`（股数） | `takerAmountFilled / 1e6` | `makerAmountFilled / 1e6` |
| `price`（毛价） | `makerAmountFilled / takerAmountFilled` | `takerAmountFilled / makerAmountFilled` |
| `usdc_size`（净现金流） | `(makerAmountFilled + fee) / 1e6` | `(takerAmountFilled - fee) / 1e6` |
| `timestamp` | 区块时间戳 | 区块时间戳 |

`price` 是订单毛价（与 data-api 一致），`usdc_size` 已扣/加手续费。
按此规则生成的 activity row 直接喂给现有 `Engine.ingest_rows()`，去重、
风控、下单模拟、结算全部复用原引擎。

## 2. 阶段一：解码验证

命令：

```bash
python -m polycopy onchain verify \
  --config configs/paper_f705.toml \
  --wallet 0xf705fa045201391d9632b7f3cde06a5e24453ca7 \
  --count 50 --lookback 86400 \
  --json-out reports/verify_f705_50.json
```

2026-11-23 的结果（50 笔 data-api 成交，交易哈希一一对应）：

```text
data-api trades: 50  txs: 50  receipts: 50  own-order events: 50
matched: 50  passed: 50  failed: 0  unmatched rows: 0  unmatched events: 0
max |diff|: size 0.00000000  price 0.00000000  usdc 0.00000000
timestamp mismatches 0  condition mismatches 0
```

价格最大差异约 `4.85e-11`（data-api 只保留 10 位小数），数量、金额、方向、
时间戳、condition_id 均完全一致。完整 JSON 见 VPS
`/root/polycopy-onchain/data/verify_f705_50.json`。

另外对高频 maker 钱包 `0xeee92f1cc6d6e0ad0b4ffda20b01cf3678e27ecb`
取 20 笔（30 分钟、18 个 tx，含同一 tx 内多笔成交）复核：20/20 通过，
最大价格误差 `4.65e-11`，`verify_eee9_20.json`。这覆盖了限价单 maker
成交以及“一个 tx 多笔 OrderFilled”的匹配逻辑。

## 3. 阶段二：并行纸盘

### 部署

- data-api 纸盘（现有）：`/root/polycopy`，进程 PID 在 `data/paper.pid`
- 链上纸盘（新增）：`/root/polycopy-onchain`，PID 在 `data/onchain.pid`
- 两个进程读取同一份目标配置 `/root/polycopy/configs/paper_f705.toml`，
  但写不同的 DB / 日志：
  - data-api：`/root/polycopy/data/paper_f705.db`
  - 链上：`/root/polycopy-onchain/data/paper_f705_onchain.db`
  - 逐笔延迟明细：`/root/polycopy-onchain/data/onchain_trace.jsonl`
- 链上库启动时从 data-api 库复制了 `target_positions`，使两边的目标持仓账本
  一致；纸盘账户本身从 $10,000 重新开始。

### 启动 / 运维

也可以在本机用 `scripts/onchain_ctl.sh {deploy|start|stop|status|report|compare|verify|log}`
（需要 `POLY_HOST` / `POLY_KEY`；默认目录 `/root/polycopy-onchain`），
等价于下面的命令。

```bash
cd /root/polycopy-onchain

# 启动（--lookback 0 表示从现在开始；重启会从 kv 游标续传）
setsid nohup /root/polycopy/.venv/bin/python -m polycopy onchain run \
  --config /root/polycopy/configs/paper_f705.toml \
  --db data/paper_f705_onchain.db \
  --log data/paper_f705_onchain.log \
  --trace data/onchain_trace.jsonl \
  --lookback 0 >> data/onchain.out 2>&1 < /dev/null &
echo $! > data/onchain.pid

# 状态 / 报告
tail -f data/paper_f705_onchain.log
/root/polycopy/.venv/bin/python -m polycopy report \
  --config /root/polycopy/configs/paper_f705.toml \
  --db data/paper_f705_onchain.db

# 停止
kill $(cat data/onchain.pid)
```

### 对比延迟与滑点

```bash
/root/polycopy/.venv/bin/python /root/polycopy-onchain/scripts/compare_sources.py \
  /root/polycopy/data/paper_f705.db \
  /root/polycopy-onchain/data/paper_f705_onchain.db \
  --verbose --json-out /root/polycopy-onchain/data/compare.json
```

脚本按 `(transaction_hash, side, token_id)` 配对两边的信号，输出：

- 同一笔成交的 `latency_seconds`：链上 vs data-api
- 链上相对 data-api 的领先时间（`lead = data-api 延迟 - 链上延迟`）
- 两边都纸盘成交时的逆向滑点（相对目标成交价，c/share）及改善值
- 只在一边出现的信号与跳过原因

默认以链上账户创建时间作为对比起点；若只想比较最近一次链上进程启动后的
信号，加 `--since <epoch>`。

`latency_seconds` 的统一定义是“信号落库时间 − 区块/成交时间戳”，所以
两边可直接比较。链上侧的 `onchain_trace.jsonl` 还额外拆分：

- `feed_delay_s`：WebSocket 收到日志 − 区块时间戳
- `queue_delay_s`：进入处理队列到开始处理
- `handle_s`：元数据解析 + 盘口抓取 + 引擎处理

### 实测延迟

2026-11-23 10:30 UTC 用高频钱包 `0xeee9…` 做实时（非回放）冒烟：WebSocket
收到区块 `97816252` 的成交 `feed_delay_s = -0.30`（区块时间戳略快于本机
时钟），从成交到纸盘成交落库 `latency_seconds = 0.36` 秒；目标价 0.10，
纸盘以 0.101 成交。

另做了一次受控 data-api 索引探针（同一钱包，75 秒窗口，样本 1 笔）：成交在
data-api activity 中可查询的时间是区块时间戳后 `1.32` 秒、我们收到
WebSocket 后 `2.48` 秒。因此 data-api 纸盘 3–20 秒的延迟主要来自 5 秒轮询
周期 + API 调用/处理，而不是索引本身。f705 的并行对比跑起来后会用匹配样本
给出分布。

第一笔真实匹配信号（2026-11-23 11:45:29 UTC，f705 SELL 800 @ 0.98301875）：

- data-api 延迟 `6.819` 秒；链上延迟 `0.295` 秒；链上领先 `6.524` 秒
- 链上 trace：`feed_delay=-0.070s`、`queue_delay=0.365s`、`handle=0.015s`
- 两边都因 `target_no_position` 跳过；dedupe key 完全一致
- `compare_sources.py`：matched=1，only_data_api=0，only_onchain=0

这笔是 98.3c 的卖单，不在 30–70c 买入桶；5% 仓位和 60–70c 滑点样本仍待
下一笔符合条件的买单。

### 可靠性设计

- **订阅**：一条 `eth_subscribe(logs)`，同时过滤两个 V2 交易所和所有目标
  钱包（`topics[2]` 为 padded maker 地址列表）。
- **补洞**：即使订阅正常，也每 15 秒用 `eth_getLogs` 从
  `last_block + 1` 补拉一次；WebSocket 断线重连后同样先补拉再收实时。
  drpc 免费版单次 `eth_getLogs` 最多 100 个区块，因此默认
  `catchup_chunk = 100`（publicnode 也支持），长间隔补拉会自动分片。
- **去重**：进程内用 `(tx_hash, log_index)` 去重；数据库
  `target_fills.dedupe_key` 与 data-api row 完全一致（tx/type/condition/
  token/side/size/price/block timestamp），因此将来两条链路写入同一个 DB
  时，同一笔成交也只会处理一次。
- **游标**：`kv.onchain:last_block` 在队列排空且区块号稳定后落库；崩溃最多
  回放几个区块，由去重兜底。
- **降级**：`--no-ws` 或未安装 `websockets` 时自动退化为 HTTP
  `eth_getLogs` 轮询（1 秒）。
- **元数据**：首次见到 token 时调用 CLOB
  `/markets-by-token/{id}` + `/markets/{condition_id}`，解析
  condition_id、outcome、title、slug，并缓存到内存和 SQLite；之后零额外
  延迟。

## 4. 已知限制

- 只覆盖 V2 交易所；历史 V1 成交需要另加解码分支（当前近期成交不需要）。
- 元数据首次解析约 0.2–0.4 秒，只影响该 token 的第一笔信号。
- `event_slug` 默认留空；只有配置了 `exclude_event_substrings` 时才调用
  Gamma 补全，避免给每笔信号增加一次请求。
- 公共 RPC 可能限流或抖动：PoC 中 drpc 30/30 次 `eth_getLogs` 成功，
  publicnode 偶发 `invalid block range params`（30 次中 2 次）。默认顺序已
  改为 drpc 主用、publicnode 兜底；另有 HTTP 补拉和轮询降级。生产替换前
  建议加一个付费 RPC（Alchemy/Infura/QuickNode）作为主用。
- 未做链重组确认（确认数会额外增加约 2 秒延迟）；纸盘阶段接受这一风险。
- **未处理 REDEEM / MERGE / SPLIT**：当前只订阅 `OrderFilled`，目标的
  `target_positions` 账本在这些事件后会和 data-api 纸盘漂移，影响后续
  SELL 的复制比例。PoC 启动时从 data-api 库复制了一次目标持仓；正式替换
  前需要补 CTF 的 `PayoutRedemption` / `PositionsMerge` / `PositionSplit`
  日志，或定期用 data-api `/positions` 做对账。
- 链上进程启动时固定订阅当时的 enabled targets；运行中新增钱包需要重启。

## 5. 替换主链路的门槛

建议并行跑满至少 24 小时，满足以下条件后把 `polycopy run` 的信号源切到
链上（保留 data-api 作为兜底/对账）：

1. `compare_sources.py` 中 matched 信号覆盖率 ≥ 99%，无“只在 data-api”
   的成交（排除启动前历史）。
2. 链上 `latency_seconds` 中位数 < 1.5 秒，且相对 data-api 领先中位数
   ≥ 3 秒。
3. 两边都成交的样本里，链上逆向滑点中位数不劣于 data-api。
4. 链上纸盘连续 24 小时无 `metadata resolution failed`、无断线漏单；
   `onchain_trace.jsonl` 中 `feed_delay_s` 稳定。
5. 切换后保留 data-api 影子模式至少一周，只对账不交易。

## 6. 切换步骤（验证通过后）

1. 先补齐第 4 节的 REDEEM/MERGE/SPLIT 缺口；否则 SELL 的复制比例会漂移。
2. 给链上进程配置付费 RPC（HTTP + WSS 同一家），保留公共 RPC 作为兜底。
3. 停掉 data-api 主进程，把 `--db` 指向正式库，用 `onchain run` 启动：
   ```bash
   kill $(cat /root/polycopy/data/paper.pid)
   cd /root/polycopy-onchain
   setsid nohup /root/polycopy/.venv/bin/python -m polycopy onchain run \
     --config /root/polycopy/configs/paper_f705.toml \
     --db /root/polycopy/data/paper_f705.db \
     --log data/paper_f705_onchain.log \
     --trace data/onchain_trace.jsonl --lookback 300 \
     >> data/onchain.out 2>&1 < /dev/null &
   echo $! > data/onchain.pid
   ```
   `dedupe_key` 与 data-api 完全一致，所以即使之后把 data-api 作为影子源写回
   同一个库，也不会重复下单。
4. data-api 影子进程用单独的 DB 继续跑，定期执行
   `scripts/compare_sources.py <影子库> <正式库>` 对账。
5. 监控：`onchain heartbeat connected=True`、`errors=0`、
   `queue` 持续为 0、`feed_delay_s` 中位数稳定；连续断线超过 30 秒告警。

5% 仓位的并发敞口与 60–70c 薄 edge 滑点监控见
[`docs/sizing_5pct.md`](sizing_5pct.md)。
