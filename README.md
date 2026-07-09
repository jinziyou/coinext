# Coinext

多资产、与交易场所无关的量化综合平台，覆盖 **数据接入 → 策略研究 → 回测仿真 → 分析优化 → 风控组合 → 执行实盘 → 运维控制面**。仓库仍是一个 monorepo，但业务源码按量化交易生命周期拆成 8 个根级模块目录；**没有 `modules/` 前缀**。

热路径（行情接入、订单执行、确定性内核）是 **Rust 1.95 on Tokio**；控制面（策略编写、研究、分析、运维）是 **Python 3.13 + uv**。Rust 与 Python 之间仅通过 PyO3/maturin 桥 `foundation/ffi-bridge/rust/coinext-py` 连接，Python 侧导入名保持 `coinext_*` 不变。

核心不变量仍是 **backtest↔live parity**：

> 同一套 Strategy API，同一组 Data / Execution / Risk / Portfolio 引擎，同一条确定性同步核心循环。Backtest / Sandbox / Live 之间，Kernel 只替换 Clock、Cache 内容，以及 Data/Execution 端口后的客户端。设计冲突一律以一致性为准。

当前状态要诚实区分：确定性回测核心、Python 回测桥、数据湖、分析优化、衍生品、风控组合已有测试覆盖；`LiveKernel`、`ingestor`、`exec-svc`、服务/UI 仍是 scaffold/stub；真实场所端到端 parity 尚未验证。完整设计见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 根级模块状态

| 根级模块 | 关键功能域 | 当前状态 |
|---|---|---|
| `foundation/` | 固定精度值类型、领域模型、端口、状态缓存、Python 契约、运行配置、PyO3 桥、testkit | ✅ 核心契约已实现；PyO3 桥用于回测路径；默认配置在 `foundation/runtime-config/config` |
| `market-data/` | 数据引擎、Parquet 数据湖、REST/WS transport、Binance adapter、ingestion service | ✅ 数据湖/HistoryReader/公共数据下载已测试；adapter/network 有单测；`ingestor` 是 stub daemon |
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

`coinext-example-backtest` 位于 `backtesting-simulation/examples/rust/backtest-sma`。它通过 Rust `Strategy` + `SimulatedExecutionClient` 跑同一组引擎，输出 tear-sheet 风格摘要（订单、成交、权益、收益、Sharpe、最大回撤）。

Workspace 外的 live-edge crates 单独验证：

```bash
just test-live-edge
```

覆盖 network、Binance adapter、persistence、ingest stub、exec-svc stub 的 manifest paths。

## 快速开始：Python 研究控制面

```bash
just py-setup
just py-build     # maturin develop --manifest-path foundation/ffi-bridge/rust/coinext-py/Cargo.toml --features python

uv run coinext download --symbols BTCUSDT,ETHUSDT --interval 1m --days 30
uv run coinext catalog
uv run coinext backtest --from-lake --symbol BTCUSDT
uv run coinext optimize --from-lake --mode anchored
```

`coinext backtest` 使用 authoritative event-driven runner；`coinext screen` 是快速向量化扫描，只能收窄参数空间，不能替代 event-driven parity surface。数据湖运行态根仍是仓库根 `data/` 或容器内 `/data`，不是源码模块。

Research loop 脚本位于 `strategy-research/research-notebooks/notebooks`。直接运行：

```bash
uv run python strategy-research/research-notebooks/notebooks/research_loop.py
```

默认使用合成数据；把脚本内 `USE_LAKE = True` 可切到真实本地 lake。

## 仓库布局

```text
foundation/                 全平台基础契约：primitives、domain-model、ports、state-cache、config、FFI、testkit
market-data/                数据接入、标准化、历史数据：data-engine、data-lake、transport、adapters、ingestion
strategy-research/          策略开发与研究循环：strategy API、indicators、research notebooks
backtesting-simulation/     回测、模拟场所、一致性门禁：kernel、sim、runner、parity、examples
analytics-optimization/     绩效分析、筛选、优化、衍生品定价
risk-portfolio/             风控、组合、账户级保护：risk engine、portfolio engine、risk monitor
execution-live/             订单执行、实盘/沙箱运行：execution engine、live runtime、trader、exec service
operations-interface/       总线、CLI、API、UI、部署、持久化

data/                       本地/compose 数据湖运行态根；保持在仓库根
config files                运行配置源码在 foundation/runtime-config/config
Cargo.toml / pyproject.toml 根 workspace 与 Python discovery 入口
docker-compose*.yml         根目录 compose 入口；Dockerfiles 在 operations-interface/deployment/docker
tests/                      根测试树，按生命周期模块分组
docs/                       路线图、testnet 手册、架构 stub
```

## 文档地图

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — 权威设计：领域模型、六边形端口、Kernel、数据流、部署形态。
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — 已完成/下一步/延后/开放问题。
- [`docs/TESTNET.md`](docs/TESTNET.md) — Binance market data + spot testnet paper execution runbook。
- [`tests/backtesting-simulation/parity/README.md`](tests/backtesting-simulation/parity/README.md) — advisory cross-check 与 sandbox gate 说明。
- [`operations-interface/deployment/README.md`](operations-interface/deployment/README.md) — compose、Docker、observability。
- [`operations-interface/deployment/services.md`](operations-interface/deployment/services.md) — deployable service index。

## 工具链

Rust 1.95（stable）、Python 3.13（uv）、Node 22（dashboard）、Docker。常用检查：

```bash
just test
just test-live-edge
just py-test
just py-lint
just compose-check
```

## 许可证

MIT。
