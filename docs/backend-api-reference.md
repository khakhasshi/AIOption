# Backend API Reference

> 自动从 `ai_option_scanner/web_api.py` 及路由模块同步生成。所有端点基于 FastAPI，端口 7001。

## 通用约定

### 认证
- 登录后通过 `HttpOnly` Cookie 维持会话。
- 每个请求携带 `X-AI-Option-User` header 标识 owner；启用登录认证后以后端 session principal 覆盖。
- 未登录返回 `401`，权限不足返回 `403`。

### 权限模型

| 权限 | 含义 |
|---|---|
| `can_analyze` | 可使用分析器、机会雷达、通知中心 |
| `can_trade` | 可使用实盘交易、券商账号、Longbridge 行情 |
| `is_admin` | 可访问管理后台、AI 用量、用户管理 |

### 响应约定
- 成功：`200` 或 `201`，JSON body。
- 错误：标准 `HTTPException`，`detail` 字段包含错误信息。
- 所有金额、百分比、时间为原始值，前端通过 formatter 展示。

### 通用请求头

| Header | 值 | 说明 |
|---|---|---|
| `Content-Type` | `application/json` | 所有 POST/PATCH/PUT 请求 |
| `X-AI-Option-User` | username | 指定操作 owner（未登录时） |
| `Cookie` | `ai_option_session=...` | 登录会话 |

---

## 1. Health & Admin

### `GET /api/health`
公开。浅层依赖探活：ping 数据库与 Redis，返回 `status`（`ok` / `degraded`）、各依赖的 `ok` 与延迟，以及进程 `role`。始终返回 HTTP 200（单个依赖降级不致使负载均衡器摘除节点）；详细指标见 admin 的 `/api/admin/server-health`。

响应：
```json
{
  "status": "ok",
  "database": { "ok": true, "backend": "postgres", "latency_ms": 0.81 },
  "redis": { "ok": true, "latency_ms": 0.27 },
  "role": "web"
}
```

### `GET /api/admin/server-health`
Admin。返回系统详细指标。

响应：
```json
{
  "db_pool": { "checked_out": 0, "total": 10 },
  "redis": { "connected": true },
  "trading_scheduler": { "enabled": true, "next_fire": "2026-06-03T14:30:00Z" },
  "order_monitor": { "running": true, "last_check": "2026-06-03T10:05:00Z" },
  "observation_health": { "active_instances": 5 },
  "scan_queue_size": 2
}
```

---

## 2. Authentication

### `GET /api/auth/me`
返回当前登录用户信息和权限。

响应：
```json
{
  "authenticated": true,
  "username": "trader1",
  "can_analyze": true,
  "can_trade": true,
  "is_admin": false,
  "limits": {
    "max_daily_scans": 50,
    "max_daily_ai_scans": 20,
    "max_watchlists": 10,
    "max_scan_loop_instances": 5,
    "max_notification_channels": 10,
    "max_longbridge_accounts": 3
  },
  "expires_at": "2027-01-01T00:00:00Z"
}
```

未登录：
```json
{ "authenticated": false }
```

### `POST /api/auth/login`
登录。Body：
```json
{
  "username": "trader1",
  "password": "your-password"
}
```
成功后设置 `HttpOnly` session cookie。

### `POST /api/auth/logout`
清除 session cookie。

### `GET /api/auth/oauth/config`
公开。返回已配置的 Google / Apple OAuth provider，用于决定登录页是否显示对应按钮；不返回 client secret。

### `GET /api/auth/turnstile/config`
公开。返回 Turnstile 是否启用及可公开的 site key；不返回 secret key。

### `POST /api/auth/oauth/login`
验证 Google / Apple identity token，按已验证邮箱映射或创建用户，并签发 HttpOnly session cookie。请求必须接受使用条款；启用 Turnstile 后还必须提供有效 token。

### `GET /api/auth/oauth/links`
返回当前用户已绑定的 OAuth 身份和是否存在可用密码。

### `POST /api/auth/oauth/links`
把一个已验证且未被其他用户占用的 OAuth 身份绑定到当前用户。

### `DELETE /api/auth/oauth/links/{provider}`
解除当前用户的 OAuth 身份。若该身份是没有密码账户的最后一种登录方式，请求会被拒绝以避免锁死账户。

### `GET /api/auth/users`
Admin。列出所有用户。

### `POST /api/auth/users`
Admin。创建用户。Body：
```json
{
  "username": "newuser",
  "password": "secret123",
  "can_analyze": true,
  "can_trade": false,
  "is_admin": false,
  "expires_at": "2027-01-01T00:00:00Z",
  "max_daily_scans": 30,
  "max_daily_ai_scans": 10
}
```

### `PATCH /api/auth/users/{username}`
Admin。更新用户权限和配额。Body 同创建但字段均为可选。

### `DELETE /api/auth/users/{username}`
Admin。删除用户。

---

## 3. Market & Trading Info

### `GET /api/market-clock`
公开。美股市场时钟。

响应：
```json
{
  "timezone": "America/New_York",
  "now_et": "2026-06-03T10:30:00-04:00",
  "date_et": "2026-06-03",
  "is_regular": true,
  "session": "regular",
  "next_open": null,
  "next_close": "2026-06-03T16:00:00-04:00"
}
```

session 可能值：`pre_market`、`regular`、`after_hours`、`closed`、`weekend`、`holiday`。

### `GET /api/market-environment`
公开。当前市场环境摘要，用于交易准备度和扫描上下文。

### `GET /api/trading/readiness`
需 `can_trade`。交易配置的就绪检查，返回 readiness status 和阻断项列表。风险熔断只统计真正暴露过交易风险的实例：有订单/成交、进入持仓或监控生命周期、存在未保护数量、或需要人工处理；纯扫描、AI 决策拒绝、`decision_gate_blocked`、`data_integrity_blocked` 且 `orders=0` 的记录不消耗当日交易次数，也不会计入连续失败熔断。

响应：
```json
{
  "ok": false,
  "issues": [
    { "field": "live_enabled", "message": "实盘交易未开启", "blocker": true }
  ],
  "warnings": [],
  "next_run_slot": "2026-06-03T14:35:00Z",
  "schedule_preview": { "interval_minutes": 15, "next": "..." },
  "risk": {
    "today_run_count": 1,
    "consecutive_failures": 0,
    "consecutive_failure_runs": [],
    "manual_attention_count": 0,
    "manual_attention_runs": [],
    "active_unprotected_quantity": 0,
    "active_unprotected_runs": []
  }
}
```

---

## 4. AI Providers

### `GET /api/providers`
列出全局 AI providers 和当前用户的个人 providers。

响应：
```json
{
  "providers": [
    { "name": "deepseek", "model": "deepseek-v4-flash", "api_base": "https://api.deepseek.com", "is_default": true }
  ],
  "user_providers": []
}
```

### `POST /api/providers`
Admin。添加全局 provider。Body：
```json
{
  "name": "openai-compat",
  "api_base": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "model": "gpt-4o",
  "is_default": false
}
```

### `DELETE /api/providers/{name}`
Admin。删除全局 provider。

### `POST /api/user-providers`
创建个人 AI provider（仅影响当前用户）。Body 同上 `ProviderRequest`。

### `DELETE /api/user-providers/{name}`
删除个人 provider。

### `GET /api/analysis-presets`
返回分析预设模板列表（策略模板、扫描预设）。

---

## 5. Longbridge Accounts

### `GET /api/longbridge/accounts`
列出当前用户的 Longbridge 账号。

响应：
```json
{
  "accounts": [
    {
      "name": "my-hk-account",
      "is_default": true,
      "has_credentials": true,
      "created_at": "2026-05-01T00:00:00Z"
    }
  ]
}
```

### `POST /api/longbridge/accounts`
创建账号。Body：
```json
{
  "name": "my-us-account",
  "account_type": "us"
}
```

### `PUT /api/longbridge/accounts/{name}/credentials`
设置账号的 SDK 凭证。Body：
```json
{
  "api_key": "lb-...",
  "api_secret": "...",
  "access_token": "..."
}
```

### `POST /api/longbridge/accounts/{name}/default`
设为默认账号。

### `DELETE /api/longbridge/accounts/{name}`
删除账号。

### `GET /api/longbridge/status`
检查 Longbridge 会话状态。Query：`account`（账号名），`force`（是否强制刷新）。

---

## 6. Broker Accounts

### `GET /api/brokers/accounts`
列出模块化券商账号（目前支持 Alpaca，预留 IBKR、Tradier 等）。

### `POST /api/brokers/accounts`
创建券商账号。Body：
```json
{
  "broker": "alpaca",
  "name": "my-alpaca-paper",
  "api_key": "PK...",
  "api_secret": "...",
  "is_paper": true
}
```

### `POST /api/brokers/accounts/{broker}/{name}/default`
设为默认券商账号。

### `DELETE /api/brokers/accounts/{broker}/{name}`
删除券商账号。

---

## 7. Scanning

### `POST /api/scans`
**异步**提交扫描任务到 Redis 队列，立即返回 `scan_id` 以便轮询。（注：当前仅提供异步入口；不存在同步 `POST /api/scan`。）

Body：
```json
{
  "query": "扫描SPY最近日线和分时，找一个单腿期权",
  "symbol": "SPY",
  "market_data_source": "thetadata",
  "longbridge_account": null,
  "use_ai": true,
  "council": false,
  "strategy_modes": ["single_leg"],
  "ai_provider": null,
  "analysis_modules": ["intraday_structure", "gex", "volume_profile"]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | str | 自然语言或简写 |
| `symbol` | str? | 强制指定标的，留空由 AI 从 query 提取 |
| `market_data_source` | str | `thetadata`（默认）、`yfinance`、`longbridge`、`auto` |
| `longbridge_account` | str? | Longbridge 行情用的账号名 |
| `use_ai` | bool | 是否启用 AI 分析 |
| `council` | bool | 是否启用三顾问内阁 |
| `strategy_modes` | list[str] | 策略族过滤 |
| `ai_provider` | str? | 指定 AI provider，留空用默认 |
| `analysis_modules` | list[str] | 启用的分析模块 |

### `GET /api/scans`
扫描历史。Query：`limit`（默认 20）、`offset`、`starred`（bool）、`query`（搜索）、`tag`。

### `GET /api/scans/{scan_id}`
单个扫描详情，包含完整 `result`（payload、candidates、decision_gate 等）。

### `GET /api/scans/{scan_id}/events`
扫描状态实时推送（SSE，`text/event-stream`）。需登录，且只能订阅本人的扫描（否则 404）。订阅后先收到一帧当前快照，之后随 worker 推进收到状态变更帧，空闲时发送 `: keep-alive` 心跳；扫描到达终态（`succeeded` / `failed`）后发送 `event: done` 并关闭。若扫描已是终态或 Redis 不可用，立即发 `done` 结束（前端据此回退到轮询）。最长连接 600s（与负载均衡器响应超时一致）。

数据帧（`data:`）：
```json
{ "scan_id": "…", "status": "running", "stage": "option_chain", "progress": 60 }
```
`status`：`running` / `succeeded` / `failed`；`failed` 帧附带 `error`。终态帧不含结果体——客户端收到后用 `GET /api/scans/{scan_id}` 拉取完整结果。事件经 Redis pub/sub 由 worker 进程发布、web 进程转发；裸跑无 Redis 时该端点不可用、前端自动回退轮询。

### `GET /api/scan-marks`
当前 owner 已星标 / 备注 / 打标签的扫描记录列表。每项包含 `scan_id`、`starred`、`note`、`tags`、`updated_at`，按更新时间倒序返回。用于扫描历史列表合并显示标记态。

### `PATCH /api/scans/{scan_id}/mark`
标记扫描（星标、笔记、标签）。Body：
```json
{
  "starred": true,
  "note": "值得跟踪的信用价差机会",
  "tags": ["earnings-play"]
}
```

---

## 8. Scan Triggers（等待触发器）

从扫描结果创建的条件触发器——当标的价格、技术指标或期权报价满足条件时触发。

### `GET /api/scan-triggers`
列出当前用户的所有触发器。

### `POST /api/scan-triggers`
创建触发器。Body：
```json
{
  "scan_id": "scan-xxx",
  "symbol": "SPY",
  "trigger_type": "price_above",
  "reference_value": 520.0,
  "cooldown_minutes": 15,
  "max_fire_count": 3,
  "expires_at": "2026-06-10T00:00:00Z",
  "enabled": true,
  "note": "突破阻力位后考虑入场"
}
```

### `PATCH /api/scan-triggers/{trigger_id}`
更新触发器，Body 同上但字段可选。

### `DELETE /api/scan-triggers/{trigger_id}`
删除触发器。

### `POST /api/scan-triggers/{trigger_id}/check`
手动检查触发器是否满足条件（可能发送通知、创建机会事件）。

### `POST /api/scan-triggers/{trigger_id}/test`
测试触发器——不发送通知、不增加触发次数、不创建机会。返回模拟结果。

---

## 9. Watchlists（股票池）

### `GET /api/watchlists`
列出股票池。

### `POST /api/watchlists`
创建股票池。Body：
```json
{
  "name": "Mag7",
  "symbols": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"],
  "note": "科技七巨头"
}
```

### `PATCH /api/watchlists/{watchlist_id}`
更新股票池。Body 同上但字段可选。

### `DELETE /api/watchlists/{watchlist_id}`
删除股票池。

---

## 10. Scan Loop Instances（循环扫描实例）

按规则和频率对股票池或单票进行持续扫描配置。

### `GET /api/scan-loop-instances`
列出所有循环扫描实例。

### `POST /api/scan-loop-instances`
创建循环实例。Body：
```json
{
  "name": "SPY 信用价差雷达",
  "symbols": ["SPY"],
  "strategy_family": "credit_spread",
  "market_data_source": "thetadata",
  "interval_minutes": 3,
  "prefilter_rules": {
    "min_rvol": 0.8,
    "max_spread_pct": 5.0,
    "min_price": 100.0
  },
  "alert_rules": {
    "iv_rank_above": 60,
    "min_decision_score": 0.7
  },
  "enabled": true,
  "ai_provider": "deepseek",
  "council": false,
  "notification_channel_ids": ["ch-xxx"],
  "max_runs_per_day": 100
}
```

### `PATCH /api/scan-loop-instances/{instance_id}`
更新循环实例。

### `DELETE /api/scan-loop-instances/{instance_id}`
删除循环实例。

### `POST /api/scan-loop-instances/{instance_id}/run-now`
手动触发一次立即运行。

### `POST /api/scan-loop-instances/{instance_id}/test-rules`
测试预过滤和告警规则——不创建真实机会和通知。

### `POST /api/scan-loop-instances/{instance_id}/notification-preview`
预览通知样例内容。

### `GET /api/scan-loop-instances/{instance_id}/runs`
查询循环实例的历史运行记录。Query：`limit`。

### `GET /api/scan-loop-runs/{run_id}`
单次循环运行的详情（含每个 symbol 的规则命中、AI/通知决策）。

---

## 11. Notification Channels（通知渠道）

### `GET /api/notification-channels`
列出通知渠道。

### `POST /api/notification-channels`
创建通知渠道。Body 按渠道类型不同：

**Email：**
```json
{
  "channel_type": "email",
  "label": "我的邮箱",
  "config": { "email": "trader@example.com" },
  "enabled": true
}
```

**Slack：**
```json
{
  "channel_type": "slack",
  "label": "交易通知频道",
  "config": { "webhook_url": "https://hooks.slack.com/..." },
  "enabled": true
}
```

**Telegram：**
```json
{
  "channel_type": "telegram",
  "label": "Telegram Bot",
  "config": { "bot_token": "...", "chat_id": "..." },
  "enabled": true
}
```

**Discord / 飞书 / WhatsApp：** 类似 webhook 配置。

### `PATCH /api/notification-channels/{channel_id}`
更新渠道。Body：部分字段可选。

### `DELETE /api/notification-channels/{channel_id}`
删除渠道。

### `POST /api/notification-channels/{channel_id}/test`
发送测试通知。

### `GET /api/notification-channels/{channel_id}/payload-preview`
预览该渠道的通知样例。

### `GET /api/notification-channels/{channel_id}/delivery-logs`
查询该渠道的投递日志。Query：`limit`。

### `GET /api/notification-delivery-logs`
跨渠道查询投递日志。Query：`channel_id`、`event_id`、`limit`。

---

## 12. Notification Events（通知事件）

### `GET /api/notification-events`
列出待发送队列中的事件。Query：`limit`。

### `POST /api/notification-events/{event_id}/send`
手动触发某事件的发送。

### `GET /api/notification-events/{event_id}/delivery-logs`
查询该事件的投递日志。

### `POST /api/notification-events/process`
手动触发处理所有待发事件。

---

## 13. Opportunities（机会实例）

从扫描、雷达或 Trigger 产生的可持续追踪交易参考机会。

### `GET /api/opportunities`
列出机会。Query：`limit`。

### `POST /api/opportunity-followups/process`
处理到期的 followup 检查。

### `GET /api/opportunities/{opportunity_id}`
机会详情，含关联 triggers 和通知事件。

响应包含：
- `thesis`：交易论点
- `risk_plan`：风险计划
- `linked_triggers`：关联的触发器列表
- `status`：`active` / `paused` / `archived`
- `timeline`：事件时间线

### `PATCH /api/opportunities/{opportunity_id}`
更新标题、论点或通知渠道。Body：
```json
{
  "title": "SPY 信用价差机会",
  "thesis": "IV Rank > 60%, 短期回调概率高",
  "notification_channel_ids": ["ch-xxx"]
}
```

### `POST /api/opportunities/{opportunity_id}/pause`
暂停 followup 检查。

### `POST /api/opportunities/{opportunity_id}/resume`
恢复 followup 检查。

### `POST /api/opportunities/{opportunity_id}/archive`
归档机会。

### `POST /api/opportunities/{opportunity_id}/check`
手动检查 followup 条件。

### `GET /api/opportunities/{opportunity_id}/events`
查询关联的通知事件。Query：`limit`。

---

## 14. Observation Health

### `GET /api/observation-health`
返回机会雷达调度器状态。

### `POST /api/observation-health/run-due-cycle`
手动触发一次到期的雷达周期扫描。

---

## 15. Trading Configuration

### `GET /api/trading/config`
需 `can_trade`。获取当前交易配置。

响应：
```json
{
  "live_enabled": true,
  "broker": "longbridge",
  "broker_account": "my-us-account",
  "market_data_source": "thetadata",
  "strategy_family": "credit_spread",
  "capital": 5000.0,
  "max_positions": 3,
  "max_daily_instances": 10,
  "default_stop_loss_pct": 0.5,
  "enable_software_stops": true,
  "enable_smart_exits": true,
  "schedule_interval_minutes": 15,
  "symbols": ["SPY", "QQQ", "IWM"],
  "dte_min": 7,
  "dte_max": 45,
  "ai_provider": "deepseek",
  "council": true,
  "max_ai_candidates": 5
}
```

### `PUT /api/trading/config`
保存交易配置。Body 同上。

---

## 16. Trading Execution & History

### `POST /api/trading/run-now`
立即触发一次实盘交易扫描。系统根据当前配置运行完整 pipeline：扫描 → AI 决策 → 下单 → 保护。

### `GET /api/trading/runs`
交易运行历史。Query：`limit`。

### `GET /api/trading/runs/{run_id}`
单个交易实例详情，包含：
- `status`、`stage`、`lifecycle_state`、`protection_state`
- `orders[]`：订单列表（状态、数量、价格）
- `fills[]`：成交记录
- `pnl_snapshots[]`：盈亏快照
- `events[]`：生命周期事件时间线
- `ai_trace`：AI 决策痕迹

Query：
- `light`（bool，默认 `false`）：轻量模式，省略入场期一次性 blob（`scan_results` / `council` / `selections`，合计约 50–150 KB），仅返回实盘期间会变的字段（`status`、`stage`、`progress`、`lifecycle_state`、`protection_state`、`orders`、`instance_json`、`error`、时间戳）。被省略的字段以 `null` 返回，响应附带 `_payload_mode: "light"` 标记。前端轮询时使用该模式可显著降低带宽 / CPU，缺失字段需与首次 full 响应缓存合并。

### `GET /api/trading/runs/{run_id}/review`
返回该交易实例的 AI 复盘结果（由 post-mortem worker 异步生成）。`404` 表示尚未入队或仍在处理。返回字段包含：
- `run_id`、`owner_id`、`locator_id`、`lifecycle_state`、`exit_reason`
- `realized_pnl`、`return_pct`、`holding_minutes`
- `facts`：复盘输入快照（决策摘要、订单/成交、关键指标）
- `review`：AI 输出（`verdict` / `score` / `summary` / `what_went_right` / `what_went_wrong` / `lessons` / `suggested_changes`）；`review_status="skipped"`、`"failed"` 或 `"pending"` 时可能为 `null`
- `review_status`：`pending` / `processing` / `done` / `skipped` / `failed`
- `review_error`、`attempts`、`ai_provider`、`ai_model`
- `created_at`、`updated_at`、`reviewed_at`

触发条件（worker 端，不在 API 控制范围）：lifecycle 进入 `closed` / `reviewed` 且 |`realized_pnl`| ≥ `AI_OPTION_POST_MORTEM_PNL_THRESHOLD` 或 `holding_minutes` ≥ `AI_OPTION_POST_MORTEM_HOLDING_MIN`，且至少有一次成交；不满足时写 `review_status="skipped"`。

### `GET /api/trading/reviews`
当前 owner 最近的复盘列表，按 `created_at` 倒序返回。Query：
- `limit`（默认 50，最大 200）
- `status`：逗号分隔的 `review_status` 过滤集合（如 `done,failed`）

返回数组中每项结构与 `/api/trading/runs/{run_id}/review` 相同。

### `GET /api/trading/schedule-fires`
查询即将/过去的调度触发时间。Query：`limit`。

### `GET /api/trading/ai-quality`
AI 决策质量指标。Query：`limit`。

### `POST /api/trading/monitor`
手动触发一次订单监控循环（检查挂单、评估退出规则、提交保护单）。

### `GET /api/trading/snapshots`
盈亏快照历史。Query：`days`（默认 7），`refresh`（是否实时刷新）。

---

## 17. Trading Instance Actions（危险操作）

**所有操作需要请求体中的 `confirmation` 字段与要求文本完全匹配。**

### `POST /api/trading/runs/{run_id}/cancel-orders`
撤消当前实例的所有已知订单。Body：
```json
{ "confirmation": "撤实例" }
```

### `POST /api/trading/runs/{run_id}/flatten`
仅按当前实例记录平仓（非全账户平仓）。Body：
```json
{ "confirmation": "平实例" }
```

### `POST /api/trading/runs/{run_id}/reset-risk`
重建本地风险状态（不触碰券商）。Body：
```json
{ "confirmation": "初始化风控" }
```

### `POST /api/trading/runs/{run_id}/delete`
删除本地实例记录（不撤单不平仓）。Body：
```json
{ "confirmation": "删除实例" }
```

### `POST /api/trading/flatten`
全账户全平：撤销当前 broker 账号所有订单并平仓。Body：
```json
{ "confirmation": "全平" }
```

### `POST /api/trading/runs/bulk-delete`
批量删除多个实例（仅本地记录）。Body：
```json
{ "confirmation": "批量删除实例", "run_ids": ["run-1", "run-2"] }
```

---

## 18. Auto-Trade（全自动交易）

全自动交易实例是定时唤醒的自动交易控制器。每个周期会生成一条 `auto_trade_cycles` 审计记录，并在需要执行完整交易 pipeline 时关联一个或多个 `trading_runs`。所有端点需 `can_trade`；真实券商自动交易的启动操作额外要求手输确认文本。

### `GET /api/auto-trade/instances`
列出当前 owner 的自动交易实例。Query：`limit`（默认 50）。

返回数组中每项包含：
- `id`：实例 ID，形如 `AUTO-XXXXXXXXXXXX`
- `name`、`status`：`stopped` / `active` / `paused`
- `use_broker`、`broker`、`broker_account`、`ai_provider`
- `symbols[]`：股票池，最多 8 个标的
- `interval_minutes`、`risk_preset`、`total_capital`、`session_policy`
- `next_run_at`、`last_run_at`：UTC ISO 时间；`next_run_at=null` 表示下个 scheduler tick 立即可运行
- `cycles_today`、`orders_today`、`realized_pnl_today`、`halted_reason`
- `last_cycle_summary`、`memory`
- `caps`：由 `risk_preset` 派生的自动交易风控上限

响应示例：
```json
[
  {
    "id": "AUTO-4AB81EE71E1A",
    "name": "Morning AI Trader",
    "status": "active",
    "use_broker": true,
    "broker": "longbridge",
    "broker_account": "live-us",
    "ai_provider": "deepseek",
    "symbols": ["SPY", "QQQ", "NVDA"],
    "interval_minutes": 5,
    "risk_preset": "conservative",
    "total_capital": 3000,
    "session_policy": "regular_only",
    "next_run_at": "2026-06-15T14:40:00+00:00",
    "cycles_today": 3,
    "orders_today": 0,
    "last_cycle_summary": {
      "status": "completed",
      "stage": "data_integrity_blocked",
      "orders": 0,
      "run_ids": ["79d4d6c2fbf54e6b9c4432b6f54d459c"]
    },
    "caps": {
      "max_order_cycles_per_session": 4,
      "max_open_positions": 2,
      "session_capital_budget_pct": 0.3,
      "max_allocation_pct_per_trade": 0.2,
      "max_daily_loss_pct": 0.06,
      "max_drawdown_pct": 0.08
    }
  }
]
```

`risk_preset` 可选值：`conservative`、`balanced`、`aggressive`。preset 会注入常规交易配置（止损止盈、入场单类型、Top N 等），同时由 auto-trade loop 执行每日下单周期、开仓数量、资金占用、日内亏损与回撤上限。

### `POST /api/auto-trade/instances`
创建自动交易实例，初始 `status="stopped"`。Body：
```json
{
  "name": "Morning AI Trader",
  "use_broker": true,
  "broker": "longbridge",
  "broker_account": "live-us",
  "ai_provider": "deepseek",
  "symbols": ["SPY", "QQQ", "NVDA"],
  "interval_minutes": 5,
  "risk_preset": "conservative",
  "total_capital": 3000,
  "session_policy": "regular_only",
  "config": {
    "market_data_source": "thetadata",
    "option_data_source": "thetadata",
    "strategy_modes": ["single_leg"]
  }
}
```

约束：
- `symbols` 至少 1 个，最多保留 8 个，自动转大写去重。
- `interval_minutes` 被限制在 1–240。
- `total_capital` 如提供会被限制在 0–10,000,000。
- `config` 会作为底层 `/api/trading/run-now` 配置的覆盖项，但 broker、AI provider、universe、auto-trade 风控字段由实例统一注入。

### `GET /api/auto-trade/instances/{instance_id}`
获取单个自动交易实例，并附带最近周期。Query：`cycles`（默认 20）。

### `PUT /api/auto-trade/instances/{instance_id}`
更新实例配置。Body 同创建接口；`symbols` 仍必须至少 1 个。返回更新后的实例视图。

### `DELETE /api/auto-trade/instances/{instance_id}`
删除本地自动交易实例配置，不撤单、不平仓、不删除已产生的交易 run。

响应：
```json
{ "status": "ok", "deleted": "AUTO-4AB81EE71E1A" }
```

### `POST /api/auto-trade/instances/{instance_id}/start`
启动自动交易实例。Dry-run 实例无需确认；`use_broker=true` 的真实券商实例必须提供确认文本。

Body：
```json
{ "confirmation": "全自动交易" }
```

启动后返回实例视图，`status="active"`，且 `next_run_at=null`，scheduler 下个 tick 会捡起该实例；实际是否下单仍由交易时段、readiness、风控、AI 决策和数据完整性校验决定。

### `POST /api/auto-trade/instances/{instance_id}/pause`
暂停实例。返回实例视图，`status="paused"`；不会撤销已有订单或关闭持仓。

### `POST /api/auto-trade/instances/{instance_id}/stop`
停止实例。返回实例视图，`status="stopped"` 且 `next_run_at=null`；不会撤销已有订单或关闭持仓。

### `GET /api/auto-trade/instances/{instance_id}/cycles`
列出实例周期历史。Query：`limit`（默认 50）。

周期字段：
- `id`、`instance_id`、`cycle_index`
- `started_at`、`finished_at`
- `session_state`、`intraday_phase`
- `status`：`running` / `completed` / `error`
- `dry_run`
- `plan`、`decision_gate`、`validation`
- `run_ids`：关联的 trading run ID 列表
- `summary`：前端列表摘要，通常包含 `status`、`stage`、`orders`、`run_status`、`run_ids`
- `error`

### `GET /api/auto-trade/instances/{instance_id}/cycles/{cycle_id}`
获取单个周期详情，并把 `run_ids` 对应的完整 `trading_runs` 附加到 `runs[]`。

响应示例：
```json
{
  "id": "cycle-1",
  "instance_id": "AUTO-A575D240FBC3",
  "cycle_index": 21,
  "session_state": "regular",
  "intraday_phase": "morning",
  "status": "completed",
  "dry_run": false,
  "run_ids": ["79d4d6c2fbf54e6b9c4432b6f54d459c"],
  "summary": {
    "status": "completed",
    "stage": "data_integrity_blocked",
    "orders": 0,
    "run_status": "failed"
  },
  "runs": [
    {
      "id": "79d4d6c2fbf54e6b9c4432b6f54d459c",
      "status": "failed",
      "stage": "data_integrity_blocked",
      "orders": [],
      "error": "数据完整性阻断：扫描结果中有 48 个候选合约 root 与股票池标的不一致：T260618C00023500(contract_root_mismatch:T!=SPY)"
    }
  ]
}
```

### 自动交易相关状态说明

`trading_runs.stage` / 周期 `summary.stage` 可能出现：

| 状态 | 含义 |
|---|---|
| `decision_gate_blocked` | 有候选但规则/AI 决策门禁不允许执行，通常 `orders=0` |
| `council_blocked` | AI 顾问/主持人没有产出可执行选择 |
| `data_integrity_blocked` | 扫描结果被数据完整性校验阻断，例如期权合约 root 与股票池标的不一致 |
| `strategy_manual_attention` | 多腿策略或保护状态需要人工处理 |
| `order_submitted` / `monitoring` / `closed` | 已进入订单、监控或结束生命周期 |

当出现 `data_integrity_blocked` 时，详情中的 `scan_results[].data_integrity`、`candidate_snapshot.data_integrity_rejected_count`、`candidate_snapshot.data_integrity_rejected_examples` 和 `events[].payload` 会包含被剔除数量与示例。此类记录不会提交订单，且 `orders=0` 时不计入 readiness 的连续失败熔断。

---

## 19. Beta Lottery（公测抽签）

### `GET /api/beta-lottery/status`
公开。查询抽签状态。Query：`entry_token`。

### `POST /api/beta-lottery/enter`
公开。登记参与抽签。Body：
```json
{ "email": "user@example.com" }
```

### `GET /api/beta-lottery/admin`
Admin。查看所有参与者和当前状态。

### `POST /api/beta-lottery/finalize`
Admin。结束本轮抽签，分配中签者。

### `POST /api/beta-lottery/admin/action`
Admin。执行公测抽签后台动作。Body：
```json
{
  "action": "configure",
  "announce_at": "2026-06-20T12:00:00Z",
  "registration_start_at": "2026-06-18T12:00:00Z",
  "slot_count": 100,
  "user_valid_days": 30,
  "limits": {
    "max_daily_scans": 50,
    "max_daily_ai_scans": 20,
    "max_watchlists": 10
  }
}
```

`action` 的具体取值由后端 `beta_lottery_admin_action` 解释；非法动作返回 `400`。

---

## 20. AI Usage

### `GET /api/admin/ai-usage`
Admin。按 owner、天数等维度查询 AI 用量汇总。Query：`owner_id`、`days`（默认 30）、`limit`。

### `GET /api/ai-usage/me`
查询自己的 AI 用量。Query：`days`（默认 30）、`limit`。

响应：
```json
{
  "total_tokens": 1250000,
  "total_cost": 3.42,
  "by_model": {
    "deepseek-v4-flash": { "tokens": 980000, "cost": 1.96 }
  },
  "daily_breakdown": [
    { "date": "2026-06-03", "tokens": 42000, "requests": 14 }
  ]
}
```

### `GET /api/auth/me/usage`
查询当前登录用户的资源用量与配额（ET 日期口径）。无需 admin 权限；未启用 auth 时返回空资源列表和说明。

响应：
```json
{
  "resources": [
    { "key": "daily_scans", "label": "每日扫描", "usage": 3, "limit": 50 },
    { "key": "daily_ai_scans", "label": "每日 AI 精扫", "usage": 1, "limit": 20 },
    { "key": "daily_ai_chat", "label": "每日 AI 对话", "usage": 4, "limit": 100 },
    { "key": "watchlists", "label": "股票池", "usage": 2, "limit": 10 },
    { "key": "scan_loop_instances", "label": "雷达实例", "usage": 1, "limit": 5 },
    { "key": "notification_channels", "label": "通知渠道", "usage": 2, "limit": 10 },
    { "key": "longbridge_accounts", "label": "Longbridge 账户", "usage": 1, "limit": 3 }
  ],
  "can_analyze": true,
  "can_trade": true,
  "is_admin": false,
  "expired": false
}
```

---

## 21. Chat（AI 对话助手）

### `GET /api/chat/tools`
返回对话助手可用的调查工具链目录（如行情数据、期权链分析等），用于前端展示和按需启用。无需 Body。

### `POST /api/chat`
提交一条对话消息，以 SSE（`text/event-stream`）流式返回助手回应、工具调用 trace 与最终文本。请求体含 `message`、可选 `tools`（启用的工具 ID 列表）、可选 `session_id` 与 `instance_id`（前端生成的轮次 ID，用于即时显示与断线恢复）。

### `GET /api/chat/sessions`
列出当前用户的对话会话。

### `POST /api/chat/sessions`
新建一个对话会话。

### `PATCH /api/chat/sessions/{session_id}`
更新当前用户会话的标题、provider、账号或启用工具；不存在或不属于当前用户时返回 `404`。

### `DELETE /api/chat/sessions/{session_id}`
删除指定会话。

### `GET /api/chat/sessions/{session_id}/messages`
获取指定会话的历史消息。

### `DELETE /api/chat/sessions/{session_id}/messages`
清空指定会话的历史消息（保留会话本身）。

---

## 22. Static Assets

以下端点直接返回静态文件（不在 OpenAPI schema 中显示）：

| 路径 | 文件 |
|---|---|
| `GET /logo.svg` | `web/dist/logo.svg` |
| `GET /logo.png` | `web/dist/logo.png` |
| `GET /favicon.ico` | `web/dist/favicon.ico` |
| `GET /favicon-16x16.png` | `web/dist/favicon-16x16.png` |
| `GET /favicon-32x32.png` | `web/dist/favicon-32x32.png` |
| `GET /apple-touch-icon.png` | `web/dist/apple-touch-icon.png` |
| `GET /icon-192.png` | `web/dist/icon-192.png` |
| `GET /icon-512.png` | `web/dist/icon-512.png` |
| `GET /manifest.webmanifest` | `web/dist/manifest.webmanifest` |
| `GET /sw.js` | `web/dist/sw.js` |

所有以上路径也有对应的 `HEAD` 端点。

React SPA 的静态资源挂载于 `/assets`，由 Vite 构建产物提供。
非 API 路径（如 `/site`、`/` 等）返回 `index.html` 由 React Router 接管。

### 缓存头策略（Cache-Control）

| 资源 | 头 | 说明 |
|---|---|---|
| `/assets/*`（带内容哈希） | `public, max-age=31536000, immutable` | 文件名带哈希，内容不可变，可永久缓存。 |
| `/`、`index.html` | `no-store` | 每次取最新，确保新部署的 chunk 哈希引用即时生效。 |
| `/sw.js` | `no-store, no-cache, must-revalidate, max-age=0` + `CDN-Cache-Control: no-store` + `Cloudflare-CDN-Cache-Control: no-store` | Service Worker 脚本必须既不被浏览器、也不被 CDN（Cloudflare）缓存。否则旧 SW 会在边缘缓存有效期内继续向客户端提供过期 `index.html`（引用已被新部署清除的资源哈希），导致整页丢样式 / 404。Cloudflare 对静态 `.js` 会忽略普通 `no-cache`，因此额外发送 `CDN-Cache-Control`/`Cloudflare-CDN-Cache-Control`，使 `cf-cache-status` 变为 `BYPASS`。 |

> 注：`/sw.js` 的缓存头由 `web_api.py` 的 `_SW_NO_CACHE_HEADERS` 常量统一控制，应用于 `GET` 与 `HEAD` 两个处理器。部署后可用 `curl -sD- -o/dev/null https://<host>/sw.js | grep -i cache` 验证应出现 `no-store` 与 `cf-cache-status: BYPASS`。

---

## Error Codes

| HTTP Status | 含义 |
|---|---|
| `400` | 请求参数错误 |
| `401` | 未登录或会话过期 |
| `403` | 权限不足（如 `can_trade=false` 访问交易接口） |
| `404` | 资源不存在 |
| `409` | 资源冲突（如重复名称） |
| `422` | Pydantic 校验失败 |
| `429` | 超过速率/配额限制 |
| `500` | 服务器内部错误 |
| `503` | 服务不可用（如依赖服务未就绪） |
