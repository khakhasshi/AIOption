# Frontend API View Contract

本文档定义后端 API 数据如何进入前端视图。后端 API 文档说明“接口返回什么”，本文档说明“页面应该如何消费、刷新、展示和降级”。

## 1. 通用规则

- 所有业务请求使用 `credentials: same-origin`，携带 Cookie session。
- 所有业务请求携带 `X-AI-Option-User`，但启用认证后后端以 session principal 覆盖 owner。
- `401`：前端必须触发登录态过期处理。
- `403`：显示权限不足，不要自动重试。
- `404`：资源不存在，详情页应提供返回入口。
- `500/503`：显示可读错误，保留重试按钮。
- 所有金额、百分比、时间、状态、策略名通过统一 formatter 展示。

## 2. Session Contract

API：

- `GET /api/auth/me`
- `POST /api/auth/login`
- `POST /api/auth/logout`

关键字段：

| 字段 | 视图用途 |
|---|---|
| `authenticated` | 是否显示业务页面 |
| `username` | 顶部登录态 |
| `can_analyze` | 分析器、机会雷达、通知中心 |
| `can_trade` | 实盘交易、Longbridge、交易实例 |
| `is_admin` | 管理后台、AI 用量 |
| `limits` | 用户配额提示 |

视图规则：

- 未登录时只显示登录页和公测抽签入口。
- `can_trade=false` 时隐藏或禁用实盘入口；直接访问交易详情显示权限页。
- `is_admin=false` 访问 `/admin` 显示管理员权限页。

## 3. Scanner Contract

API：

- `POST /api/scans`
- `GET /api/scans`
- `GET /api/scans/{scan_id}`
- `PATCH /api/scans/{scan_id}/mark`

列表页字段：

| 字段 | 用途 |
|---|---|
| `id` / `locator_id` | 展示复制 ID，打开详情 |
| `status` / `stage` | 状态 badge |
| `query` | 历史标题 |
| `symbol` | 标的 |
| `ai_provider` | 模型 badge |
| `market_data_source` | 行情源 badge |
| `strategy_modes` | 策略模式 |
| `created_at` | 创建时间，美东显示 |
| `starred` / `note` / `tags` | 星标、笔记、标签 |

详情字段：

| 字段 | 用途 |
|---|---|
| `result.answer` | AI/规则报告正文 |
| `result.payload` | 候选池、图表、结构、风险数据 |
| `result.charts` | 图表 |
| `payload.candidates` | 单腿候选池 |
| `payload.strategy_candidates` | 多腿策略候选 |
| `payload.decision_gate` | 门禁显示 |
| `payload.gex_context` | GEX/结构显示 |
| `payload.tool_plan` | 工具链说明 |

刷新规则：

- 新建扫描后立刻轮询 `GET /api/scans/{id}`。
- `queued/running` 每 `SCAN_POLL_INTERVAL_MS` 刷新。
- `succeeded` 后停止轮询并刷新历史。
- `failed` 后停止轮询并显示错误。

降级规则：

- `result.answer` 缺失时，展示结构化 payload 摘要。
- `charts` 缺失时，不显示空图框。
- `payload.candidates` 为空时显示“无可用候选”，不要报错。

## 4. Radar Contract

API：

- `GET/POST/PATCH/DELETE /api/watchlists`
- `GET/POST/PATCH/DELETE /api/scan-loop-instances`
- `POST /api/scan-loop-instances/{id}/run-now`
- `POST /api/scan-loop-instances/{id}/test-rules`
- `GET /api/scan-loop-runs/{run_id}`
- `GET /api/observation-health`

循环实例字段：

| 字段 | 用途 |
|---|---|
| `id` | 操作主键 |
| `name` / `description` | 卡片标题 |
| `status` | active/paused |
| `symbols` / `watchlist_id` | 标的来源 |
| `schedule.interval_minutes` | 扫描频率 |
| `market_data_source` | 行情源 |
| `prefilter_rules` | 预筛规则摘要 |
| `alert_rules` | 提醒规则摘要 |
| `notification_channel_ids` | 通知绑定 |
| `ai_scan_policy` / `ai_scan_top_n` | AI 精扫预算 |

运行详情字段：

| 字段 | 用途 |
|---|---|
| `summary` | 扫描数量、预筛、触发、AI 预算 |
| `items[]` | 单标的扫描明细 |
| `items[].snapshot` | last、rvol、vwap、gex 等快照 |
| `items[].recommendation` | 观察、结论、决策、风控、AI 剧本 |
| `items[].notification_events` | 本轮通知 |
| `items[].data_quality` | 缺数据原因 |

AI 剧本显示规则：

- 如果 `recommendation.scenario_baseline` 存在，显示情景基准块。
- 如果是缓存复用，显示“沿用上一轮剧本”或同义 badge。
- 如果 AI 未生成，不要伪造交易台报告，只显示规则结论。
- HIRO、dealer inventory、真实订单流未接入时显示“未接入”，不要隐藏为 0。

## 5. Opportunity Contract

API：

- `GET /api/opportunities`
- `GET /api/opportunities/{id}`
- `PATCH /api/opportunities/{id}`
- `POST /api/opportunities/{id}/pause`
- `POST /api/opportunities/{id}/resume`
- `POST /api/opportunities/{id}/archive`
- `POST /api/opportunities/{id}/check`
- `GET /api/opportunities/{id}/events`

列表字段：

| 字段 | 用途 |
|---|---|
| `id` | 打开详情 |
| `status` | 观察/追踪/归档 |
| `symbol` | 标的 |
| `title` | 标题 |
| `thesis` | 机会假设 |
| `strategy_family` | 策略族 |
| `risk_plan` | 风险摘要 |
| `created_at` / `updated_at` | 时间 |

详情字段：

| 字段 | 用途 |
|---|---|
| `legs` / `strategy` | 多腿结构 |
| `payoff` | Payoff 图或表 |
| `gex_context` | 结构上下文 |
| `risk_plan` | 止盈、止损、失效条件 |
| `linked_triggers` | 绑定触发器 |
| `notification_events` | 相关通知 |
| `source_run` | 来源雷达运行 |
| `events` | 生命周期时间线 |

操作规则：

- pause/resume/archive 后必须刷新详情和列表。
- check 后显示检查结果，并追加时间线。
- risk_plan 编辑不能覆盖未编辑字段。

## 6. Notification Contract

API：

- `GET/POST/PATCH/DELETE /api/notification-channels`
- `POST /api/notification-channels/{id}/test`
- `GET /api/notification-channels/{id}/payload-preview`
- `GET /api/notification-channels/{id}/delivery-logs`
- `GET /api/notification-events`
- `POST /api/notification-events/{event_id}/send`
- `GET /api/notification-events/{event_id}/delivery-logs`
- `POST /api/notification-events/process`

渠道字段：

| 字段 | 用途 |
|---|---|
| `id` | 操作 |
| `type` | email/webhook |
| `label` | 标题 |
| `config.provider` | telegram/discord/slack/feishu/whatsapp/generic |
| `enabled` | 开关 |
| `verified_at` | 最近验证 |
| `last_error` | 最近错误 |
| `last_test_at` | 最近测试 |

敏感字段规则：

- 前端永远不显示完整 `secret`、`bot_token`、`access_token`。
- payload preview 中 Authorization 必须是 masked。
- PATCH 时敏感字段留空表示保持原值。

事件字段：

| 字段 | 用途 |
|---|---|
| `status` | pending/sent/failed |
| `title` / `body` | 通知内容 |
| `source_type` / `source_id` | 来源跳转 |
| `attempts` | 重试次数 |
| `last_error` | 错误说明 |
| `payload` | symbol、opportunity_id、run_id 等上下文 |

## 7. Trading Contract

API：

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

配置视图：

| 字段 | 用途 |
|---|---|
| `broker` | Longbridge/Alpaca |
| `broker_account` | Alpaca 账号 |
| `longbridge_account` | Longbridge 账号 |
| `market_data_source` | 行情源 |
| `strategy_modes` | 策略族 |
| `strategy_auto_execute_enabled` | 多腿自动执行 |
| `software_stop_enabled` | 软件止损 |
| `software_take_profit_enabled` | 软件止盈 |
| `schedule_slots` | 多时段 |

Readiness 规则：

- `readiness.ok=false` 禁止手动创建交易实例。
- `issues` 显示为阻断。
- `warnings` 显示为警告，但不一定阻断。
- `next_run_slot` 用于多时段提示。

交易实例列表：

| 字段 | 用途 |
|---|---|
| `locator_id` | 用户可见 ID |
| `status` | 顶层状态 |
| `stage` | 阶段 |
| `lifecycle_state` | 生命周期 |
| `protection_state` | 保护状态 |
| `created_at` | 时间 |
| `config.broker` | 券商 |
| `trade_instance.summary` | 摘要 |

交易实例详情：

| 字段 | 用途 |
|---|---|
| `scan_results` | 股票池扫描结果 |
| `selections` | AI/系统最终选择 |
| `orders` | 订单/策略执行记录 |
| `trade_instance.risk_plan` | 风控计划 |
| `trade_instance.execution_plan` | 执行计划 |
| `trade_instance.protection_status` | 保护状态 |
| `trade_instance.event_timeline` | 时间线 |
| `trade_instance.review_metrics` | 复盘 |

PnL 规则：

- 账户级 PnL 使用 `/api/trading/snapshots`。
- 实例级 PnL 使用 `trade_instance` 中的 strategy ledger / review metrics。
- 未平仓浮盈亏必须标记为 mark 估算。
- 退出单提交但未成交时，不能显示为已实现 PnL。

危险操作：

| 操作 | 确认文本 | 真实交易动作 |
|---|---|---|
| 全账户全平 | `全平` | 是 |
| 撤实例订单 | `撤实例` | 是 |
| 平当前实例 | `平实例` | 是 |
| 重置风控 | `初始化风控` | 否 |
| 删除实例 | `删除实例` | 否 |
| 批量删除实例 | `批量删除实例` | 否 |

## 8. Admin Contract

用户管理字段：

| 字段 | 用途 |
|---|---|
| `username` | 用户名 |
| `can_analyze` | 分析权限 |
| `can_trade` | 实盘权限 |
| `is_admin` | 管理权限 |
| `remaining_days` | 剩余天数 |
| `limits` | 配额 |
| `usage` | 当前用量 |
| `editable` | 是否可修改 |

规则：

- `editable=false` 的 env 用户不能删除或修改。
- 删除最后一个管理员应由后端阻止，前端显示错误。

## 9. Loading / Empty / Error

所有页面至少提供三种状态：

- Loading：首次请求未完成。
- Empty：请求成功但列表为空。
- Error：请求失败并可重试。

禁止：

- 用空白页面代表 loading。
- 把权限错误显示成“暂无数据”。
- 在危险交易动作失败后只 toast，不刷新实例。
