# Trading Risk And Safety Spec

本文档定义实盘交易模块的安全边界、确认动作、保护状态、智能退出和前端展示要求。任何实盘功能改动都必须先检查本文档。

## 1. 核心原则

- 系统是交易工作流软件，不承诺收益。
- AI 只能辅助决策，不能绕过结构化门禁和风险限制。
- 行情源和券商通道必须分离。
- 下单、撤单、平仓和成交回报必须来自当前 broker。
- 软件止盈止损和智能退出依赖 worker、行情和 broker API，不是券商原生保证单。
- `status=failed` 不代表无订单，必须检查订单和 broker 回报。
- 删除本地实例永远不代表撤单或平仓。

## 2. 真实交易动作

| 操作 | API | 确认文本 | 是否触碰券商 | 说明 |
|---|---|---|---|---|
| 手动创建交易实例 | `POST /api/trading/run-now` | 无固定文本，但需 readiness 通过 | 可能 | 配置允许自动执行时会提交订单 |
| 全账户全平 | `POST /api/trading/flatten` | `全平` | 是 | 当前 broker 账号全部撤单并平仓 |
| 撤实例订单 | `POST /api/trading/runs/{id}/cancel-orders` | `撤实例` | 是 | 撤当前实例已知订单 |
| 平当前实例 | `POST /api/trading/runs/{id}/flatten` | `平实例` | 是 | 只按当前实例记录平仓 |
| 手动订单监控 | `POST /api/trading/monitor` | 无 | 可能 | 触发软件保护/退出 |
| 重置实例风控 | `POST /api/trading/runs/{id}/reset-risk` | `初始化风控` | 否 | 只重建本地风险状态 |
| 删除实例 | `POST /api/trading/runs/{id}/delete` | `删除实例` | 否 | 只删本地记录 |
| 批量删除实例 | `POST /api/trading/runs/bulk-delete` | `批量删除实例` | 否 | 只删本地记录 |

前端要求：

- 真实交易动作必须和本地动作视觉分区。
- 全平、撤单、平实例必须要求用户输入确认文本。
- 操作后无论成功失败，都要刷新交易实例、列表、readiness 和 snapshots。

## 3. Readiness Gate

创建实盘交易实例前必须通过 readiness。

阻断类问题：

- 未开启 `live_enabled`。
- 未配置当前 broker 对应账号。
- `market_data_source=longbridge` 但无可用 Longbridge 凭证。
- AI provider 不可用。
- 策略资金小于等于 0。
- 股票池为空。
- 日内实例数超过上限。
- 连续失败超过上限。
- 未保护数量超过上限。
- 默认止损超过单合约止损上限。
- 市价单要求保护但软件止盈止损未开启。

前端规则：

- `readiness.ok=false` 禁止点击“创建交易实例”。
- `issues` 用 danger 展示。
- `warnings` 用 warning 展示。
- `next_run_slot` 和 `schedule_preview` 只作信息，不替代 readiness。

## 4. 下单前门禁

### 单腿

必须检查：

- 合约代码可规范化。
- bid/ask 可用。
- spread pct 不超过阈值。
- volume/OI 基本可用。
- theta/ask 不过高。
- IV/RV/IV Rank 不明显过热。
- 入场价可计算。
- 资金分配大于最小下单单位。

### 多腿策略

必须检查：

- 策略族与用户选择一致。
- 所有 legs 有合法合约代码。
- 每条腿 bid/ask 可用。
- 净价方向正确：debit/credit 不能反。
- credit-to-width 或 debit-to-width 在合理范围。
- 最大亏损可计算。
- 宽度、到期、执行价结构符合策略。
- 自动执行开启时才允许提交多腿订单。

阻断原因必须进入 `orders[].strategy_net_price_gate` 或相关 blockers。

## 5. Partial Execution

多策略执行允许出现部分执行。

典型路径：

1. 策略 A 提交并成交。
2. 策略 B 提交并成交。
3. 策略 C 因报价刷新失败、invalid symbol、净价门禁或资金不足被阻断。
4. 顶层 run 可能是 `status=failed`。
5. `stage=strategy_partial_execution`。
6. 已成交策略必须继续进入保护监控。

前端必须展示：

- `stage=strategy_partial_execution`。
- 已提交 order ids。
- `entry_filled_quantity`。
- `strategy_fill_ledger`。
- `risk_tracking_active`。
- `protection_state`。

禁止：

- 只因 `status=failed` 隐藏实例。
- 将部分执行显示成“完全失败”。
- 删除本地实例前不提示券商侧可能仍有订单/持仓。

## 6. 保护状态

| 状态 | 语义 | 前端 tone |
|---|---|---|
| `not_started` | 未开始保护 | muted |
| `protected` | 单腿保护已建立 | ok |
| `software_protected` | 软件保护中 | warning |
| `strategy_protected` | 多腿策略保护中 | ok |
| `strategy_exiting` | 策略退出中 | warning |
| `strategy_partial_exiting` | 策略部分退出中 | warning |
| `strategy_residual_tracking` | 残腿追踪中 | warning |
| `broker_combo_close_required` | 券商要求组合平仓 | danger |
| `strategy_exit_failed` | 策略退出失败 | danger |
| `completed` / `exited` | 已完成退出 | ok |

必须展示的保护字段：

- 当前状态。
- 是否需要人工处理。
- 未保护数量。
- active stop/take-profit/smart-exit。
- 触发条件和当前值。
- 最近错误。

## 7. 智能退出规则

支持的退出类型：

| 类型 | 变量 | 说明 |
|---|---|---|
| 软件止损 | option mark / strategy net | 达到亏损阈值退出 |
| 软件止盈 | option mark / strategy net | 达到盈利阈值退出 |
| 分层止盈 | quantity pct + profit pct | 分批提交退出 |
| 时间限制退出 | time / DTE / holding minutes | 到时间退出 |
| 正股价格退出 | underlying last / VWAP / support / resistance | 标的价格破坏 thesis |
| Greeks 变化退出 | delta/gamma/theta/vega/iv | 风险暴露偏离计划 |
| AI 计划失效退出 | invalidation level / scenario shift | AI 风险计划失效 |
| 残腿追踪 | remaining legs | 多腿退出后剩余风险处理 |

每个规则应包含：

- `type`
- `field`
- `operator`
- `threshold` 或 `change_pct`
- `current_value`
- `source`
- `reason`
- `triggered`

前端展示：

- AI 规则标记为 `AI 风控`。
- 系统默认标记为 `系统默认`。
- 触发后显示提交 broker、order id 和数量。

### 7.1 自适应限价单 (Adaptive Limit)

介于市价单和限价单之间的下单方式：以限价单为基础，价格贴近中间价并向对手价"行走"（buy 略高于 mid、sell 略低于 mid），未成交则逐轮加价，最终必然兜底为市价。目的是省下每次都吃满半个价差的成本，同时保证一定会成交。

**核心定价**（`ai_option_scanner/adaptive_pricing.py`，入场/出场共用同一份逻辑，无跨模块依赖）：

- `limit = mid ± aggr × half_spread`，`aggr=0` 为 mid、`aggr=1` 为对手价（touch）。
- `aggr` 随尝试轮次从 `AI_OPTION_ADAPTIVE_AGGR_START`（默认 0.3）走到 1.0，最后一轮必为 marketable。
- 报价不可信（单边、交叉、价差离谱）时回退到保守 touch —— **永远不会比普通限价单更差**。
- US 期权 tick 自行取整（<$3 为 $0.01，≥$3 为 $0.05；buy 向上、sell 向下，跨 $3 档重新取整）。

**开关**（全部默认 OFF，不开则行为零变化）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AI_OPTION_ADAPTIVE_ORDER_ENABLED` | off | 总开关 |
| `AI_OPTION_ADAPTIVE_EXIT_ENABLED` | off | 出场自适应（与总开关 AND 门控） |
| `AI_OPTION_ADAPTIVE_AGGR_START` | 0.3 | 首轮激进度 |
| `AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES` | 3 | 出场行走轮数上限（clamp [1,10]） |

启用方式：节点 `.env` 设开关 + 配置 `entry_order_type="adaptive"`（别名 `smart`/`mid`/`adaptive_limit`）。

**安全边界（不可逾越）：**

- **软件止损始终纯市价** —— 资本保护必须确定成交，不走自适应。
- **多腿组合的空头平仓（buy-to-close）始终市价**（Phase-13 裸空保护）；只有多头腿（sell-to-close）在 `short_first` 排序开启（空头已先平）时才走自适应 —— 此时挂着的多头限价是有限风险的多头，绝不会造成裸空。
- **净价门禁不变**（保守上界）；自适应价永远 ≤ ask，门禁只会更宽松。
- 每轮 escalation 先撤单、再用 `order_detail` 复查是否已在竞态窗口成交（**绝不重复平仓**），才加价重挂；走到 `max_cycles` 兜底市价，挂单绝不会把已触发的仓位晾在场上。
- monitor 5s 巡检周期兼作行走计时器，非阻塞（Phase 9/11）。

## 8. 行情安全

保护行情优先级：

1. ThetaData。
2. yfinance。
3. Longbridge。

规则：

- 保护行情失败不能静默忽略。
- 期权报价缺 bid/ask 时可以用 last/indicative fallback，但必须标注。
- 正股价格退出必须标注 underlying source。
- Greeks 变化退出必须标注 Greeks 来源或估算。

## 9. Broker 安全

Longbridge：

- 账号由 `/api/longbridge/*` 管理。
- 下单、撤单、平仓、成交回报走 Longbridge adapter。

Alpaca：

- 账号由 `/api/brokers/*` 管理。
- `paper=true` 和 live 必须明确显示。
- 使用 Alpaca 时，行情不从 Alpaca 取。

通用 broker adapter 必须返回：

- account snapshot。
- positions。
- order submit result。
- order id。
- cancel result。
- fills/executions 或不可用说明。
- 标准化错误。

## 10. PnL 安全

PnL 必须有来源口径。

| 口径 | 可显示为 |
|---|---|
| `broker_confirmed` | 已确认 |
| `broker_and_estimate` | 券商成交 + 行情估算 |
| `local_estimate` | 本地估算 |

警告必须显示：

- `entry_price_estimated`
- `entry_price_unavailable`
- `exit_order_pending_broker_fill`
- `exit_price_estimated`
- `open_positions_use_mark`

禁止：

- 未平仓 mark 估算显示为已实现收益。
- Alpaca 资产混 Longbridge 成交。
- 部分成交缺价时不给 warning。

## 11. 多时段调度安全

默认三时段：

| Slot | 时间 ET | 目标 |
|---|---|---|
| `open_confirmation` | 09:45 | 开盘确认 |
| `midday_structure` | 12:45 | 中盘结构 |
| `power_hour_risk` | 15:10 | 尾盘风控 |

安全规则：

- `trading_schedule_fires` 保证每个 owner/date/profile/slot 只触发一次。
- `claimed` 长时间未完成可恢复为 `retrying`。
- `force_no_overnight=true` 时不应开新隔夜风险。
- 配置漂移时 session 应标记 degraded。

前端展示：

- 下一个 slot。
- 今日 fired/skipped/failed。
- slot action 和 gate profile。
- session degraded 警告。

## 12. Incident Playbook

### 发现交易实例显示 failed

1. 打开实例详情。
2. 查看 `stage`。
3. 查看 `orders[]` 是否有 order id。
4. 查看 `entry_filled_quantity`。
5. 查看 `strategy_fill_ledger`。
6. 核对 broker 侧订单和持仓。
7. 如有未保护数量，进入人工处理。

### 软件保护失败

1. 查看 `protection_state` 和最近错误。
2. 手动运行 `/api/trading/monitor`。
3. 查看行情源是否可用。
4. 查看 broker API 是否可用。
5. 如仍失败，使用 broker 原生界面处理。
6. 在系统中记录人工处理结果。

### PnL 不一致

1. 查看 `pnl_basis`。
2. 查看 `pnl_warnings`。
3. 刷新 `/api/trading/snapshots?refresh=true`。
4. 核对当前 broker account。
5. 核对是否 partial fill 或 exit pending。
6. 不用本地估算覆盖券商确认值。

## 13. Release Safety Checklist

每次涉及实盘的发布前检查：

- 后端测试通过。
- 前端构建通过。
- `/api/trading/readiness` 正常。
- worker 正常运行。
- order monitor 正常运行。
- 当前交易开关状态符合预期。
- broker API 未在错误节点开启。
- 数据库和 Redis 指向生产共享状态。
- 通知渠道测试不泄露 token。
- partial execution 实例仍能正确展示。
- 自适应限价单开关状态符合预期（默认 OFF；如开启，确认软件止损仍为纯市价、空头平仓仍为市价）。
