# #8 实时状态推送（SSE）方案评估

状态：**评估中 / 待决策**　范围：把扫描进度与交易实例状态从前端轮询迁移到服务端推送（SSE）。

---

## 1. 为什么做 / 解决什么

当前前端全靠定时轮询拉状态（`web/src/config.js`）：

| 轮询 | 间隔 | 触发条件 |
|---|---|---|
| 扫描进度 `SCAN_POLL_INTERVAL_MS` | 5s | `activeScan.status ∈ {queued, running}` |
| 交易实例 `TRADING_POLL_INTERVAL_MS` | 6s | 交易页打开 |
| 软件风控 `PROTECTION_POLL_INTERVAL_MS` | 15s | 有持仓需保护时 |
| 机会雷达 | 无自动刷新 | 全靠手动 `refreshAll` |

问题：
- **延迟与抖动**：进度条最多滞后 5s，体验上"卡顿—跳变"而非平滑推进。
- **请求量**：每个在线用户每 5–6s 一次请求，节点多、用户多时是持续负载。
- **机会雷达零实时性**：本该最需要实时的"机会发现"反而完全靠手动刷新。

目标：进行中的扫描/交易状态由后端在**状态变更时**推送，去抖动、降请求量、即时进度。

---

## 2. 核心难点：web / worker 进程分离

这是本方案复杂度的根源，不是 SSE 端点本身。

```
浏览器 ──SSE(GET)──▶ web 进程(FastAPI)         worker 进程
                          │                        │
                          │  ◀── Redis pub/sub ──   │ 执行扫描/交易
                          │                        │ 写 scan_runs / trading_runs
                       推送给浏览器                 发布"状态变更"事件
```

- 扫描在 **worker** 执行（`scan_jobs.py` → `mark_scan_*`），SSE 端点在 **web** 进程。两者不共享内存。
- 必须经 **Redis pub/sub** 跨进程传递事件。
- 现状：`redis_runtime.py` 只有 list 队列（`rpush`/`brpop`），**没有 pub/sub**，需新增。

> 没有 Redis（裸跑 SQLite）时必须优雅降级——这种部署下 SSE 不可用，前端回退轮询。

---

## 3. 事件发布点（好消息：很集中）

### 扫描（干净，低风险）
全部状态写入集中在 `scan_store.py` 4 个函数：
- `mark_scan_running` (141)
- `mark_scan_stage` (149) — worker 已通过 `progress_callback` 持续调用
- `mark_scan_succeeded` (158)
- `mark_scan_failed` (180)

只需在这 4 处各加一行 `publish_scan_event(scan_id, {...})`。发布失败不能影响状态写入（best-effort，包 try/except）。

### 交易（敏感，需谨慎）
`trading_store.py` 仅 4 处 `UPDATE trading_runs`，但这是**实盘链路**。发布事件本身是只读旁路（不改交易逻辑），但仍需确保：发布异常绝不阻断或拖慢下单/平仓。

---

## 4. 实现方案（分阶段，强烈建议）

### 阶段 A：后端基础设施（无前端行为变化，零风险）
1. `redis_runtime.py` 新增 `redis_publish(channel, payload)` + `redis_subscribe(channel)`（生成器，带超时/重连）。
2. 新增 `scan_events.py`：`publish_scan_event()` / `iter_scan_events(scan_id)`，封装频道命名（如 `scan:events:{id}`）与 JSON 编解码。
3. 在 `scan_store.py` 4 个 `mark_scan_*` 加 best-effort 发布。
4. **此阶段前端完全不变**，轮询照常工作。可单独部署验证"事件确实在 Redis 流动"（用 `redis-cli SUBSCRIBE` 观察）。

### 阶段 B：后端 SSE 端点（新增，不动旧端点）
5. `web_api.py` 新增 `GET /api/scans/{id}/events` → `StreamingResponse(media_type="text/event-stream")`，复用 chat 已验证的 SSE 格式（`_format_trace`/keep-alive 模式，见 2766–3037）。
6. 端点订阅 Redis 频道，把事件转发给浏览器；含 keep-alive 心跳、客户端断开检测、Redis 不可用时立即结束流（前端据此回退轮询）。

### 阶段 C：前端接入（保留轮询兜底）
7. `use-scanner-controller.js`：扫描进入 `queued/running` 时优先开 `EventSource('/api/scans/{id}/events')`；收到事件即更新 `activeScan`。
8. **轮询不删除**，降级为兜底：EventSource 打开成功则把轮询间隔拉长（如 30s 对账）或暂停；EventSource 出错/不支持则回退到现有 5s 轮询。
9. 扫描结束（succeeded/failed）即关闭 EventSource。

### 阶段 D（可选，后续）：交易实例 + 机会雷达
10. 同模式扩展到交易实例状态、机会雷达。交易部分单独评估、单独部署，因触及实盘链路。

---

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 发布事件拖慢/阻断扫描或交易写入 | best-effort try/except，发布失败只记日志不抛 |
| 无 Redis 部署下不可用 | 端点检测 `redis_available()`，不可用立即结束流；前端回退轮询 |
| SSE 连接泄漏/堆积 | keep-alive 心跳 + 服务端检测 `request.is_disconnected()`；扫描结束即关闭 |
| 负载均衡器超时切断长连接 | GCP backend timeout 已设 600s（chat 流复用同配置，已验证可行） |
| 多 web 节点：用户连到 A 节点，事件发布在 worker | Redis pub/sub 是广播，任一 web 节点都能收到，天然支持多节点 |
| 前端双通道（SSE+轮询）状态打架 | 单一事实源 `activeScan`；SSE 在线时轮询降级为低频对账，幂等更新 |

---

## 6. 工作量与建议

| 阶段 | 改动面 | 风险 | 价值 |
|---|---|---|---|
| A 后端基础设施 | redis_runtime + scan_events + scan_store(4处) | 低 | 无（铺路） |
| B SSE 端点 | web_api 新增端点 | 低（新增不动旧） | 无（铺路） |
| C 前端扫描接入 | use-scanner-controller | 中 | **高**（扫描实时） |
| D 交易+雷达 | trading_store + 前端 | 中-高（实盘） | 高 |

**建议**：先做 **A+B+C（仅扫描）** 作为第一个可交付闭环——它独立、可验证、保留轮询兜底，且扫描是用户最高频盯进度的场景。验证稳定后再单独评估 D（交易/雷达）。

**测试计划**：
- 单元：`redis_publish/subscribe` 往返；`mark_scan_*` 触发发布（mock redis）。
- 集成：本地 docker 起全栈，发起扫描，用 `curl -N /api/scans/{id}/events` 观察事件流；断开 Redis 验证前端回退轮询。
- 回归：现有扫描轮询路径在 EventSource 关闭时仍工作。

---

## 7. 待决策

1. 是否按 **A+B+C 仅扫描** 起步？（推荐）
2. 交易实例（阶段 D）触及实盘链路，是否暂不纳入首期？
3. 是否需要我先只做**阶段 A**（纯后端基础设施，零行为变化）作为最小可部署验证，再继续？
