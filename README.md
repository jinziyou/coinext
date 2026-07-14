# Coinext

多资产、与交易场所无关的量化综合平台，覆盖 **数据接入 → 策略研究 → 回测仿真 → 分析优化 → 风控组合 → 执行实盘 → 运维控制面**。仓库仍是一个 monorepo，但业务源码按量化交易生命周期拆成 8 个根级模块目录；**没有 `modules/` 前缀**。

热路径（行情接入、订单执行、确定性内核）是 **Rust 1.95 on Tokio**；控制面（策略编写、研究、分析、运维）是 **Python 3.13 + uv**。Rust 与 Python 之间仅通过 PyO3/maturin 桥 `foundation/crates/coinext-py` 连接，Python 侧导入名保持 `coinext_*` 不变。

核心不变量仍是 **backtest↔live parity**：

> 同一套 Strategy API，同一组 Data / Execution / Risk / Portfolio 引擎，同一条确定性同步核心循环。Backtest / Sandbox / Live 之间，Kernel 只替换 Clock、Cache 内容，以及 Data/Execution 端口后的客户端。设计冲突一律以一致性为准。

当前状态要诚实区分：确定性回测核心、Python 回测桥、数据湖、分析优化、衍生品、风控组合已有测试覆盖；`LiveKernel`、`ingestor`、`exec-svc`、服务/UI 仍是 scaffold/stub；真实场所端到端 parity 尚未验证。完整设计见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 根级模块状态

| 根级模块 | 关键功能域 | 当前状态 |
|---|---|---|
| `foundation/` | 固定精度值类型、领域模型、端口、状态缓存、Python 契约、运行配置、PyO3 桥、testkit | ✅ 核心契约已实现；PyO3 桥用于回测路径；默认配置在 `foundation/config` |
| `market-data/` | 数据引擎、Parquet 数据湖、REST/WS transport、Binance adapter、全球股市 venue 目录、ingestion service | ✅ 数据湖/HistoryReader/Binance+Yahoo 公共下载/股市 catalog 已测试；adapter/network 有单测；`ingestor` 为 partial |
| `strategy-research/` | Python Strategy API、Rust/Python 指标、research notebooks | ✅ 策略 API、SMA/RSI 等流式指标、research-loop 脚本已测试（需要构建 `coinext_py`） |
| `backtesting-simulation/` | deterministic Kernel、SimulatedExchange、authoritative runner、parity gates、Rust example | ✅ 回测内核、模拟交易所、Python runner、示例 SMA 回测已测试；recorded sandbox-session replay gate 已测试，真实 testnet fills 捕获仍需 spot-testnet keys |
| `analytics-optimization/` | metrics、tear sheet、bias screens、vectorized screen、walk-forward optimizer、derivatives pricing | ✅ 分析、筛选、优化、Black-Scholes/Greeks/IV 已测试 |
| `risk-portfolio/` | 风控门禁、风险 facade、组合引擎、组合 facade、risk-monitor service | ✅ pre-trade risk、margin/liquidation、portfolio PnL/exposure 已测试；`risk-monitor` 是 out-of-band scaffold |
| `execution-live/` | execution engine、exec service、live runtime、trader service | ✅ OMS/FSM folding crate 已实现；`LiveKernel`、`coinext_live`、`trader`、`exec-svc` 是 scaffold/stub |
| `operations-interface/` | Redis/in-proc bus、CLI、FastAPI、React UI、deployment、persistence | ✅ bus/persistence/API auth/control-plane paths 有测试； API/UI/deployment 可解析但仍是 scaffold |

## 快速开始：Rust 核心

```bash
cargo test --workspace          # 或: just test
cargo run -p coinext-example-backtest
```

`coinext-example-backtest` 位于 `backtesting-simulation/examples/backtest-sma`。它通过 Rust `Strategy` + `SimulatedExecutionClient` 跑同一组引擎，输出 tear-sheet 风格摘要（订单、成交、权益、收益、Sharpe、最大回撤）。

Workspace 外的 live-edge crates 单独验证：

```bash
just test-live-edge
```

覆盖 network、Binance adapter、persistence、ingest stub、exec-svc stub 的 manifest paths。

## 快速开始：Python 研究控制面

```bash
just py-setup
just py-build     # maturin develop --manifest-path foundation/crates/coinext-py/Cargo.toml --features python

uv run coinext venues                              # 全球主流股市 + 加密 venue 目录
uv run coinext download --symbols BTCUSDT,ETHUSDT --interval 1m --days 30

# 美股 / 港股 / A股 / ETF（Yahoo 公共历史，无需 key）
uv run coinext download --venue NASDAQ --symbols @default --interval 1d --days 365
uv run coinext download --venue US --symbols AAPL,JPM,SPY --interval 1d --days 365   # 美股市场组
uv run coinext download --venue HKEX --symbols 0700,2800 --interval 1d --days 365     # 港股 + 港股 ETF
uv run coinext download --venue ASHARE --symbols 600519,000001,510300 --interval 1d --days 365  # A股自动路由 SSE/SZSE
uv run coinext download --venue SSE --symbols @etf --interval 1d --days 365           # A股 ETF 预设
uv run coinext download --venue NYSE --symbols @etf --interval 1d --days 365          # 美股 ETF 预设
uv run coinext download-fx --pairs USDCNY,USDHKD --days 365                             # 多币种 FX
# A股纸交易回放（T+1 / 涨跌停；Kernel 回测对 SSE/SZSE equity 同样 enforce T+1）
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext paper-equity --venue SSE --symbol 510300
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext paper-equity --venue ASHARE --symbol @default --multi
# IB TWS paper 连通性（需 ib_insync + 运行中的 TWS，见 docs/IB_PAPER.md）
# uv run coinext ib-status

uv run coinext catalog --venue ALL
uv run coinext backtest --from-lake --symbol BTCUSDT
# 离线 sample 股票 / ETF fixture（已提交 data/sample）
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext backtest \
  --venue NASDAQ --symbol AAPL --from-lake --interval 1d
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext backtest \
  --venue SSE --symbol 510300 --from-lake --interval 1d
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext backtest-multi \
  --venue INDEX --symbols '^GSPC,^HSI' --from-lake --interval 1d
uv run coinext optimize --from-lake --mode anchored
```

`coinext backtest` 使用 authoritative event-driven runner；`coinext screen` 是快速向量化扫描，只能收窄参数空间，不能替代 event-driven parity surface。数据湖运行态根仍是仓库根 `data/` 或容器内 `/data`，不是源码模块。

**股票 / ETF 研究路径（已支持）：** A股（SSE/SZSE，别名 `ASHARE`/`A股`）、美股（NYSE/NASDAQ/AMEX，别名 `US`/`美股`）、港股（HKEX，别名 `HK`/`港股`）、ETF（`--symbols @etf` 或与股票相同 venue）。历史走 Yahoo 公共接口（无需 key）；`--symbols @default` 为蓝筹股票池，`@etf` 为流动性 ETF 池。A股代码按首位自动路由（6/5→上交所，0/1/3→深交所）；也支持 `sh600519` / `sz000001` / `hk0700` 前缀。CLI 在股票 venue 上会把加密默认的 `1m×7d` 改成 `1d×365`；回测自动套用分市场费率/整手精度（`instrument_spec`）。日线自动过滤周末/节假日/停牌扁条；跨币种组合用 `--base-ccy USD` 统一计价（sample 含 `FX/USDCNY`、`FX/USDHKD`）。纸交易 `PaperEquityBroker` 支持 A股 **T+1 / 涨跌停**；**Kernel 回测** 对 SSE/SZSE `Equity` 同样在 OMS 层拒绝 T+0 卖出（`orders_denied` / `TPlusOne`）。IB TWS paper：`IbPaperBroker(mode="ib")` + `coinext ib-status`（`docs/IB_PAPER.md`，`uv sync --extra ib`）。**Rust Kernel 股票实盘 ExecutionClient 尚未接入。**

Research loop 脚本位于 `strategy-research/notebooks`。直接运行：

```bash
uv run python strategy-research/notebooks/research_loop.py
```

默认使用合成数据；把脚本内 `USE_LAKE = True` 可切到真实本地 lake。

## 仓库布局

每个生命周期模块内部统一为 `crates/`（Rust）、`python/`（纯 Python 包）、可选 `services/`（可部署入口）：

```text
foundation/                 crates/ + python/ + config/（领域契约、PyO3、默认 YAML）
market-data/                crates/ + python/ + services/ingestor
strategy-research/          crates/ + python/ + notebooks/
backtesting-simulation/     crates/ + python/ + examples/
analytics-optimization/     crates/ + python/
risk-portfolio/             crates/ + python/ + services/risk-monitor
execution-live/             crates/ + python/ + services/trader
operations-interface/       crates/ + python/ + services/{api,ui} + deployment/

data/                       本地/compose 数据湖运行态根（保持在仓库根）
Cargo.toml / pyproject.toml 根 workspace 与 Python discovery
docker-compose*.yml         根 compose 入口；镜像定义在 operations-interface/deployment/docker
tests/                      按生命周期模块分组
docs/                       索引、路线图、runbook；权威架构见根 ARCHITECTURE.md
```

## 文档地图

完整索引见 [`docs/README.md`](docs/README.md)。

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — 权威设计：领域模型、六边形端口、Kernel、数据流、部署形态（英文）。
- [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) — 目录约定、如何加 crate/包、本地检查。
- [`docs/EQUITY_RESEARCH.md`](docs/EQUITY_RESEARCH.md) — A股/港股/美股/ETF 研究最短路径。
- [`docs/IB_PAPER.md`](docs/IB_PAPER.md) — Interactive Brokers paper 联调。
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — 状态快照、下一步、延后 live/ops。
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — 已验证能力的历史叙事。
- [`docs/STATUS.md`](docs/STATUS.md) — verified / partial / scaffold 标注约定。
- [`docs/TESTNET.md`](docs/TESTNET.md) — Binance market data + spot testnet paper execution runbook。
- [`tests/backtesting-simulation/parity/README.md`](tests/backtesting-simulation/parity/README.md) — advisory cross-check 与 sandbox gate 说明。
- [`operations-interface/deployment/README.md`](operations-interface/deployment/README.md) — compose、Docker、observability。
- [`operations-interface/deployment/services.md`](operations-interface/deployment/services.md) — deployable service index。

## 工具链

Rust **MSRV 1.95**（CI/本机用 stable）、Python 3.13（uv）、Node 22（dashboard）、Docker。常用检查：

```bash
just test
just test-live-edge    # 产物在 target/live-edge，避免 crate 下嵌套 target/
just py-test
just py-lint
just compose-check
just clean-targets     # 清理 cargo target（含历史嵌套目录）
```

## 许可证

MIT。
