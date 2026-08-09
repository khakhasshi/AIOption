# uSMART（盈立证券）Open API 接入适配方案

> 目标：对照现有 Longbridge / Alpaca broker 适配层，新增 uSMART（盈立证券）券商通道，覆盖鉴权、下单、撤改单、持仓/资产/订单查询等 Longbridge 现有全部能力。
>
> 文档来源：https://api-doc.usmart8.com/zh-cn/ （trade.html / quote-base.html）。调研日期 2026-06-15。

## 0. 关键前置事实（决定整体方案）

uSMART API 与现有两家券商**都不同**，是「无状态 RSA 签名」+「有状态登录 token」的混合模型：

| 维度 | Longbridge | Alpaca | **uSMART** |
|---|---|---|---|
| 鉴权 | Python SDK（app_key/secret/token） | 静态 API key/secret，每请求带 header | **每请求 RSA 签名 + 登录 token** |
| 凭证存储 | `account_store`（home_dir + SDK 凭证） | `broker_store`（加密 key/secret） | `broker_store` + **RSA 私钥 + token 缓存** |
| 下单交互 | SDK 方法调用 | REST `POST /v2/orders` | REST `POST /stock-order-server/...` |
| 协议 | gRPC（SDK 封装） | REST/JSON | REST/JSON |
| 期权 | 支持（`.US` 符号） | 支持（OCC 符号） | 支持（OCC 风格 `AAPL250815C90000`，`exchangeType:51`） |

### 0.1 鉴权细节（最复杂的部分）

- **基础 URL**：交易 `https://open-jy.yxzq.com`（UAT `http://open-jy-uat.yxzq.com`）；行情 `https://open-hz.yxzq.com:8443`。服务按模块分路径：`/user-server/`、`/stock-order-server/`、`/asset-center-server/`、行情 `/quotes-openservice/api/v1/`。
- **签名**：`MD5withRSA` 对 body 加密 → URL-safe Base64（RFC4648）→ 放入 `X-Sign` header。**每个 channel 一对密钥**。
- **两对 RSA 密钥**：一对用于 X-Sign 验签，另一对用于加密敏感字段（password、phoneNumber）——注意是「不同密钥」。
- **必需 headers**：`Authorization`（登录 token）、`X-Lang`、`X-Request-Id`（雪花算法唯一 ID，幂等用）、`X-Channel`、`X-Time`、`X-Dt`、`X-Type`、`X-Sign`、`Content-Type: application/json; charset=utf-8`。
- **登录流程（多步、有状态）**：
  1. `POST /user-server/open-api/login`（password 登录，password/phoneNumber 需 RSA 加密）→ 返回 `token`、`uuid`、`expiration`。
  2. `POST /user-server/open-api/trade-login`（交易解锁）+ `get-trade-status`（0 锁定 / 1 解锁）。
  3. token 有过期时间，需缓存 + 自动重登。
- ⚠️ **阻塞项**：RSA 公钥「需双方商定」（`需双方商定`），且 API「仅对已开户且在盈立有资产的客户开放」。**没有 channel + 密钥对就无法联调**。

### 0.2 响应信封

统一 `{ "code", "data", "msg" }`，`code: 0` 成功。行情错误码 `806xxx`。

---

## 1. 现有适配层结构（要对照/扩展的文件）

```
broker_client.py      # 统一分发层：按 broker 字符串 dispatch 到 alpaca/longbridge
broker_store.py       # broker_accounts 表 + 凭证加解密 + normalize_broker()
alpaca_client.py      # REST 券商参考模板（最接近 uSMART）
longbridge_client.py  # SDK 券商
option_symbol_utils.py# OCC ↔ Longbridge 符号互转
trading_agent.py      # 调用 account_ref_for_config / submit / check
web_api.py            # /api/brokers/accounts CRUD 端点
```

**核心分发点**（`broker_client.py`）目前是二元 `if broker == "alpaca": ... else longbridge`。每个函数都要加第三分支：
`check / assets / positions / today_orders / cancel_order / submit_buy_order / submit_sell_order / submit_market_order / submit_stop_sell_order / order_detail / wait_for_order_fill / _order_symbol_for_broker`。

---

## 2. 实施方案

### 阶段 A — 凭证存储与鉴权（基础）

**A1. `broker_store.normalize_broker`**（line 242）：白名单加入 `"usmart"`：
```python
return text if text in {"longbridge", "alpaca", "usmart"} else "longbridge"
```

**A2. uSMART 凭证模型**。`broker_accounts` 表现有 `api_key_enc`/`api_secret_enc` 两个加密槽不够用，uSMART 需要：channel ID、RSA 签名私钥、RSA 加密公钥、登录手机号/区号、交易密码。方案：
- 复用 `api_key_enc` = channel ID，`api_secret_enc` = RSA 签名私钥（PEM）。
- 新增一个加密 JSON 槽 `extra_enc`（`ensure_column`）存其余字段（加密公钥、phone、areaCode、交易密码）。保持与现有迁移模式一致。

**A3. 新建 `usmart_client.py`**（参考 `alpaca_client.py` 的 `_request` 模板），实现：
- `_sign(body) -> str`：`MD5withRSA`（用 `cryptography` 库，`padding.PKCS1v15` + `hashes.MD5`）→ urlsafe_b64。
- `_headers(account, body)`：组装 9 个必需 header，`X-Request-Id` 用雪花/UUID，`X-Time` 当前毫秒。
- `_login(account) -> token`：RSA 加密 password/phone → `POST /login` → 缓存 token（带 `expiration`，线程安全，仿 `longbridge_auth._session_cache`）+ `trade-login` 解锁。
- `_request(account, path, body)`：自动注入 token，token 失效（code 表示鉴权失败）→ 重登一次重试。
- `check(account)`：登录 + `get-trade-status` → 返回 `{"broker":"usmart","status":...}`。

**依赖**：`requirements.txt` 加 `cryptography`（若未在）。RSA 签名不要手搓，用 `cryptography`。

### 阶段 B — 交易与查询（核心能力）

在 `usmart_client.py` 内实现，端点对照表：

| 统一接口 | uSMART 端点 | 关键字段映射 |
|---|---|---|
| `submit_order` | `POST /stock-order-server/open-api/entrust-order` | `entrustType` 0买/1卖；`entrustProp` US:`0`限价/`w`市价；`exchangeType` 5=US,51=期权；`entrustAmount`/`entrustPrice`；`serialNo`=雪花（幂等）|
| `cancel_order` | `POST /modify-order` | `actionType:0`，`entrustId`，amount/price=0 |
| `modify_order` | `POST /modify-order` | `actionType:1` |
| `order_detail` | `POST /order-detail` | `serialNo` 或 `entrustId` → fee 明细 |
| `today_orders` | `POST /today-entrust` | `exchangeType:100`=全部 |
| `positions` + `assets` | `POST /asset-center-server/open-api/open-assetQuery/v1` | `moneyType` 1=USD；`holdInfos[]` → 持仓；总额字段 → assets |
| `wait_for_order_fill` | 轮询 `order-detail` / `stock-record` | `businessAmount` 成交量、`orderStatus` |
| 可买量 | `POST /trade-quantity` | sizing 用（`buyEnableAmount`/`cashPurchasingPower`）|

**B1. 符号转换**。`option_symbol_utils` 加 `option_symbol_for_usmart`。uSMART 用 OCC 风格（`AAPL250815C90000`，无 `.US` 后缀），与 Alpaca 接近——大概率可直接复用 `option_symbol_for_occ`，但去掉/调整分隔。`exchangeType` 期权用 `51`，正股用 `5`。`_order_symbol_for_broker` 加 usmart 分支。

**B2. 幂等**。uSMART 原生支持 `serialNo`（int64 雪花，唯一）+ `X-Request-Id`。把现有 `[cok:KEY]` 幂等 key 映射到 `serialNo`（需 int64，所以对 cok 做 hash→int64，而非直接用字符串）。这比 Longbridge（无原生幂等、靠 journal）更好。

**B3. 订单状态归一**。uSMART `status`/`statusName` + `businessAmount`（已成交）→ 映射到现有 `_normalize_order` 的 `order_id`/`executed_quantity`/`filled_quantity`/`executed_price`。注意 `_status_indicates_filled` 的 token 匹配规则（见 trading-hardening memory）——uSMART 的中文/数字状态要正确映射成「filled」语义，**不要用子串匹配**。

**B4. 止损单**。uSMART 文档**未见原生 stop 单**（只有限价/市价 + auction）。`submit_stop_sell_order` 在 uSMART 上**只能软件止损兜底**（与 Alpaca paper 的 `broker_stop_unsupported` 路径一致，`trading_agent.py:4706` `_arm_software_stop`）。需在 `check()` 返回里标注 `supports_native_stop: False`。

### 阶段 C — 分发层接线

**C1. `broker_client.py`**：12 个函数各加 `elif broker == "usmart": return usmart_client.xxx(...)`。`_resolve` 加 usmart 分支（用 `resolve_broker_account` 像 alpaca 一样）。`import usmart_client`。

**C2. `trading_agent.py`**：`account_ref_for_config`（broker_client line 25）加 usmart 分支（走 `resolve_broker_account`）。`_validate_*`（line 5081）加 usmart 的 `broker_account` 必填校验。

**C3. 行情通道分离**。按 trading-risk-and-safety-spec「行情源和券商通道必须分离」原则：uSMART 行情（`/quotes-openservice/`）**可选**接入，但下单/持仓/成交回报必须来自 uSMART 交易通道。`quote()`/`quote_option_contract` 现在写死 longbridge——若 uSMART 账户无 Longbridge 配套，需要 uSMART 行情兜底（阶段 D，非阻塞核心下单）。

### 阶段 D — 前端与配置

**D1. `web_api.py`** broker 端点已是通用的（`/api/brokers/accounts` 走 `create_broker_account`）。但 `create_broker_account` 只收 `api_key`/`api_secret` 两个参数——uSMART 多字段需扩展请求模型（channel/RSA私钥/phone/交易密码），或塞进结构化 secret。

**D2. 前端 `accounts-page.jsx` / `config-panel.jsx`**：broker 下拉加 `usmart` 选项；uSMART 表单字段比 Alpaca 多（channel ID、RSA 私钥上传、手机号、交易密码）。i18n zh/en 同步加 key。

**D3. `auto_trade` / `trading` 配置**：`broker` 字段已是自由字符串，`use_broker` 流程通用。确认 `normalize_broker` 放行 usmart 后即可在自动交易里选 uSMART。

---

## 3. 测试与验证

- 单元测试 `tests/test_usmart_client.py`：monkeypatch HTTP，验证签名串构造、header 组装、token 重登逻辑、订单状态归一、符号转换、幂等 serialNo 生成。**不做真实调用**（与现有 `test_trading_phase*` 模式一致）。
- 签名正确性：用 uSMART 提供的测试密钥对 + demo 期望值做 golden test。
- 联调：UAT 环境（`open-jy-uat`）+ 测试 channel。

## 4. 阻塞项与风险

1. **🔴 必须先拿到**：uSMART 分配的 channel ID + 双方商定的 RSA 密钥对（签名 + 加密）+ UAT 测试账户。没有这些无法实现/联调签名。
2. **🟡 期权端点细节缺失**：文档 section 8（期权交易）在抓取内容里被截断。下单正股可做，**期权下单的确切 path/字段需要 uSMART 完整文档或 demo**。
3. **🟡 无原生止损**：uSMART 不支持 stop 单，必须依赖软件止损 worker（已有机制，但要确认 uSMART 账户的行情可用于触发）。
4. **🟡 GTC/GTD 不支持**：`validDate`/GTC 文档标注「暂不支持」，默认 DAY 当日有效——智能退出的 `time_exit` 需在软件层处理（已有）。

## 5. 建议落地顺序

1. **先确认阻塞项 1**（拿密钥/channel/UAT）——否则后续都是纸上谈兵。
2. 阶段 A（凭证 + 鉴权 + 签名）→ 单测签名 golden。
3. 阶段 B 正股下单/查询 → UAT 联调。
4. 阶段 B 期权（待完整文档）。
5. 阶段 C 接线 + 阶段 D 前端。
6. 全程遵守 trading-risk-and-safety-spec：实盘动作需确认文本、幂等、软件止损兜底。
