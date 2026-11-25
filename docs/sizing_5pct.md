# 5% 跟单与敞口/滑点监控（2026-11-23）

## 1. 配置变更

`configs/paper_f705.toml`（VPS 上 `/root/polycopy/configs/paper_f705.toml`，
两个纸盘进程共用并热加载）：

```toml
scale_factor = 0.05        # 1% -> 5%（目标成交额的 5%）
max_notional_usd = 500.0   # 100 -> 500
```

其余风控保持不变：

- `min_notional_usd = 2.0`
- `max_market_usd = 500.0`（单 condition 成本上限，5% 下会更常触发）
- `max_open_positions = 300`
- `cash_reserve_pct = 0.05`
- `buy_price_min/max = 0.30 / 0.70`
- `fee_rate = 0.01`（月/周 crypto 市场；BTC 5m/15m 为 0.05）

2026-11-23 11:37 UTC 两个进程都已热加载生效（A 的
`data/paper_f705.log`、B 的 `data/paper_f705_onchain.log` 均有
`config reloaded`）。

> 2026-11-24 修正：原 `fee_rate=0.07` 是旧/全局值。Gamma
> `feeSchedule` 与 30/30 笔链上 taker 成交都确认 f705 的月/周市场是
> **0.01**；BTC 5m/15m 是 **0.05**；maker 为 0（takerOnly）。因此
> 60–70c 分桶的实际手续费约为旧 paper 的 1/7，下面的 `net_med` 对新单会更高。

## 2. 为什么 5% 需要额外监控

f705 的 30–70c one-sided 历史样本（1,589 笔 BUY，424 天；VWAP 已含
target 的 taker fee，所以 edge 是费后）：

| 子桶 | 笔数 | VWAP | 胜率 | 费后 edge/股 | ROI/笔 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 30–50c | 930 | 0.377 | 56.0% | +18.3c | 48.6% |
| 50–60c | 272 | 0.552 | 67.3% | +12.1c | 21.9% |
| 60–70c | 387 | 0.660 | 72.1% | +6.1c | 9.3% |
| **30–70c** | **1,589** | **0.474** | **61.9%** | **+14.4c** | **30.5%** |

风险点：

- 月度胜率在 14%–92% 之间波动，5% 会同比放大回撤。
- **60–70c 只有约 6.1c/股 edge**，5 倍仓位后 1–2c 的滑点就会显著侵蚀。
- 30–70c 目标成交额：p50 `$315`、p90 `$2,560`、p99 `$6,245`、max `$7,147`。
  5% 对应 `$15.8 / $128 / $312 / $357`，`$500` 上限基本不截断。
- 当前纸盘在 30–70c 桶还没有实际跟单样本；以下监控就是用来补这个证据。

## 3. 并发敞口记录

独立只读监控进程（不改交易逻辑）：

```bash
cd /root/polycopy-onchain
setsid nohup /root/polycopy/.venv/bin/python scripts/exposure_monitor.py \
  --db data-api=/root/polycopy/data/paper_f705.db \
  --db onchain=/root/polycopy-onchain/data/paper_f705_onchain.db \
  --out data/exposure.jsonl --interval 60 \
  >> data/exposure.out 2>&1 < /dev/null &
echo $! > data/exposure.pid
```

- 每 60 秒为两个库各写一行 JSON 到 `data/exposure.jsonl`
- 字段：`open_positions`、`open_conditions`、`invested`（成本）、
  `market_value`（有 mark 用 mid，否则 avg_cost）、`cash`、`equity`、
  `realized_pnl`、`unrealized_pnl`、`cash_utilization`、
  `max_market_exposure`（单 condition 成本）、`signals`、`copied_signals`
- 只读连接（`mode=ro`），与 A/B 写入并行安全
- 当前 PID 在 `data/exposure.pid`，日志 `data/exposure.out`
- 停止：`kill $(cat data/exposure.pid)`

本机快捷方式（需要 `POLY_HOST` / `POLY_KEY`）：

```bash
scripts/onchain_ctl.sh monitor-start
scripts/onchain_ctl.sh monitor-status
scripts/onchain_ctl.sh monitor-stop
scripts/onchain_ctl.sh bucket
```

## 4. 分桶滑点报告

```bash
/root/polycopy/.venv/bin/python /root/polycopy-onchain/scripts/bucket_report.py \
  --db data-api=/root/polycopy/data/paper_f705.db \
  --db onchain=/root/polycopy-onchain/data/paper_f705_onchain.db \
  --exposure /root/polycopy-onchain/data/exposure.jsonl \
  --json-out /root/polycopy-onchain/data/bucket_report.json
```

输出：

- 30–50 / 50–60 / 60–70c 的 BUY 信号数、copied 数、跳过原因
- 已成交样本的滑点：`our_price - target_price`（c/share，正数=更差），
  中位数 / 均值 / p90 / max
- 每笔我们的手续费（c/share）
- 历史 edge budget（18.3 / 12.1 / 6.1c）和 `net_med`：
  `edge - 中位滑点 - 中位手续费`（保守，手续费重复扣了一次）
- 60–70c 标记规则（只有这个薄 edge 桶用 2c/4c 阈值）：
  - 中位滑点 > 2.0c，或 p90 > 4.0c，或 `net_med <= 0` → `WARNING thin-edge`
- 30–50 / 50–60c：`net_med <= 0` 或中位滑点 > 50% edge 才告警
  （例如 30–50c edge 18.3c，2–3c 滑点仍属正常）
- 并发敞口汇总：最大/中位持仓数、最大投入、最大现金利用率、
  单 condition 最大敞口、权益最大回撤、当前快照

首笔匹配信号（2026-11-23 11:45:29 UTC）是 f705 的 `SELL 800 @ 0.983`
（98.3c），两边都因 `target_no_position` 跳过；链上延迟 0.295s，data-api
延迟 6.819s，领先 6.524s。它不是 30–70c 买单，所以 5% 仓位的实际滑点和
60–70c 薄 edge 样本仍待下一笔符合条件的 BUY。

初始报告（2026-11-23 11:39 UTC）：

- data-api：20 条信号，2 笔 copied（都是 >70c 的旧配置单）；
  >70c 中位滑点 0.17c、p90 0.71c；30–70c 尚无跟单
- on-chain：0 条信号；敞口 0
- data-api 当前敞口：2 个持仓，投入 `$41.64`，现金利用率 `0.4%`

首笔 30–50c 跟单（2026-11-23 14:10:35 UTC）：

- 目标：BUY 552.342858 @ 0.3402777772，$189.18994
- 5% 预算：$9.4595 → 27.799338 股（5% 比例验证通过）
- data-api：延迟 2.091s，成交 0.3628056，逆向滑点 +2.25c
- 链上：延迟 0.044s，成交 0.3557047，逆向滑点 +1.54c
- 链上优 0.71c/股；总成本 $10.334 vs $10.536
- 30–50c 分桶：data-api `net_med 14.43c`、链上 `net_med 15.15c`
  （历史 edge 18.3c），两个都判定为 `ok`
- 60–70c 仍为 0 笔，薄 edge 样本待出现

## 5. 交易日志中的即时记录

B（链上纸盘，新代码）的成交日志已增加成交前敞口字段：

```text
COPY BUY  f705 px=0.660 sh=... $=... fee=... open=12 mkt=$345.20 cash=$9650.10 ...
THIN-EDGE 60-70c buy target=0.6600 our=0.6720 slip=+1.20c open=12 mkt=$345.20 cash=$9650.10 ...
COPY SELL f705 px=... open=12 mkt=$... cash=$...
```

- `open`：成交前全账户未平仓数
- `mkt`：成交前该 condition 的成本敞口
- `cash`：成交前现金
- `THIN-EDGE` 只在 60–70c 买入时输出，`slip` 正数表示比目标成交价更差

A（data-api 旧进程）不会输出这些新字段；`bucket_report.py` 仍可从
`signals.price/our_price/our_fee` 计算 A 的滑点，敞口由
`exposure_monitor.py` 单独记录。

## 6. 观察阈值

- **60–70c**：若 `WARNING thin-edge` 持续出现，优先降低 60–70c 子桶的
  `scale_factor`（例如 2–3%）或直接排除 60–70c，而不是整体降回 1%。
- **现金利用率**：>70% 时注意 `insufficient_cash` / `max_market_exposure`
  跳过率；当前 `max_market_usd=500`、`max_open_positions=300` 未改。
- **单 condition 敞口**：接近 `$500` 时说明 `max_market_usd` 开始截断，
  需要决定是否提高上限或接受截断。
- **回撤**：`max_drawdown_pct` 超过可接受阈值（纸盘先按 10–15% 观察）时
  暂停加仓/回到 2–3%。
- **样本量**：30–70c 至少 50–100 笔 copied 再评估是否维持 5%。

## 7. 已知限制

- 敞口监控是 60 秒快照，不记录每笔成交瞬间的精确敞口；对约 4 笔/天的
  f705 足够，但不适合高频策略。
- `bucket_report.py` 的 edge budget 来自 f705 历史 one-sided 样本，是
  目标历史 edge，不是我们复制后的保证；滑点变大时以实际 `net_med` 为准。
- REDEEM/MERGE/SPLIT 未订阅，链上 B 的目标持仓账本仍可能漂移（见
  `docs/onchain_poc.md`）。
