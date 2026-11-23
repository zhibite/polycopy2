# polycopy — Polymarket 纸盘跟单系统

用一个或多个 Polymarket 钱包的公开成交作为信号，在**本地纸上账户**里模拟跟单：
真实的订单簿成交价、Polymarket taker 手续费、按目标仓位比例卖出、到期结算。
不接触私钥、不下真实订单。

当前默认目标：`0xf705…53ca7`（"mo-money" 方向性巨鲸）。筛选依据见
`analysis_f705.txt`：该地址 81% 的利润来自 **30–70c 的方向性仓位**，所以默认只跟
这个价格区间的主动买单，并同步跟卖。

## 目录结构

```
polycopy/
  config.py       配置（TOML）
  api.py          data-api / CLOB / Gamma 客户端
  http.py         带重试的 HTTP
  store.py        SQLite（账户、信号、持仓、成交、结算、标记）
  broker.py       盘口行走 + 手续费模型
  rules.py        信号过滤与仓位计算
  engine.py       轮询、跟单、结算、mark-to-market
  report.py       控制台报告 / CSV 导出
  cli.py          python -m polycopy
  web/            FastAPI 中文控制台（端口 8040）
configs/paper_f705.toml
tests/            纯本地单元测试（无需网络）
scripts/          paper_ctl.sh、fetch_wallet.py、analyze_wallet.py
Dockerfile / docker-compose.yml / DEPLOYMENT.md
```

## Web 控制台

- 地址：`https://polycopy.bquant.com`（Coolify + Traefik，容器端口 **8040**）
- 登录：账号密码通过 `WEB_USERNAME` / `WEB_PASSWORD` 环境变量设置（参考 `.env.example`）
- 页面：控制台（权益/盈亏/持仓/最近交易/跳过原因）、交易记录、持仓、跟单信号、策略配置
- 策略配置保存后写入 TOML，引擎在下一次轮询时**自动热加载**
- 部署步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)

本地调试：

```bash
pip install -r requirements.txt
WEB_USERNAME=admin WEB_PASSWORD='<你的登录密码>' WEB_PORT=8040 python -m polycopy.web
```

## 快速开始（本地，仅测试）

```bash
python -m unittest discover -s tests -v
```

本地机器无法直连 Polymarket API，实际运行请在服务器上。

## 部署与运行（VPS）

服务器：你的 VPS（通过环境变量配置 `POLY_HOST` / `POLY_KEY`）。

```bash
export POLY_HOST=root@your-vps
export POLY_KEY=~/.ssh/your-key.pem

# 1. 上传代码
scripts/paper_ctl.sh deploy

# 2. 建库 / 单次连通性测试
scripts/paper_ctl.sh init
scripts/paper_ctl.sh once

# 3. 常驻纸盘
scripts/paper_ctl.sh start
scripts/paper_ctl.sh status
scripts/paper_ctl.sh report
scripts/paper_ctl.sh log
scripts/paper_ctl.sh stop
```

也可以直接 ssh 进入 `/root/polycopy` 手动执行：

```bash
python3 -m polycopy run    --config configs/paper_f705.toml
python3 -m polycopy report --config configs/paper_f705.toml --csv data/reports
python3 -m polycopy settle --config configs/paper_f705.toml
```

## 配置要点（`configs/paper_f705.toml`）

| 键 | 含义 | 默认 |
|---|---|---|
| `account.starting_cash` | 纸盘初始资金 | $10,000 |
| `paper.poll_seconds` | 轮询目标活动的间隔 | 20s |
| `paper.fee_rate` | 手续费率，`fee = shares × rate × p × (1-p)` | 0.01 |
| `paper.max_buy_drift` | 买入时最多比信号价高多少 | $0.03 |
| `paper.max_sell_drift` | 卖出时最多比信号价低多少 | $0.10 |
| `paper.min_fill_ratio` | 盘口深度不足时最低成交比例 | 0.5 |
| `targets.<name>.buy_price_min/max` | 跟单买入价格区间 | 0.30–0.70 |
| `targets.<name>.scale_factor` | 我们的名义金额 = 目标成交额 × 该系数 | 0.02 |
| `targets.<name>.min/max_notional_usd` | 单笔跟单金额上下限 | $2 / $100 |
| `targets.<name>.min_target_usd` | 过滤目标的小额成交 | $20 |
| `targets.<name>.max_market_usd` | 单市场最大敞口 | $300 |
| `targets.<name>.max_open_positions` | 最大同时持仓数 | 100 |
| `targets.<name>.copy_sells` | 是否按比例跟卖 | true |
| `targets.<name>.fee_rate` | 覆盖全局费率（如 BTC 5m/15m 用 0.05） | 继承 |

多目标：直接增加 `[targets.xxx]` 段，各目标独立过滤/仓位；账户与现金共享。
`/activity` 包含目标所有成交（含其挂单成交），对做市型目标请谨慎启用。

## 纸盘成交模型

1. **信号**：轮询 `data-api /v2/activity`，按 `transaction_hash+token+side+size+price+ts`
   去重；只处理 `TRADE`。`REDEEM/MERGE/SPLIT` 用于维护目标仓位账本。
2. **买入过滤**：价格区间、目标成交额、事件黑名单、市场敞口、持仓数、信号新鲜度。
3. **仓位**：`min(目标成交额 × scale_factor, max_notional, 市场剩余额度, 可用现金)`。
4. **成交**：实时拉取 CLOB `/book`，按档位吃单得到 VWAP；若 VWAP 超过
   `信号价 + max_buy_drift` 或深度不足则放弃（记录原因）。
5. **卖出**：目标卖出其持仓的 `frac` 比例时，卖出我们仓位的同比例份额
   （目标仓位账本由我们观测到的成交推算）。
6. **手续费**：`shares × fee_rate × p × (1-p)`，买入计入成本、卖出计入实现盈亏。
7. **结算**：每 5 分钟用 Gamma 检查持仓市场；`closed` 且 `outcomePrices` 为 1/0 时，
   赢的一边按 $1、输的一边按 $0 结算。MERGE 不单独模拟——配对两边持有到结算的
   最终 PnL 与提前 merge 相同。

## 报告

`python3 -m polycopy report` 输出：现金、持仓市值、已实现/未实现盈亏、手续费、
信号数与跳过原因分布、跟单延迟、前 25 个持仓、最近成交。`--csv <dir>` 导出
`positions.csv / fills.csv / signals.csv`。

跳过原因常见值：`price_out_of_range`（绝大多数）、`target_too_small`、
`stale`、`max_market_exposure`、`insufficient_depth`、`drift`、`no_position`。

## 已知局限

- 纸盘假设我们能在当前盘口吃到量；真实成交会进一步推动价格（我们的量很小，影响有限）。
- 活动接口有秒级到分钟级的聚合延迟；`stale_signal_seconds` 会丢弃过期信号。
- 不复制目标在我们启动前已有的仓位；只跟启动后的新成交。
- 不模拟 MERGE/SPLIT 对现金的时点影响（最终 PnL 一致，资金占用时间不同）。
- 费率按文档公式近似；如需精确，可对每个市场调用 `/fee-rate` 或按类别覆盖。
- 目标仓位账本是观测推算值，部分平台操作（如 split 后立刻卖出）会造成再平衡偏差。

## 后续可做

- CLOB websocket（行情/成交流），把跟单延迟从秒级降到亚秒级。
- 实盘执行层（py-clob-client / 签名下单），用同一套规则与风控。
- 逐钱包的归因报告：哪些信号族贡献 PnL，哪些应继续过滤。
- 多目标与组合级风险（相关性、单事件集中度）。
