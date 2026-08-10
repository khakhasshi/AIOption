# AIOption

[![CI](https://github.com/khakhasshi/AIOption/actions/workflows/ci.yml/badge.svg)](https://github.com/khakhasshi/AIOption/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-blue.svg)](LICENSE)

中文 | [English](README.en.md)

**AIOption 是一个本地优先、证据驱动的美股期权研究与交易工作台。**

它把自然语言扫描、正股与期权链、IV/RV/GEX、策略构造、AI 交叉审查、券商执行、软件风控和交易复盘放进同一条可检查的决策链。系统关注的不是让模型给出一句“买什么”，而是让每次判断都能回答：**用了什么数据、为什么选择这个结构、最大风险是多少、什么条件会推翻结论。**

> **高风险提示**：本项目处于 Alpha 阶段，仅用于研究、辅助决策和自动化工作流验证，不构成投资建议。期权可能快速损失全部本金。公开源码版本默认关闭券商与自动交易接口；任何实盘使用都必须由操作者自行审计、配置并承担后果。

## 从一句话到可复核决策

AIOption 的核心工作流由三步组成：

1. **提出意图**：用自然语言说明标的、市场观点、期限或策略偏好。
2. **冻结证据**：保留行情时间、期权链、Greeks、IV、OI、GEX、工具调用和原始表格。
3. **形成判断**：同时列出支持证据、反对证据、关键价位、失效条件、结构风险和执行门禁。

### 1. 智能扫描器：把模糊观点变成候选结构

![AIOption 智能期权扫描器](docs/assets/readme/scanner-console.png)

截图中的请求是：扫描 NVDA 的日线、分时和相关新闻，并寻找具有高额盈亏潜力的单腿期权。系统没有只返回一个方向标签，而是同时展示：

- 标的现价 `223.96`、技术偏向 `bullish`、ThetaData 行情和正 GEX 环境。
- 候选结构为 Put 日历价差，净支出与最大亏损约 `122 美元`，平衡点 `220`，综合评分 `85.65`。
- 结构适配、盈亏质量、执行复杂度、资金效率、风险定义和报价一致性等分项评分。
- 每条腿的方向、合约、行权价、到期日和报价，以及止盈、止损和期限结构风险。
- 多角色 AI 的分歧、模板与用户意图不一致等警告。

这个示例的最终状态仍是“仅观察”：候选结构得分高，不代表决策门禁已经通过。系统把**反对理由和不执行结果**也作为正式输出。

#### 区间策略示例：四腿铁鹰与定义风险

![AIOption INTC 铁鹰策略扫描结果](docs/assets/readme/scanner-iron-condor.png)

同一个扫描器也可以构造并检查多腿区间策略。截图中 INTC 现价为 `101.65`，技术偏向为 `bullish`，ThetaData 行情与正 GEX 环境支持震荡区间假设。系统给出的铁鹰结构到期日为 `2026-08-14`，并逐腿列出两个 Put 和两个 Call 合约。

- 组合每股净收入约 `0.60 美元`（每组约 `60 美元`），最大盈利 `60 美元`，最大亏损 `40 美元`。
- 双侧盈亏平衡点为 `100.6 / 102.4`，盈亏比约 `1.5:1`。
- 综合评分 `93.92`，结构适配 `91`、盈亏质量 `93.4`、风险定义 `92`、资金效率 `100`。
- 系统同时提示 IV 相对 RV 偏低、环境处于混乱状态，以及 AI 最终策略与当前模板不一致。

因此，即使候选结构具有定义明确的最大亏损和较高评分，最终状态仍是“等待触发/仅观察”。这说明评分用于比较候选，而环境门控、一致性检查和入场条件负责决定是否允许继续执行。

### 2. AI 对话：先看原始数据，再看结论

![AIOption AI 对话中的原始期权证据](docs/assets/readme/chat-evidence.png)

AI 对话不是脱离行情的通用聊天窗口。针对“分析 NVDA 最近走势并给出多空判断”的问题，工具链先冻结并呈现 ThetaData 证据：

- 数据日期为上一交易日 EOD `2026-08-07`，分析到期日为 `2026-08-10`。
- ATM 行权价 `225`，ATM 区总持仓 `91,165`，Put/Call OI 比 `0.26`。
- ATM Call IV `23.5%`、Put IV `24.6%`，并展示权利金、Delta、Gamma、Theta 和盈亏平衡点。
- IV Skew 与 Call/Put OI 按行权价展开，保留模型分析前的原始截面。

工具调用、数据日期和原始表格始终可见，用户可以判断模型引用的是实时数据、上一交易日 EOD，还是估算字段，而不是只接受一段无法追溯的答案。

### 3. 多空判断：把不确定性和失效条件写出来

![AIOption AI 对话中的多空分析](docs/assets/readme/chat-analysis.png)

完成取数后，系统把同一截面拆成多头逻辑、空头逻辑、中性区间与操作框架：

- 多头证据包括价格接近阶段高点、正 Gamma 区、`225` Call OI 集中。
- 空头证据包括 PCR 仅 `0.26`、Put IV 近端抬升和到期日 Gamma 陷阱。
- 关键阻力为 `225`，关键支撑依次为 `217.5` 与 Gamma Flip 附近的 `210`。
- 短线结论偏多，但必须等待放量突破；中期维持中性偏谨慎。
- 当 GEX 或外部行情工具获取失败时，答案明确标注数据缺口，并要求开盘后重新确认。

这正是 AIOption 与“AI 喊单器”的区别：**方向判断必须附带证据、反证和失效条件；数据不完整时，正确答案可以是不交易。**

## 能力全景

| 决策阶段 | AIOption 提供的能力 |
|---|---|
| 意图解析 | 从自然语言提取标的、方向、DTE、策略族和风险偏好 |
| 市场证据 | 日线、分时、VWAP、ORB、RVOL、EMA/MACD、Volume Profile、新闻 |
| 期权证据 | 期权链、bid/ask、volume、OI、IV、Greeks、Skew、期限结构、GEX |
| 策略构造 | 单腿、垂直价差、信用价差、跨式、宽跨式、领式、备兑、现金担保 Put、日历、对角、穷人备兑、铁鹰和蝶式 |
| 风险门禁 | 多腿净价、资金占用、最大损失、报价新鲜度、部分成交与残腿风险 |
| AI 审查 | 单模型分析或“进攻、风控、反方、主持人”多角色交叉审查 |
| 自动化 | 股票池、循环扫描、Wait Trigger、机会雷达、通知和交易实例 |
| 可审计性 | 工具 trace、原始数据、订单/成交、资金快照、退出原因和事后复盘 |

### 数据源与券商边界

| 职责 | 实现 |
|---|---|
| 正股与期权研究数据 | ThetaData 优先，可使用 yfinance / Longbridge |
| Greeks | 数据源字段或本地 Black-Scholes 估算 |
| GEX / Volume Profile | 基于期权链、OI、IV 与成交结构计算 |
| AI Provider | DeepSeek 默认，也支持 OpenAI-compatible provider |
| 券商执行 | Longbridge、Alpaca、uSMART 适配层 |
| 状态存储 | 本地 SQLite；Docker 默认 Postgres + Redis |

行情源与下单券商是两条独立链路。订单状态、撤单和平仓结果必须以当前券商回报为准，市场数据不能替代成交事实。

## 快速开始

### Docker Compose

需要 Docker Engine 24+ 与 Compose v2：

```bash
git clone https://github.com/khakhasshi/AIOption.git
cd AIOption
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:7001/api/health
```

打开 [http://127.0.0.1:7001](http://127.0.0.1:7001)。默认可以进行本地研究，但券商和交易 API 不可用。

本地演示管理员（来自 `.env.example`）：

```text
账号：local-admin
密码：AIOption-Local-Admin-2026!
```

该账号拥有分析、交易页面、管理员和不限额权限，便于完整体验与截图；券商 API、交易调度、自动交易和订单监控的服务器总开关仍默认关闭。此公开密码**只能用于绑定 `127.0.0.1` 的本地实例**。部署到局域网或公网前，必须更换账号、密码、`AI_OPTION_AUTH_SECRET` 与 `AI_OPTION_CREDENTIAL_SECRET`。

### 本地开发

需要 Python 3.12 和 Node.js 22：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env

cd web
npm ci
npm run build
cd ..

PYTHONPATH=. .venv/bin/python -m uvicorn ai_option_scanner.web_api:app \
  --host 127.0.0.1 --port 7001
```

CLI 示例：

```bash
PYTHONPATH=. .venv/bin/python -m ai_option_scanner.cli --no-ai "扫描 QQQ 期权结构"
PYTHONPATH=. .venv/bin/python -m ai_option_scanner.cli --council "分析 NVDA 趋势价差机会"
```

## 配置数据源与模型

所有敏感配置只放在本地 `.env`、后台加密凭证存储或秘密管理服务中。完整模板见 [.env.example](.env.example)。

### DeepSeek / AI Provider

AI 是可选能力。默认配置为 DeepSeek V4 Flash，并显式关闭思考模式；也可以配置其他 OpenAI-compatible provider。

```bash
DEEPSEEK_API_KEY=your-key
DEEPSEEK_MODEL=deepseek-v4-flash
```

### ThetaData

管理员登录后可在“账户与连接 → ThetaData 数据源”保存、更新、删除并测试凭证。凭证由后端加密保存，前端只显示脱敏邮箱；扫描器、AI 对话、实例和复盘会复用同一份服务器级配置。

生产部署也可以使用环境变量或 credentials file，且它们的优先级高于前端保存的配置：

```bash
THETADATA_EMAIL=your-email
THETADATA_PASSWORD=your-password
# 或 THETADATA_CREDENTIALS_FILE=/run/secrets/thetadata-credentials
```

订阅等级决定可用端点与历史深度。项目会在缺少完整 Greeks 时进行本地估算，但估算值必须与供应商原始字段区分。ThetaData Cloud 通常限制同一账号的活动会话数量，请勿同时启动多个使用相同凭证的独立部署。

### Longbridge

可以通过 UI 按账户保存凭证，也可以使用环境变量：

```bash
AI_OPTION_LONGBRIDGE_BACKEND=sdk
LONGBRIDGE_APP_KEY=your-app-key
LONGBRIDGE_APP_SECRET=your-app-secret
LONGBRIDGE_ACCESS_TOKEN=your-access-token
```

### 登录与 OAuth

复制 `.env.example` 后可使用上方本地演示管理员。部署到局域网或公网前必须替换该公开账号，设置长随机 `AI_OPTION_AUTH_SECRET` 和 `AI_OPTION_CREDENTIAL_SECRET`，并优先使用密码哈希而不是明文密码。Google、Apple OAuth 与 Turnstile 配置见 [OAuth 文档](docs/oauth-login-setup.md)。

生成密码哈希：

```bash
PYTHONPATH=. python - <<'PY'
from ai_option_scanner.app_auth import hash_password
print(hash_password("replace-this-password"))
PY
```

## 安全默认值

新安装默认采用研究模式：

- `live_enabled=false`。
- `AI_OPTION_ENABLE_BROKER_API=false`。
- 交易调度、自动交易调度和订单监控均为 `false`。
- Docker 仅把 Web、Postgres 和 Redis 端口绑定到 `127.0.0.1`。
- `.env`、数据库、账户缓存、报告、下载数据和密钥文件均被 Git 忽略。

启用实盘前至少需要完成：身份认证、模拟账户验证、订单幂等检查、最大亏损设置、部分成交处置、紧急全平演练和独立券商对账。详见 [实盘风险与安全规范](docs/trading-risk-and-safety-spec.md)。

### 显式启用交易

先使用模拟账户。以下开关不会替代 UI 中的 `live_enabled`、用户 `can_trade` 权限和策略风控门禁：

```bash
AI_OPTION_ENABLE_BROKER_API=true
AI_OPTION_ENABLE_TRADING_SCHEDULER=true
AI_OPTION_ENABLE_AUTO_TRADE_SCHEDULER=true
AI_OPTION_ENABLE_ORDER_MONITOR=true
```

不要在没有订单监控的情况下启动自动交易，也不要在多个 Worker 上重复启动同一个调度器。

## 架构

```text
React SPA
   |
   | HTTP / Cookie Session / SSE
   v
FastAPI Web API
   |-----------------------------|
   v                             v
Postgres or SQLite          Redis queue/locks/cache
   |                             |
   v                             v
Domain services              Worker process
   |                             |
   | market data / AI            | schedulers / monitors
   v                             v
ThetaData / yfinance / Longbridge / brokers
```

Docker 推荐把 Web 与 Worker 分离：Web 只服务 API 和前端，Worker 消费扫描任务并运行已显式开启的后台任务。详细状态机、数据模型和接口见 [docs](docs/)。

## 仓库边界

本仓库只包含 AIOption 主应用。以下项目保持独立，不作为 Git 子模块或运行时依赖：

- `OptionWorkstation`：期权实时分析与历史回放工作台。
- `aioption-rule-trader`：无 LLM 规则交易实验。
- `aioption-trading-backtester`：历史仿真与参数研究。

本仓库不包含市场数据、生产数据库、真实订单记录、账户缓存、私有报告或供应商凭证。

## 开发与验证

```bash
# 后端
PYTHONPATH=. .venv/bin/python -m pytest

# API 文档覆盖
PYTHONPATH=. .venv/bin/python scripts/check_api_docs.py

# 仓库卫生与秘密模式检查
PYTHONPATH=. .venv/bin/python scripts/check_repository_hygiene.py

# 前端
cd web && npm ci && npm run build

# Docker
docker compose config --quiet
docker build -t aioption:local .
```

历史下载与自适应入场研究脚本需要额外依赖：

```bash
.venv/bin/pip install -r requirements-research.txt
```

## 文档

- [后端 API](docs/backend-api-reference.md)
- [数据模型](docs/data-model-and-schema.md)
- [市场数据 API](docs/market-data-api-reference.md)
- [前端页面与功能](docs/frontend-route-feature-map.md)
- [实时状态设计](docs/sse-realtime-status-design.md)
- [部署手册](docs/deployment-runbook.md)
- [安全政策](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)
- [公开发布准备清单](docs/OPEN_SOURCE_READINESS.md)

## 贡献

欢迎提交 issue 和 pull request。涉及交易执行、风控或数据语义的改动，请同时提供失败场景、测试证据与可回滚说明。提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 作者与许可证

Jiang Jingzhe（江景哲），Email: jiangjingzhe2004@gmail.com

当前版本采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，仅允许许可证定义的非商业用途。商业使用需要另行取得书面授权，请联系 jiangjingzhe2004@gmail.com。

此前已经按 Apache License 2.0 发布的历史版本继续受原授权约束，当前变更不会追溯撤销已授予的许可。详见 [许可说明](LICENSING.md)。第三方数据和 SDK 仍受各自供应商条款约束。
