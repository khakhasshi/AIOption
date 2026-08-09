# Deployment Runbook

本文档覆盖公开仓库可复现的单机 Docker 部署。云厂商账号、主机清单、SSH key、生产数据库和私有同步脚本不属于本仓库。

## 1. 运行模型

| 服务 | 职责 | 默认网络 |
|---|---|---|
| `app` | FastAPI API 与 React 静态资源 | `127.0.0.1:7001` |
| `worker` | 扫描队列与显式开启的后台任务 | 无公开端口 |
| `db` | Postgres 状态 | `127.0.0.1:54321` |
| `redis` | 队列、锁、缓存和事件 | `127.0.0.1:63790` |

交易相关后台任务默认关闭。研究扫描仍可由 Worker 消费。

## 2. 部署前检查

```bash
PYTHONPATH=. python scripts/check_repository_hygiene.py
PYTHONPATH=. pytest
cd web && npm ci && npm run build && cd ..
docker compose config --quiet
docker build -t aioption:verify .
```

确认 `.env` 未进入 Git，且没有把真实凭证作为 Docker build argument。部署到非本机网络前必须配置认证、TLS、反向代理访问控制和备份策略。

## 3. 启动

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:7001/api/health
```

查看日志：

```bash
docker compose logs -f app worker
```

健康响应为 `degraded` 时，检查 Postgres、Redis 和 Worker 日志，不要只根据容器 `running` 判断系统可用。

## 4. 反向代理

推荐由 Caddy、Nginx 或云负载均衡器终止 TLS，并只把流量转发到 `127.0.0.1:7001`。设置实际域名：

```bash
AI_OPTION_CORS_ORIGINS=https://your-domain.example
```

反向代理应保留 `X-Forwarded-Proto`，对 SSE 路由关闭响应缓冲，并为 `/sw.js` 保留应用返回的 `no-store` 缓存头。

## 5. 更新与回滚

更新前记录当前 commit、镜像标签和数据库备份：

```bash
git rev-parse HEAD
docker compose exec -T db pg_dump -U ai_option ai_option > aioption-backup.sql
docker compose build app worker
docker compose up -d app worker
curl http://127.0.0.1:7001/api/health
```

备份文件包含用户、账户元数据和交易记录，不得提交到 Git。回滚时切回已验证 commit 或镜像，并在确认 schema 兼容后恢复服务；不要在不理解迁移方向时直接恢复旧数据库。

## 6. 启用模拟交易

先完成模拟账户凭证、用户 `can_trade` 权限和 UI 风控配置，再显式设置：

```bash
AI_OPTION_ENABLE_BROKER_API=true
AI_OPTION_ENABLE_TRADING_SCHEDULER=true
AI_OPTION_ENABLE_AUTO_TRADE_SCHEDULER=true
AI_OPTION_ENABLE_ORDER_MONITOR=true
```

重启后检查 `/api/admin/server-health`，确认只有一个预期 Worker 持有交易调度职责。真实资金部署还需要独立的安全、合规与灾难恢复评审。

## 7. 事故处理

1. 暂停交易和自动交易实例。
2. 在私有运行环境关闭交易调度开关并重启 Worker。
3. 直接在券商端核对真实订单、成交与持仓。
4. 对部分成交、残腿或未保护仓位进行人工处理。
5. 保存脱敏日志、时间线和 commit ID，再进行根因分析。

应用数据库不是券商事实的替代品。网络错误、超时或任务失败都不能证明订单未提交。
