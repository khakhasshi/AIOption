# Frontend Domain Glossary

本文档定义前端重构时必须统一使用的业务语义、字段口径和中文文案。目标是让“分析、机会、通知、实盘、风控、退出”在所有页面上使用同一套词，不再出现同一概念多种叫法。

## 1. 命名原则

- 代码层命名使用后端字段原名：`scan`、`opportunity`、`scan_loop_instance`、`trading_run`、`protection_state`。
- UI 文案使用本文件定义的中文显示名。
- 不在组件内临时发明状态文案；统一从 `utils/display.js` 或未来的语义字典模块读取。
- 前端不能仅凭单个字段推断业务结论。交易实例必须组合 `status`、`stage`、`lifecycle_state`、`protection_state`、`orders[]` 和券商回报。

## 2. 核心对象

| 后端对象 | UI 名称 | 说明 | 主要入口 |
|---|---|---|---|
| `scan_run` | 分析实例 | 一次自然语言/预设分析请求，可同步或异步执行 | `/`、`/api/scans` |
| `scan_mark` | 星标/笔记 | 用户对分析实例的收藏、笔记和标签 | 分析历史 |
| `scan_trigger` | Wait Trigger / 等待触发器 | 观察某个价格、技术指标、期权报价或重新扫描分数 | 分析器、机会详情 |
| `watchlist` | 股票池 | 循环扫描使用的标的集合 | `/watchlists` |
| `scan_loop_instance` | 循环扫描实例 | 按规则和频率扫描股票池或单票的雷达配置 | `/watchlists` |
| `scan_loop_run` | 雷达运行 | 一次循环扫描运行记录 | 雷达运行详情 |
| `scan_loop_run_item` | 雷达单标的明细 | 某次运行中单个 symbol 的快照、规则、AI/通知审计 | 雷达运行详情 |
| `opportunity_instance` | 机会实例 | 可持续追踪的交易参考机会 | `/opportunities/{id}` |
| `notification_channel` | 通知渠道 | Telegram、Discord、飞书等渠道绑定 | `/notifications` |
| `notification_event` | 通知事件 | 一条待发送、已发送或失败的通知 | 通知中心 |
| `trading_config` | 实盘配置 | 实盘扫描、券商、风控、调度和资金参数 | 实盘交易 |
| `trading_run` | 交易实例 | 一次实盘交易任务，从扫描到下单、保护、退出和复盘 | `/trading/instances/{id}` |

## 3. 页面名称

| 路径 | 推荐页面名 | 不推荐叫法 |
|---|---|---|
| `/` | 分析器 | 首页、扫描页、主页面混用 |
| `/guide` | 使用说明 | 帮助页 |
| `/rule-presets` | 预设说明 | 规则说明页 |
| `/watchlists` | 机会雷达 | 股票池页、观察页混用 |
| `/opportunities/{id}` | 机会详情 | 机会弹窗、触发详情混用 |
| `/notifications` | 通知中心 | 消息中心、提醒中心混用 |
| `/notifications/guide` | 通知绑定教程 | 通知帮助 |
| `/trading/instances/{id}` | 交易实例详情 | 订单详情、实盘详情混用 |
| `/admin` | 管理后台 | 用户页 |
| `/admin/ai-costs` | AI 用量 | 成本页 |

## 4. 行情源和券商

| 概念 | UI 文案 | 说明 |
|---|---|---|
| `thetadata` | ThetaData | 默认行情源 |
| `yfinance` | yfinance | 备用行情源 |
| `longbridge` as market data | Longbridge API | 行情兜底或显式行情源 |
| `longbridge` as broker | Longbridge | 券商订单通道 |
| `alpaca` | Alpaca | 模块化券商订单通道 |
| `auto` | 自动（Theta优先） | 兼容入口，按 ThetaData 优先 |

前端必须区分：

- `market_data_source`：行情、期权链、保护报价和扫描依据。
- `broker` / `broker_account` / `longbridge_account`：真实订单、撤单、平仓和回报来源。

禁止把 `market_data_source=longbridge` 等同于 `broker=longbridge`。

## 5. 策略结构

| strategy mode | UI 名称 | 说明 |
|---|---|---|
| `single_leg` | 单腿 | 单个 call 或 put |
| `spread` | 价差 | 通用定义风险价差，通常偏 debit/debit-vs-credit 比较 |
| `credit_spread` | 信用价差 | bull put credit spread 或 bear call credit spread |
| `straddle` | 跨式 | 同执行价 call + put |
| `strangle` | 宽跨 | 不同执行价 call + put |
| `collar` | 领式 | 持股 + protective put + covered call |
| `covered_call` | 备兑 | 持股卖 call |
| `cash_secured_put` | 现金担保 Put | 现金担保卖 put |
| `calendar` | 日历价差 | 同执行价不同到期 |
| `diagonal` | 对角价差 | 不同执行价不同到期 |
| `poor_mans_covered_call` | 穷人备兑 | LEAPS + short call |
| `iron_condor` | 铁鹰 | bear call spread + bull put spread |
| `butterfly` | 蝶式 | 三执行价、四腿结构 |

语义约束：

- `credit_spread` 不能显示成“价差”后再给单腿结论。
- `iron_condor` 固定叫“铁鹰”，不要混用“铁秃鹰”。
- `butterfly` 固定叫“蝶式”，不要和 iron condor 混用。
- 如果后端返回策略族与用户选择不一致，前端要显示“策略族不一致”警告，不要静默展示。

## 6. 扫描和雷达状态

| 字段值 | UI 文案 | 含义 |
|---|---|---|
| `queued` | 待开始 | 已创建，等待执行 |
| `running` | 进行中 | 正在扫描或执行 |
| `succeeded` | 已完成 | 任务正常完成 |
| `failed` | 失败 | 任务失败，但交易实例仍需检查 orders |
| `skipped` | 已跳过 | 调度/规则决定跳过 |
| `reviewed` | 已复盘 | 复盘完成 |

雷达决策词固定为：

- `观察`：未满足开仓条件，继续记录。
- `触发`：条件满足，形成机会或提醒。
- `追踪`：已有机会或交易计划，继续跟踪变量。
- `退出`：触发止盈、止损、时间、价格、Greeks 或结构失效条件。
- `复核`：变化明显，需要 AI 或人工重新评估。

## 7. 交易实例状态

交易实例不能只看 `status`。

| 字段 | UI 名称 | 用途 |
|---|---|---|
| `status` | 任务状态 | 顶层运行结果 |
| `stage` | 执行阶段 | 当前阶段或失败发生阶段 |
| `lifecycle_state` | 生命周期 | 实例业务生命状态 |
| `protection_state` | 保护状态 | 止盈止损、智能退出、残腿追踪状态 |
| `orders[].status` | 订单状态 | 每条订单或策略的执行状态 |

重点语义：

- `status=failed` 不等于无订单。
- `stage=strategy_partial_execution` 表示部分策略已经提交或成交，必须继续展示和监控。
- `protection_state=strategy_residual_tracking` 表示仍有残腿或剩余风险。
- `broker_combo_close_required` 表示券商侧要求组合平仓，前端必须提示人工核对。

## 8. PnL 和价格口径

| 概念 | 推荐文案 | 来源 |
|---|---|---|
| 已实现盈亏 | 已实现 PnL | 券商成交回报或策略 ledger |
| 浮动盈亏 | 浮动 PnL | 保护行情 mark 估算 |
| 账户资产 | 账户资产 | `/api/trading/snapshots` |
| 策略资金 | 策略资金 | `trading_config.total_capital` |
| 入场净价 | 入场净价 | strategy ledger 或订单成交 |
| 退出净价 | 退出净价 | strategy ledger 或退出订单成交 |

PnL 展示必须显示 basis：

- `broker_confirmed`：券商成交确认。
- `broker_and_estimate`：券商成交 + 行情估算。
- `local_estimate`：本地估算。

如果存在 `pnl_warnings`，必须展示警告，不得只显示数字。

## 9. 通知语义

| 对象 | UI 文案 | 说明 |
|---|---|---|
| `notification_channel` | 通知渠道 | 一种外部投递配置 |
| `notification_event` | 通知事件 | 一条具体通知 |
| `delivery_log` | 投递日志 | 一次外部请求记录 |
| `payload_preview` | Payload 预览 | 不发送，只查看实际请求 |
| `test` | 测试发送 | 创建测试事件并发送 |

通知事件状态：

- `pending`：待发送。
- `sent`：已发送。
- `failed`：失败，可查看错误和重试。
- `suppressed`：被冷却、限额或市场策略抑制。

## 10. 禁用词和易混词

| 不推荐 | 推荐 |
|---|---|
| 首页 | 分析器 |
| 消息中心 | 通知中心 |
| 提醒实例 | 通知事件或机会实例，按真实对象选择 |
| 订单详情 | 交易实例详情，除非只展示单笔券商订单 |
| 铁秃鹰 | 铁鹰 |
| GEX 状态随便翻译 | 正 GEX / 负 GEX / 中性 / 混合 |
| AI 给的单子 | AI 决策或交易计划 |
| 实盘失败 | 交易任务失败，需要结合订单状态说明 |
