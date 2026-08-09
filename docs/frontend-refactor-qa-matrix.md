# Frontend Refactor QA Matrix

本文档定义前端语义收敛和重构后的验收矩阵。每次大规模前端改动都应按本矩阵抽查，尤其是实盘交易和通知链路。

## 1. 基础权限矩阵

| 场景 | 账号状态 | 期望 |
|---|---|---|
| 未登录访问 `/` | 无 session | 显示登录页 |
| 未登录访问 `/beta-lottery` | 无 session | 可访问 |
| 普通分析用户访问 `/` | `can_analyze=true` | 可扫描 |
| 普通分析用户访问 `/watchlists` | `can_analyze=true` | 可使用机会雷达 |
| 普通分析用户访问 `/trading/instances/{id}` | `can_trade=false` | 显示交易权限页 |
| 交易用户访问实盘页 | `can_trade=true` | 可查看配置和实例 |
| 非管理员访问 `/admin` | `is_admin=false` | 显示管理员权限页 |
| 管理员访问 `/admin` | `is_admin=true` | 可查看用户管理 |

## 2. 分析器矩阵

| 场景 | 输入 | 期望 |
|---|---|---|
| ThetaData 默认扫描 | `market_data_source=thetadata` | 显示 ThetaData badge，候选池可渲染 |
| 自动行情扫描 | `market_data_source=auto` | 显示自动（Theta优先） |
| yfinance 扫描 | `market_data_source=yfinance` | 不要求交易权限 |
| Longbridge 行情扫描，无交易权限 | `market_data_source=longbridge` | 阻止提交并提示权限 |
| 无 AI 扫描 | `use_ai=false` | 有 fallback/规则结果 |
| 三顾问扫描 | `council=true` | 显示 council trace |
| 扫描失败 | 后端返回 failed | 显示错误，停止 loading |
| 空候选池 | candidates 为空 | 显示空态，不崩溃 |
| 星标/笔记 | PATCH mark | 历史列表刷新 |
| 从扫描创建 trigger | 技术指标/价格/期权报价 | Trigger 出现在列表 |

## 3. 策略结构矩阵

| 场景 | 期望 |
|---|---|
| 选择单腿 | 最终方案可以是单腿 |
| 选择信用价差 | 最终方案必须是 credit spread，不能展示为单腿 |
| 选择铁鹰 | UI 显示“铁鹰”，legs 为两组价差 |
| 选择蝶式 | UI 显示“蝶式”，不能混作铁鹰 |
| 多策略候选存在 blockers | 显示 blockers，不隐藏 |
| AI 输出策略族不一致 | 显示策略族不一致警告 |
| 合约代码来自 ThetaData | 下单模块能识别并规范化 |
| 合约代码来自 yfinance | 下单模块能识别并规范化 |
| 合约代码来自 Longbridge | 下单模块能识别并规范化 |

## 4. 机会雷达矩阵

| 场景 | 期望 |
|---|---|
| 创建股票池 | 列表出现新股票池 |
| 创建循环实例 | 实例出现在 `/watchlists` |
| 暂停实例 | 状态显示已暂停，不进入 due |
| 手动 run now | 生成 run summary 和 item |
| test rules | 不发送通知、不创建真实机会 |
| SPY 通用模板 | symbols 为 SPY，频率 3 分钟 |
| 信用价差模板 | strategy family 为 credit_spread |
| AI 剧本首次生成 | 显示 AI 已重写 |
| AI 剧本相似复用 | 显示沿用上一轮 |
| 状态明显变化 | 重新生成报告 |
| 缺 HIRO/dealer inventory | 明确显示未接入，不显示伪数据 |
| 命中提醒 | 创建 notification_event |
| 达到冷却/限额 | 显示 suppressed reason |

## 5. 机会实例矩阵

| 场景 | 期望 |
|---|---|
| 打开机会详情 | thesis、risk_plan、linked_triggers 可见 |
| 暂停机会 | follow-up 停止，状态刷新 |
| 恢复机会 | follow-up 恢复 |
| 归档机会 | 从活跃列表移出或标记归档 |
| 手动 check | 时间线追加事件 |
| 从机会创建 trigger | linked_triggers 更新 |
| 来源 run 缺失 | 页面显示降级信息，不崩溃 |

## 6. 通知中心矩阵

| 渠道 | 配置 | 期望 |
|---|---|---|
| Telegram | bot token + chat id | test 可发送，payload preview masked |
| Discord | webhook url | test 可发送 |
| Slack | webhook url | test 可发送 |
| Feishu | webhook url + optional secret | secret 签名预览正确 |
| WhatsApp | phone number id + token + to | template payload 可预览 |
| Generic webhook | url + optional secret | HMAC header 可预览 |
| Email | email | SMTP 未配置时显示可读错误 |

通知事件：

| 场景 | 期望 |
|---|---|
| pending event | 可手动 process |
| failed event | 显示 last_error，可重试 |
| delivery logs | 能按 channel/event 查看 |
| disabled channel | 自动投递跳过或不发送 |
| token 已保存 | 表单显示已配置，不泄露完整值 |

## 7. 实盘配置矩阵

| 场景 | 期望 |
|---|---|
| `live_enabled=false` | 禁止创建交易实例 |
| `broker=longbridge` 无账号 | readiness issue |
| `broker=alpaca` 无 broker_account | readiness issue |
| `market_data_source=thetadata` | 配置保存，行情 badge 正确 |
| `entry_order_type=market` 且无保护 | 若要求保护则 readiness 阻断 |
| 多时段开启 | 单实例自动关闭，slot 显示 |
| 单实例关闭 | 手动 run now 阻断 |
| strategy_auto_execute=false | 多腿只分析不自动执行 |
| strategy_auto_execute=true | 多腿允许提交，但必须显示门禁 |

## 8. 交易实例矩阵

| 场景 | 期望 |
|---|---|
| queued/running | 自动轮询 |
| succeeded | 停止普通轮询，刷新列表/快照 |
| failed 无订单 | 显示失败原因 |
| failed + partial execution | 显示部分执行、orders、ledger、风险追踪 |
| strategy_no_execution | 显示门禁阻断原因 |
| strategy_partial_execution | warning tone，继续展示已成交 |
| risk_tracking_active | 开启保护轮询 |
| software stop active | 显示止损监控 |
| software take profit active | 显示止盈监控 |
| time exit active | 显示时间退出条件 |
| underlying price exit active | 显示正股价格退出条件 |
| Greeks exit active | 显示 Greeks 退出条件 |
| strategy_residual_tracking | 显示残腿追踪 |
| broker_combo_close_required | danger tone，提示人工核对 |

## 9. PnL 矩阵

| 场景 | 期望 |
|---|---|
| 券商已确认成交 | basis 显示券商成交确认 |
| 未平仓 | 浮盈亏显示 mark 估算 |
| 退出单已提交未成交 | 不计入已实现 PnL |
| 部分成交缺入场价 | 显示 warning |
| Alpaca 账户 | 不混用 Longbridge 成交 |
| Longbridge 账户 | 不混用 Alpaca 资产 |
| snapshots refresh 失败 | 显示错误但保留上次曲线 |

## 10. 危险操作矩阵

| 操作 | 确认 | 期望 |
|---|---|---|
| 全账户全平 | 输入 `全平` | 提交真实全平，刷新快照 |
| 撤实例订单 | 输入 `撤实例` | 撤当前实例已知订单 |
| 平当前实例 | 输入 `平实例` | 只平当前实例已成交合约 |
| 重置风控 | 确认/输入 `初始化风控` | 不触碰券商，刷新风控 |
| 删除实例 | 确认/输入 `删除实例` | 只删本地，不撤单不平仓 |
| 批量删除 | 确认/输入 `批量删除实例` | 只删本地选中实例 |

验收重点：

- 真实交易动作和本地动作视觉上必须分清。
- 操作成功或失败后都要刷新相关实例、列表、readiness 和 snapshots。

## 11. 移动端和布局矩阵

| 页面 | 验收 |
|---|---|
| 分析器 | 表单、结果 tabs、候选表不重叠 |
| 机会雷达 | 模板、实例卡、机会卡可读 |
| 通知中心 | 渠道表单不泄露长 token，按钮不挤压 |
| 实盘交易 | 危险操作区清晰，交易实例状态不换行错乱 |
| 实例详情 | 多腿表格可横向滚动 |
| 管理后台 | 用户表和配额在窄屏可读 |

建议视口：

- desktop：1440x900。
- laptop：1280x800。
- tablet：768x1024。
- mobile：390x844。

## 12. 回归测试建议

最低手动回归：

1. 登录普通分析用户，跑一次 ThetaData 扫描。
2. 创建一个 SPY 通用雷达实例，执行 test rules 和 run now。
3. 配置 Telegram 或 Discord 测试通知，查看投递日志。
4. 登录交易用户，打开实盘页，检查 readiness。
5. 打开一个 `TRD-...` 详情，确认 status/stage/protection 都显示。
6. 打开一个 partial execution 实例，确认不会因为 `status=failed` 隐藏订单。
7. 打开移动端视口，检查实盘详情和机会雷达。

自动化建议：

- 用 Playwright 覆盖路由加载、权限拦截、主要空态。
- 用 mock API 固定返回 partial execution、strategy residual、notification failed 等高风险状态。
- 每次前端语义重构至少跑 `npm run build`。
