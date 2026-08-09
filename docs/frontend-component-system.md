# Frontend Component System

本文档定义前端重构后的组件语义边界。目标是减少重复 UI、重复 formatter 和重复状态判断，让所有页面共享同一套业务组件。

## 1. 分层原则

```text
Route Page
  -> Page Controller Hook
    -> Domain Components
      -> UI Primitives
```

- Route Page：只负责路由级布局和权限判断。
- Controller Hook：负责 API 调用、刷新、提交、轮询和本页面状态。
- Domain Component：负责展示业务对象，如交易实例、机会、通知渠道。
- UI Primitive：按钮、badge、表格、tabs、modal、空状态等无业务组件。

禁止：

- 在 UI primitive 里调用 API。
- 在多个页面里重复拼同一种 status badge。
- 在组件里直接写散落的状态中文。
- 在展示组件里处理危险交易动作确认。

## 2. UI Primitives

| 组件 | 职责 |
|---|---|
| `Button` | 统一按钮尺寸、tone、loading、disabled |
| `IconButton` | 图标按钮，必须有 tooltip/aria label |
| `StatusBadge` | 状态 badge，接收 `tone` 和 label |
| `DataBadge` | 数据源、券商、模型等短标签 |
| `MetricValue` | 数值展示，支持 unit、loading、warning |
| `MoneyValue` | 金额展示，支持 basis 和 warning |
| `PercentValue` | 百分比展示 |
| `TimeValue` | 美东时间展示 |
| `EmptyState` | 空列表/空结果 |
| `ErrorState` | 错误和重试 |
| `LoadingState` | 加载状态 |
| `ConfirmActionButton` | 需要确认文本的危险动作 |
| `Tabs` | 固定高度 tab，不触发布局跳动 |
| `Table` | 标准表格密度、空态和横向滚动 |
| `Drawer` | 侧边详情 |
| `Modal` | 确认和编辑 |

## 3. Domain Formatters

建议新建或收敛到：

```text
web/src/domain/
├── glossary.js
├── status.js
├── strategy.js
├── money.js
├── time.js
└── source.js
```

必须统一的 formatter：

| Formatter | 输入 | 输出 |
|---|---|---|
| `formatMarketDataSource` | `thetadata/yfinance/longbridge/auto` | 显示名 |
| `formatBroker` | `longbridge/alpaca` | 显示名 |
| `formatStrategyFamily` | strategy mode | 中文策略名 |
| `formatRunStatus` | `status` | label/tone |
| `formatLifecycleState` | `lifecycle_state` | label/tone |
| `formatProtectionState` | protection | label/tone |
| `formatOrderStatus` | order status | label/tone |
| `formatOpportunityStatus` | status | label/tone |
| `formatNotificationStatus` | status | label/tone |
| `formatPnlBasis` | basis | 中文说明 |

## 4. Scanner Components

| 组件 | 职责 |
|---|---|
| `ScannerWorkspace` | 分析器主布局 |
| `ScanRequestPanel` | query、symbol、provider、行情源、策略模式 |
| `AnalysisModuleSelector` | 分析模块开关 |
| `StrategyModeSelector` | 策略模式选择 |
| `MarketDataSourceSelector` | 行情源选择 |
| `ScanHistoryPanel` | 历史、搜索、星标、标签 |
| `ScanResultWorkspace` | tabs 容器 |
| `ScanAnswerPanel` | AI/fallback 正文 |
| `CandidateTable` | 单腿候选池 |
| `StrategyCandidateTable` | 多腿候选池 |
| `DecisionTracePanel` | tool plan、decision gate、AI validator |
| `GexStructurePanel` | GEX、墙位、Volume Profile |
| `TriggerCreateMenu` | 从扫描创建 trigger |

组件边界：

- `ScanRequestPanel` 不读取 scan result。
- `CandidateTable` 不负责排序状态存储，只接收排序字段和回调。
- `DecisionTracePanel` 不隐藏 blockers/warnings。

## 5. Radar Components

| 组件 | 职责 |
|---|---|
| `WatchlistManager` | 股票池 CRUD |
| `ScanLoopInstanceList` | 循环实例列表 |
| `ScanLoopInstanceEditor` | 创建/编辑实例 |
| `RuleConditionEditor` | prefilter/alert rule 编辑 |
| `RadarPresetPicker` | SPY 通用、信用价差等模板 |
| `RadarRunSummary` | 单次运行 summary |
| `RadarRunItemCard` | 单标的 snapshot + recommendation |
| `ScenarioBaselineBlock` | 基准/次情形/偏强/真弱 |
| `AiReportCacheBadge` | AI 新写/复用/抑制 |
| `OpportunityBoard` | 机会列表 |
| `OpportunityTimeline` | 机会事件 |
| `RadarOperationsPanel` | due cycle、health、手动处理 |

规则：

- SPY 模板和信用价差模板不应硬编码在页面组件里。
- `ScenarioBaselineBlock` 必须能展示“未接入 HIRO/dealer inventory”。
- 每轮雷达消息块要复用同一组件生成前端预览和通知预览。

## 6. Notification Components

| 组件 | 职责 |
|---|---|
| `NotificationCenterWorkspace` | 通知中心布局 |
| `NotificationChannelCard` | 渠道展示 |
| `NotificationChannelForm` | 渠道创建/编辑 |
| `ProviderConfigFields` | Telegram/Discord/Feishu 等差异字段 |
| `PayloadPreviewPanel` | 实际 payload 预览 |
| `NotificationEventTable` | 事件列表 |
| `DeliveryLogTable` | 投递日志 |
| `NotificationGuideLink` | 绑定教程跳转 |

敏感字段规则：

- 任何组件不得显示完整 token。
- 输入框里已有 secret 时显示“已配置”，placeholder 提示留空保持原值。
- payload preview 中 Authorization 只能显示 masked。

## 7. Trading Components

| 组件 | 职责 |
|---|---|
| `TradingWorkspace` | 实盘主布局 |
| `TradingConfigPanel` | 基础配置 |
| `BrokerAccountSelector` | Longbridge/Alpaca 账号选择 |
| `TradingSchedulePanel` | 单实例/多时段 slot |
| `RiskConfigPanel` | 止盈止损和风险上限 |
| `ReadinessPanel` | readiness issues/warnings |
| `TradingRunList` | 交易实例列表 |
| `TradingRunStatusHeader` | status/stage/lifecycle/protection |
| `TradingRunDetail` | 实例详情 tabs |
| `SelectionTable` | AI 选择 |
| `OrderLedgerTable` | orders + strategy ledger |
| `StrategyLegTable` | 多腿结构 |
| `RiskPlanPanel` | 止盈止损/智能退出 |
| `ProtectionStatusPanel` | 保护状态 |
| `TradingTimeline` | event timeline |
| `TradingReviewPanel` | 复盘指标 |
| `TradingDangerZone` | 全平/撤单/平实例/删除 |
| `SnapshotsPanel` | 资金曲线和账户口径 |
| `AiQualityPanel` | AI 质量统计 |

强制规则：

- `TradingRunStatusHeader` 必须组合展示 `status`、`stage`、`lifecycle_state`、`protection_state`。
- `OrderLedgerTable` 必须能展示 `status=failed` 但有订单的情况。
- `TradingDangerZone` 内的真实交易动作必须用确认文本，不用普通 confirm 替代。
- `MoneyValue` 展示 PnL 时必须能显示 basis/warnings。

## 8. Admin Components

| 组件 | 职责 |
|---|---|
| `AdminWorkspace` | 管理后台布局 |
| `UserAdminPanel` | 用户 CRUD |
| `UserQuotaEditor` | max_* 配额 |
| `UserUsageTable` | usage |
| `BetaLotteryAdminPanel` | 抽签后台 |
| `ServerHealthPanel` | 服务健康 |
| `AiUsageDashboard` | AI 用量和成本 |

规则：

- `editable=false` 的用户禁用修改/删除按钮。
- 配额 `-1` 显示“不限额”。
- AI 成本显示估算标签。

## 9. Shared Domain Components

这些组件应跨页面复用：

| 组件 | 用途 |
|---|---|
| `CopyableId` | `SCN-`、`TRD-`、`OPP-` 等 ID |
| `DataSourceBadge` | 行情源 |
| `BrokerBadge` | 券商 |
| `AiProviderBadge` | 模型 |
| `StrategyFamilyBadge` | 策略族 |
| `OptionContractCode` | 期权合约代码 |
| `OptionLegTable` | 多腿结构 |
| `RiskPlanPanel` | 风险计划 |
| `EventTimeline` | 扫描、机会、交易通用事件 |
| `RawJsonPanel` | 原始 JSON |

## 10. CSS 和布局约束

- 工具型界面优先高密度、可扫描，不做营销 hero。
- 卡片只用于重复项目、弹窗和明确框定的工具，不把页面 section 包成层层卡片。
- 状态 badge、按钮、表格行高必须稳定，动态文本不能造成布局跳动。
- 移动端优先保持信息顺序，不隐藏关键风险字段。
- 交易危险操作必须视觉上独立，但不能用大面积警告色淹没正常信息。
