# AIOption

[![CI](https://github.com/khakhasshi/AIOption/actions/workflows/ci.yml/badge.svg)](https://github.com/khakhasshi/AIOption/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

中文 | [English](README.en.md)

AIOption 是一个面向美股期权研究者的本地优先工作台。它把自然语言分析、正股与期权链、IV/RV/GEX、策略构造、机会雷达、券商执行、软件风控和交易复盘放进同一条可检查的决策链。

项目的目标不是让模型直接给出一句“买什么”，而是帮助使用者持续回答五个问题：数据是否可信、方向与波动率假设是否一致、期权结构是否合适、订单是否可执行、最大风险是否在账户承受范围内。

> **高风险提示**：本项目处于 Alpha 阶段，仅用于研究、辅助决策和自动化工作流验证，不构成投资建议。期权可能快速损失全部本金。开源版本默认关闭券商与自动交易接口；任何实盘使用都必须由操作者自行审计、配置并承担后果。

![AIOption 产品界面](web/public/site-product-console.png)

## 核心能力

### 研究与扫描

- 自然语言描述市场观点，解析标的、方向、DTE、策略族和风险偏好。
- 支持同步分析链与 Redis 异步扫描任务，并通过 SSE 推送进度。
- 聚合日线、分时、VWAP、ORB、RVOL、EMA/MACD、Volume Profile、IV/RV、IV Rank 和 GEX。
- 期权候选包含 bid/ask、volume、OI、IV、Greeks、价差质量和可执行性评分。
- 支持单模型或“进攻、风控、反方、主持人”多角色决策；AI 不可用时保留规则化 fallback。

### 策略与风险

内置单腿、价差、信用价差、跨式、宽跨式、领式、备兑、现金担保 Put、日历、对角、穷人备兑、铁鹰和蝶式等策略族。

系统会检查策略族一致性、多腿净价、资金占用、报价新鲜度、部分成交和残腿风险。交易实例同时记录顶层状态、执行阶段、生命周期与保护状态，避免把“任务失败”错误解释成“没有订单”。

### 机会雷达与复盘

- 股票池、循环扫描实例、条件触发器和机会时间线。
- RVOL、VWAP 偏离、spread、volume、OI 等规则预筛。
- AI 精扫预算、相似状态报告复用和多渠道通知。
- 订单、成交、资金快照、退出原因、AI trace 与事后复盘。

### 数据源与券商

| 职责 | 实现 |
|---|---|
| 正股与期权研究数据 | ThetaData 优先，可使用 yfinance / Longbridge |
| Greeks | 数据源字段或本地 Black-Scholes 估算 |
| GEX / Volume Profile | 基于期权链、OI、IV 与成交结构计算 |
| 券商执行 | Longbridge、Alpaca、uSMART 适配层 |
| 状态存储 | 本地 SQLite；Docker 默认 Postgres + Redis |

行情源与下单券商是两条独立链路。订单状态、撤单和平仓结果必须以当前券商回报为准，市场数据不能替代成交事实。

## 安全默认值

新安装默认采用研究模式：

- `live_enabled=false`。
- `AI_OPTION_ENABLE_BROKER_API=false`。
- 交易调度、自动交易调度和订单监控均为 `false`。
- Docker 仅把 Web、Postgres 和 Redis 端口绑定到 `127.0.0.1`。
- `.env`、数据库、账户缓存、报告、下载数据和密钥文件均被 Git 忽略。

启用实盘前至少需要完成：身份认证、模拟账户验证、订单幂等检查、最大亏损设置、部分成交处置、紧急全平演练和独立券商对账。详见 [实盘风险与安全规范](docs/trading-risk-and-safety-spec.md)。

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

打开 `http://127.0.0.1:7001`。默认可以进行本地研究，但券商和交易 API 不可用。

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

## 配置

所有敏感配置只放在本地 `.env` 或秘密管理服务中。完整模板见 [.env.example](.env.example)。

### AI Provider

AI 是可选能力。默认适配 DeepSeek，也支持 OpenAI-compatible provider：

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
- [开源准备清单](docs/OPEN_SOURCE_READINESS.md)

## 贡献

欢迎提交 issue 和 pull request。涉及交易执行、风控或数据语义的改动，请同时提供失败场景、测试证据与可回滚说明。提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 作者与许可证

Jiang Jingzhe（江景哲），Email: jiangjingzhe2004@gmail.com

项目采用 [Apache License 2.0](LICENSE)。第三方数据和 SDK 仍受各自供应商条款约束。
