# Architecture Decision Records

本文档记录 AI Option 当前关键架构决策。它不是事后说明，而是后续前端重构、后端重构、Rust 迁移、券商扩展和生产运维时必须保留的约束。

格式：

- `Status`：`Accepted`、`Superseded`、`Proposed`。
- `Context`：当时面临的问题。
- `Decision`：选择。
- `Consequences`：收益、代价和后续约束。

## ADR-001: 行情源和券商通道必须分离

Status: Accepted

Context:

期权交易系统同时需要行情、期权链、Greeks/GEX、下单、撤单、成交回报和账户资产。不同供应商在这些能力上不对等：ThetaData 更适合作行情与期权数据，Longbridge/Alpaca 更适合订单和账户回报。把行情源和下单券商绑定会导致 Alpaca 接入时错误地从 Alpaca 拉行情，也会导致 Longbridge 行情不可用时影响订单回报。

Decision:

系统保留两个独立维度：

- `market_data_source`：分析行情、期权链、保护报价、Greeks/GEX 输入。
- `broker`：订单提交、撤单、平仓、成交回报、账户资产。

行情优先级为 ThetaData -> yfinance -> Longbridge。订单回报必须来自当前 broker。

Consequences:

- 前端必须同时显示行情源 badge 和券商 badge。
- PnL 不能混用不同 broker 的资产/成交。
- Alpaca 只作为 broker adapter，不作为行情源。
- 后续接 IBKR、Tradier 等券商时，不允许破坏行情链。

## ADR-002: ThetaData 是默认行情入口，`auto` 按 ThetaData 优先

Status: Accepted

Context:

系统早期使用 yfinance 默认行情，但 yfinance 在期权链稳定性、延迟、结构化字段和生产一致性上不足。ThetaData 更适合作为订阅用户的主行情源，但当前 ThetaData STANDARD 订阅不包含完整 Greeks professional 端点。

Decision:

- `market_data_source=thetadata` 为默认。
- `market_data_source=auto` 也按 ThetaData 优先。
- 显式选择 `yfinance` 或 `longbridge` 时才切换。
- ThetaData 不提供完整 Greeks 时，本地 Black-Scholes 估算并复用可用 IV 输入。

Consequences:

- 文档和 UI 不能再写“yfinance 默认”。
- Greeks/GEX 应标注估算来源。
- ThetaData 凭证缺失时，生产应允许降级到 yfinance/Longbridge，但必须暴露缺数据原因。

## ADR-003: AI 不是单点裁决，必须受结构化门禁约束

Status: Accepted

Context:

期权 AI 如果只看方向，容易买到方向对但 IV 压缩、价差过宽、theta 过高或策略结构错误的合约。尤其用户选择信用价差、铁鹰、蝶式时，AI 不能最终给一张单腿。

Decision:

- AI 输出必须经过策略族校验和决策门禁。
- 策略模式必须和最终方案一致。
- 多腿策略走腿级报价、净价门禁和风险审计。
- AI 只能基于 payload 中存在的数据写结论，不能编造 HIRO、dealer inventory、真实订单流或未接入数据。

Consequences:

- 前端必须展示 blockers/warnings，不可只展示 AI 正文。
- AI 输出不合格时可以降级为观察或规则兜底。
- 所有分析链都要保留结构化 payload，不能只有自然语言。

## ADR-004: 交易实例允许 `failed + partial_execution`

Status: Accepted

Context:

多腿或多策略实盘执行中，前几组策略可能已经提交或成交，后续策略可能因 invalid symbol、报价刷新失败、净价门禁或资金不足被阻塞。如果顶层状态只能表达成功/失败，前端可能把 `failed` 实例隐藏，从而漏掉真实订单和风险。

Decision:

交易实例顶层 `status=failed` 只代表本轮任务未完全成功，不代表没有订单。必须结合：

- `stage`
- `lifecycle_state`
- `protection_state`
- `orders[]`
- `strategy_fill_ledger`
- broker order ids

`stage=strategy_partial_execution` 是一等状态，表示需要继续展示/监控已提交或已成交策略。

Consequences:

- 前端不能只按 `status` 过滤或隐藏实例。
- 详情页必须展示已提交 order id、成交数量和 ledger。
- 运维排查必须核对券商侧。
- 删除本地实例不等于撤单或平仓。

## ADR-005: 软件保护和智能退出是工作流保护，不是券商保证单

Status: Accepted

Context:

券商并不总支持复杂期权策略的原生保护单。Paper/live、组合订单、期权类型和券商 API 限制也不同。软件止盈止损和智能退出能降低风险，但依赖行情、worker、broker API 和网络。

Decision:

系统支持软件止损、软件止盈、分层止盈、时间退出、正股价格退出、Greeks 变化退出、AI 计划失效退出和残腿追踪，但必须明确这是软件监控链路。

Consequences:

- 前端必须显示保护状态和监控变量。
- 后端 order monitor 必须可观测。
- 任何保护失败都要进入人工处理状态。
- 用户文案不能暗示“保证止损”。

## ADR-006: 机会雷达采用 AI 剧本缓存，而不是每轮无条件重写

Status: Accepted

Context:

盘中雷达可能 3 分钟或 5 分钟扫描一次。如果每轮都调用 AI，会成本高、延迟高、输出重复且噪音大。但完全不用 AI，又无法提供交易台风格的情景判断。

Decision:

- 首个有意义状态调用 AI 生成情景剧本。
- 后续扫描如果关键特征相似，复用上一轮报告。
- 当状态、预筛、提醒、RVOL bucket、GEX regime、价格/VWAP、支撑阻力或缓存年龄明显变化时，重新调用 AI。
- 每轮最多 AI 报告数量由 `AI_OPTION_SCAN_LOOP_AI_REPORT_TOP_N` 控制。

Consequences:

- 前端要显示“AI 已重写”或“沿用上一轮”。
- 通知消息应包含观察、结论、决策和风控，而不是只发规则未通过。
- 缓存不能掩盖失效条件；结构明显变化必须重写。

## ADR-007: 通知是私密投递链路，必须有审计日志

Status: Accepted

Context:

订阅客户依赖 Telegram、Discord、飞书、WhatsApp 等渠道获得雷达提醒。外部平台经常失败、限流或配置错误。只发请求不记录日志会导致无法排查。

Decision:

通知中心保存：

- `notification_channels`
- `notification_events`
- `notification_delivery_logs`

每次测试、自动投递、手动重试都要记录事件和投递日志。Payload preview 不发送外部请求。

Consequences:

- 前端必须提供渠道测试、payload preview、事件日志和投递日志。
- 敏感字段永远不返回完整值。
- 失败通知可以手动重试。

## ADR-008: Web/Worker 角色分离是生产默认模型

Status: Accepted

Context:

Web 请求、扫描队列、交易调度、机会调度和订单监控放在同一进程会产生重复调度和可观测性问题。多服务器部署时，如果每台都运行 scheduler，可能重复触发交易。

Decision:

使用 `AI_OPTION_PROCESS_ROLE=web|worker|all`：

- `web`：只提供 HTTP 和 SPA。
- `worker`：运行扫描 worker、交易 scheduler、机会 scheduler、order monitor。
- `all`：本地开发或单进程部署。

Consequences:

- Docker Compose 默认 `app=web`、`worker=worker`。
- 第二线路/分析节点可以关闭 broker API、trading scheduler 或 order monitor。
- 部署 runbook 必须检查 worker 是否唯一且健康。

## ADR-009: Postgres + Redis 是生产状态层，SQLite 只作本地/迁移兜底

Status: Accepted

Context:

SQLite 适合单机开发，但生产有多服务器、worker、队列、调度锁和并发写入。Redis 也需要承担扫描队列和共享锁。

Decision:

- Docker 生产默认 Postgres + Redis。
- SQLite 只在未配置数据库时作为本地兜底。
- 旧 SQLite 可通过 `migrate_sqlite_to_postgres.py` 迁移。

Consequences:

- 生产部署必须检查 `AI_OPTION_DATABASE_URL` 和 `AI_OPTION_REDIS_URL`。
- 多服务器必须共享同一状态层，不能各自写本地 SQLite。
- 数据模型文档以 Postgres/SQLite 兼容表为准。

## ADR-010: 前端语义收敛优先于视觉重构

Status: Accepted

Context:

系统复杂度来自业务语义：分析实例、机会实例、循环扫描、通知事件、交易实例、订单状态、保护状态、PnL 口径。只重构视觉组件会让状态含义继续分裂。

Decision:

前端重构先建立语义文档、状态机、API 视图契约和组件系统，再做页面拆分和视觉调整。

Consequences:

- 新组件必须复用统一 formatter 和状态字典。
- 页面不能发明新状态文案。
- 交易/PnL/保护状态优先正确，再考虑视觉美化。
