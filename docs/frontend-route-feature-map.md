# Frontend Route Feature Map

本文档定义前端页面、功能边界、主要 API、状态所有权和重构时的拆分目标。

## 1. 路由总表

| 路径 | 页面组件 | 权限 | 页面职责 |
|---|---|---|---|
| `/` | `ScannerPage` | Auth + can_analyze | 自然语言分析器、历史、候选池、分析结果 |
| `/guide` | `UsageGuidePage` | Auth | 使用说明 |
| `/rule-presets` | `RulePresetGuidePage` | Auth | 分析预设说明和应用 |
| `/watchlists` | `WatchlistPage` | Auth + can_analyze | 股票池、循环扫描、机会雷达、Wait Trigger |
| `/opportunities/{id}` | `OpportunityDetailPage` | Auth + can_analyze | 机会详情、风险计划、触发器、通知事件 |
| `/notifications` | `NotificationCenterPage` | Auth + can_analyze | 通知渠道、通知事件、投递日志 |
| `/notifications/guide` | `NotificationGuidePage` | Auth | 通知渠道绑定教程 |
| `/trading/instances/{id}` | `TradingPage` detail mode | Auth + can_trade | 单个交易实例详情 |
| `/admin` | `AdminPage` | Admin | 用户、抽签、服务健康 |
| `/admin/ai-costs` | `AdminAiCostPage` | Admin | AI 用量和成本 |
| `/ai-usage` | `AdminAiCostPage` | Admin | `/admin/ai-costs` 兼容入口 |
| `/beta-lottery` | `BetaLotteryPage` | Public | 公测抽签 |

当前 `/` 内通过 `page === 'trading'` 切换到 `TradingPage`。重构时建议把实盘交易主页面明确路由化为 `/trading`，保留当前入口做兼容跳转。

## 2. 页面职责

### 分析器 `/`

核心职责：

- 选择 AI Provider、行情源、策略模式和分析模块。
- 提交异步扫描。
- 轮询当前扫描。
- 查看历史扫描、星标、笔记、标签。
- 展示图表、候选池、策略结构、决策痕迹、原始 payload。
- 从扫描结果创建 Wait Trigger。

主要 API：

- `GET /api/auth/me`
- `GET /api/providers`
- `GET /api/analysis-presets`
- `POST /api/scans`
- `GET /api/scans`
- `GET /api/scans/{scan_id}`
- `PATCH /api/scans/{scan_id}/mark`
- `GET /api/scan-triggers`
- `POST /api/scan-triggers`
- `PATCH /api/scan-triggers/{trigger_id}`
- `DELETE /api/scan-triggers/{trigger_id}`
- `POST /api/scan-triggers/{trigger_id}/test`

状态所有权：

- `useScannerController` 拥有扫描表单、扫描历史、当前结果和 trigger 操作。
- 顶层 `App` 只保留 session、route、providers、Longbridge accounts。

重构目标：

- 拆出 `ScannerWorkspace`、`ScanRequestPanel`、`ScanResultWorkspace`、`ScanHistoryPanel`、`TriggerCreateMenu`。
- 把策略和行情标签全部迁移到语义字典。

### 机会雷达 `/watchlists`

核心职责：

- 管理股票池。
- 创建和编辑循环扫描实例。
- 运行规则测试、手动运行扫描、查看运行历史。
- 查看机会实例列表。
- 查看通知事件摘要。
- 管理 Wait Trigger。

主要 API：

- `GET/POST/PATCH/DELETE /api/watchlists`
- `GET/POST/PATCH/DELETE /api/scan-loop-instances`
- `POST /api/scan-loop-instances/{id}/run-now`
- `POST /api/scan-loop-instances/{id}/test-rules`
- `POST /api/scan-loop-instances/{id}/notification-preview`
- `GET /api/scan-loop-instances/{id}/runs`
- `GET /api/scan-loop-runs/{run_id}`
- `GET /api/opportunities`
- `POST /api/opportunity-followups/process`
- `GET /api/observation-health`
- `POST /api/observation-health/run-due-cycle`

状态所有权：

- `use-watchlist-radar.js` 负责雷达页状态、模板、实例和机会刷新。
- 不应从分析器 hook 里读取雷达专属状态。

重构目标：

- 拆出 `WatchlistManager`、`ScanLoopInstanceEditor`、`RadarRunTimeline`、`OpportunityBoard`、`RadarOpsPanel`。
- SPY 通用模板、信用价差模板等模板配置从 hook 中抽到 `domain/radar-presets.js` 或 `config/radar-presets.js`。

### 机会详情 `/opportunities/{id}`

核心职责：

- 展示机会 thesis、策略结构、风险计划、后续提醒。
- 展示 linked triggers、notification events、source run。
- 手动检查机会、暂停、恢复、归档。

主要 API：

- `GET /api/opportunities/{id}`
- `PATCH /api/opportunities/{id}`
- `POST /api/opportunities/{id}/pause`
- `POST /api/opportunities/{id}/resume`
- `POST /api/opportunities/{id}/archive`
- `POST /api/opportunities/{id}/check`
- `GET /api/opportunities/{id}/events`
- `POST /api/scan-triggers`

重构目标：

- 机会详情页和机会弹窗共用同一个 `OpportunityDetailView`。
- `risk_plan` 展示使用统一 `RiskPlanPanel`。

### 通知中心 `/notifications`

核心职责：

- 增删改通知渠道。
- 测试发送。
- 查看 payload preview。
- 查看渠道和事件投递日志。
- 手动发送或重试通知事件。

主要 API：

- `GET/POST/PATCH/DELETE /api/notification-channels`
- `POST /api/notification-channels/{id}/test`
- `GET /api/notification-channels/{id}/payload-preview`
- `GET /api/notification-channels/{id}/delivery-logs`
- `GET /api/notification-events`
- `POST /api/notification-events/{event_id}/send`
- `GET /api/notification-events/{event_id}/delivery-logs`
- `POST /api/notification-events/process`

重构目标：

- `NotificationChannelCard` 只展示和编辑渠道。
- `NotificationEventTable` 只展示事件。
- `DeliveryLogDrawer` 统一展示 channel/event 日志。
- `PayloadPreviewPanel` 必须隐藏敏感字段。

### 实盘交易 `/trading` 和 `/trading/instances/{id}`

核心职责：

- 配置实盘交易参数、券商、行情源、资金、策略模式、调度和保护。
- 就绪检查。
- 创建交易实例。
- 展示交易实例列表和详情。
- 执行手动监控、全平、实例撤单、实例平仓、重置风控、删除。
- 展示资金快照、成交快照和 AI 决策质量。

主要 API：

- `GET/PUT /api/trading/config`
- `GET /api/trading/readiness`
- `POST /api/trading/run-now`
- `GET /api/trading/runs`
- `GET /api/trading/runs/{run_id}`
- `POST /api/trading/monitor`
- `POST /api/trading/flatten`
- `POST /api/trading/runs/{run_id}/cancel-orders`
- `POST /api/trading/runs/{run_id}/flatten`
- `POST /api/trading/runs/{run_id}/reset-risk`
- `POST /api/trading/runs/{run_id}/delete`
- `POST /api/trading/runs/bulk-delete`
- `GET /api/trading/snapshots`
- `GET /api/trading/ai-quality`
- `GET /api/brokers/accounts`

状态所有权：

- `useTradingController` 拥有 trading config、readiness、snapshots、runs、activeRun 和危险操作。
- 页面组件只负责布局和交互，不直接拼请求路径。

重构目标：

- `TradingWorkspace` 承接主布局。
- `TradingConfigPanel` 只编辑配置。
- `TradingRunList` 只展示实例列表。
- `TradingRunDetail` 统一主页面详情和独立详情页。
- `RiskActionsPanel` 只放真实交易动作，必须要求确认。

### 管理后台

核心职责：

- 用户管理。
- 公测抽签后台。
- 服务健康。
- AI 用量和成本。

主要 API：

- `GET/POST/PATCH/DELETE /api/auth/users`
- `GET /api/beta-lottery/admin`
- `POST /api/beta-lottery/finalize`
- `GET /api/admin/server-health`
- `GET /api/admin/ai-usage`
- `GET /api/ai-usage/me`

重构目标：

- `AdminPage` 拆成 `UserAdminPanel`、`BetaLotteryAdminPanel`、`ServerHealthPanel`。
- AI 用量页继续独立，避免和用户管理耦合。

## 3. 顶层状态边界

`App` 可以持有：

- session。
- route path。
- route mode。
- providers。
- Longbridge accounts。
- market clock。
- global error 入口。

`App` 不应该持有：

- 雷达实例内部表单。
- 交易实例详情 tab。
- 通知渠道编辑状态。
- 扫描结果展示 tab。

这些状态应下沉到对应 route controller 或 page-level hook。

## 4. 建议重构目录

```text
web/src/
├── domain/
│   ├── glossary.js
│   ├── status.js
│   ├── strategy.js
│   ├── money.js
│   └── radar-presets.js
├── api/
│   ├── client.js
│   ├── scanner.js
│   ├── radar.js
│   ├── notifications.js
│   ├── trading.js
│   └── admin.js
├── routes/
│   ├── scanner/
│   ├── radar/
│   ├── notifications/
│   ├── trading/
│   └── admin/
├── components/
│   ├── ui/
│   ├── domain/
│   └── layout/
└── hooks/
```

## 5. 路由迁移建议

第一阶段保持现有路由：

- `/` 分析器。
- 内部按钮进入实盘页面。
- `/trading/instances/{id}` 交易实例详情。

第二阶段增加：

- `/trading` 实盘交易主页面。

第三阶段：

- `/` 不再承载实盘切换状态，只负责分析器。
- 顶层 navigation 用路径决定页面，不再用 `page` state。
