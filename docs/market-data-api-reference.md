# 行情与数据 API 文档

本文档汇总当前项目中和行情、新闻、期权链、扫描数据源相关的接口与内部适配器。

## 数据源

| 数据源 | 标识 | 正股行情 | 日线/分时 | 新闻 | 期权链 | 期权报价 | OI | IV | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| yfinance | `yfinance` | 支持 | 支持 | 支持，取决于 Yahoo 返回 | 支持 | 支持 | 支持 | 支持 | 免费备用源，稳定性和延迟取决于 Yahoo。 |
| Longbridge | `longbridge` | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | 通过本地分析补充 | 需要 Longbridge SDK 账号；也用于交易执行账号。 |
| ThetaData | `thetadata` | 支持 | 支持 | 不支持 | 支持 | 支持 | 支持 | 本地 Black-Scholes 估算 | 股票 Standard：Nasdaq Basic 实时快照、分钟/日线和历史；期权 Standard：OI/IV/期权链。 |
| 自动 | `auto` | 自动选择 | 自动选择 | 自动选择 | 自动选择 | 自动选择 | 自动选择 | 自动选择 | 兼容入口，当前按 ThetaData 优先；显式选择 yfinance/Longbridge 时才切换。 |

## ThetaData 能力边界

当前账号探测结果：

- 可用：股票 snapshot quote、股票 OHLC snapshot、股票 EOD 历史、股票 1m OHLC/quote 历史。
- 可用：期权 expiration 列表、期权 snapshot quote、open interest、OHLC、implied volatility。
- 可用：指数 snapshot price、指数 EOD 历史。
- 可用（订阅确认）：股票 **STANDARD** 支持 Nasdaq Basic 实时 quote/trade snapshot、1 分钟历史（2016 起）、日线、splits、trade/trade-quote 历史和最多 1,000 个正股 quote/trade 实时流；CTA/UTP 的实时全市场 NBBO 不在该档位。
- 可用（订阅确认）：期权 **STANDARD** 包含历史 EOD / OI / IV / 一阶 Greeks，回溯至 2016。
- 不可用：`option_snapshot_greeks_all`，ThetaData 返回 STANDARD 订阅不包含该 Professional 端点。
- 选择 ThetaData 时，扫描、交易实例、雷达触发和复盘的正股 quote、日线、分钟线均来自 ThetaData；ThetaData 不可用时扫描/雷达降级 yfinance，交易监控继续尝试 yfinance、Longbridge。
- 选择 ThetaData 时，期权价格、OI 和链路仍来自 ThetaData；完整 Greeks 继续由本地 Black-Scholes 估算。
- 不提供本项目使用的新闻端点；选择 ThetaData 时 `news` 返回空列表。

### 已知约束与适配（`thetadata_option_tool.py`）

- **单账户单 session**：ThetaData v3 云端每账户仅允许一个活跃 session，并发**登录**会互相顶号（grpc `UNAUTHENTICATED` "Invalid session ID"）。客户端改为**加锁单例**（`_client_lock` + `_client_singleton` + `_build_client`），失效重连经 `_reset_client(stale)` 串行化并将并发恢复折叠为单次重登（`_with_session_retry` 默认重试 3 次）。并发 gRPC**请求**不受影响。多标的并发扫描（交易代理 `_live_scan_workers`，默认 3 线程）因此不再雪崩。
- **历史整链 `strike='*'` 受限**：多合约历史请求（`option_history_*` + `strike='*'`）在本账号会被云端 MDDS 拒绝（`StatusCode.INTERNAL net.thetadata.exceptions.ProcessingError`），但**单合约**历史请求正常。`option_chain_rows(as_of_date=...)` 因此先尝试整链批量，失败再回退到**逐合约并发拉取**（`_history_chain_per_strike`，从可用的 live snapshot 取 ATM 附近行权价窗口，`AI_OPTION_THETADATA_HISTORY_WORKERS` 默认 8 并发）。
- **周末/休市 snapshot 不稳定**：收盘时 `option_snapshot_*` 间歇返回 `NoDataFoundError`。`collect_candidates`（扫描器 / 机会雷达）经 `_scan_expiration_rows` 在 snapshot 为空时回退到上一交易日 EOD 链（`_eod_as_of_date()`），保证周末扫描仍得到真实 OI/IV/Greeks。回退受限于 `AI_OPTION_THETADATA_SCAN_EOD_FALLBACK`（默认开）、`SCAN_EOD_MAX_EXPIRATIONS`（4）、`SCAN_EOD_STRIKE_RANGE`（24）。
- **参数注意**：`strike` 必须是字符串（传 int 触发客户端 TypeError）；`option_history_eod` 的 `start_date`/`end_date` 是前两个位置参数。

运行时凭证通过环境变量配置：

```bash
THETADATA_EMAIL=...
THETADATA_PASSWORD=...
```

也支持：

```bash
AI_OPTION_THETADATA_EMAIL=...
AI_OPTION_THETADATA_PASSWORD=...
THETADATA_CREDENTIALS_FILE=...
AI_OPTION_THETADATA_CREDENTIALS_FILE=...
```

## 对外 API

### `GET /api/market-clock`

返回美股市场时钟。

响应示例：

```json
{
  "timezone": "America/New_York",
  "now_et": "2026-05-22T10:30:00-04:00",
  "date_et": "2026-05-22",
  "is_regular": true,
  "session": "regular"
}
```

### `GET /api/market-environment`

返回当前市场环境摘要，用于交易准备度、风控和扫描上下文。

### `POST /api/scan`

同步执行一次扫描。请求体中的 `market_data_source` 可传：

```json
{
  "query": "扫描SPY最近日线和分时，找一个单腿期权",
  "symbol": "SPY",
  "market_data_source": "thetadata",
  "longbridge_account": null,
  "use_ai": true,
  "council": false
}
```

返回结果中的关键字段：

- `market_data_source`：实际使用的数据源。
- `longbridge_account`：Longbridge 模式为账号名；ThetaData 模式为 `thetadata`。
- `payload.quote`：现价、bid/ask、原始行情。
- `payload.daily`：日线摘要。
- `payload.intraday`：分时摘要。
- `payload.news`：新闻列表；ThetaData 为空。
- `payload.option_candidates`：期权候选池。
- `payload.tool_plan`：本次启用的工具链，例如 `thetadata_market_data`、`thetadata_option_chain`。

### `POST /api/scans`

异步创建扫描任务，字段同 `POST /api/scan`。适合前端扫描页使用。

### `GET /api/scans`

分页查询扫描历史。

常用查询参数：

- `limit`
- `offset`
- `starred`
- `query`
- `tag`

### `GET /api/scans/{scan_id}`

查询单次扫描详情，包含扫描状态、阶段、进度和结果。

### `GET /api/scan-triggers`

查询观察触发器。触发器可保存 `market_data_source`，当前支持 `yfinance`、`longbridge`、`thetadata`。

### `POST /api/scan-triggers`

创建观察触发器。技术指标类触发器会调用对应数据源的正股行情；期权报价类触发器会调用对应数据源的期权报价。

期权报价触发器示例：

```json
{
  "symbol": "SPY",
  "condition": {
    "type": "option_quote",
    "symbol": "SPY",
    "contract_symbol": "SPY260626C00550000",
    "field": "ask",
    "operator": "<=",
    "value": 2.5,
    "market_data_source": "thetadata"
  }
}
```

### `POST /api/scan-triggers/{trigger_id}/test`

立即测试触发器条件。返回当前快照、当前值、命中状态和数据质量。

### `GET /api/opportunities`

查询机会雷达列表。机会跟踪会读取机会创建时保存的数据源，并在 follow-up 中刷新正股行情、GEX 与期权报价。

### `POST /api/opportunities/{opportunity_id}/check`

立即刷新单个机会的后续跟踪。ThetaData 机会会使用 ThetaData 刷新正股行情、期权报价和 GEX 相关快照，并复用 yfinance IV 输入估算 Greeks/GEX。

### `GET /api/trading/config`

读取交易配置。配置中包含 `market_data_source`，当前可选 `thetadata`、`yfinance`、`longbridge`、`auto`，默认与 `auto` 均按 ThetaData 优先。

### `POST /api/trading/run-now`

按交易配置立即运行扫描/选标流程。`market_data_source` 决定扫描阶段的数据源；真实交易执行仍依赖 Longbridge 交易账号。

## 内部适配器

### yfinance

文件：`ai_option_scanner/yfinance_option_tool.py`

主要函数：

- `market_data(symbol, daily_count=80)`
- `collect_candidates(...)`
- `quote_option_contract(contract_symbol)`

### Longbridge

文件：`ai_option_scanner/longbridge_option_tool.py`

主要函数：

- `market_data(symbol, daily_count=80, account_name=None)`
- `collect_candidates(..., account_name=None)`
- `quote_option_contract(contract_symbol, account_name=None)`

### ThetaData

文件：`ai_option_scanner/thetadata_option_tool.py`

主要函数：

- `market_data(symbol, daily_count=80)`
- `collect_candidates(...)`
- `option_expirations(symbol)`
- `quote_option_contract(contract_symbol)`
- `account_capabilities()`

ThetaData 适配器只读取数据，不保存账号密码。生产部署请通过环境变量或 ThetaData credentials file 注入凭证。
