# OAuth 登录接入指南（Google / Apple）

本文档说明如何为 AI Option 配置「使用 Google / Apple 登录」。功能默认关闭：
只有在 `.env` 里填了对应的 client id，登录页和账号设置页才会显示按钮，否则
完全隐藏、不影响原有账号密码登录。

## 工作原理

- 前端用 Google Identity Services / Sign in with Apple JS 拿到一个 `id_token`。
- 后端用 Google / Apple 的 JWKS 公钥**验签**该 token，并校验 `iss` / `aud` /
  `exp` / `nonce` / `email_verified`。
- 验证通过后，按以下顺序定位账号：
  1. 已存在的 `(provider, sub)` 绑定；
  2. 否则按 verified email 匹配已有账号并自动绑定（provider 已证明邮箱所有权）；
  3. 否则**自动开号**，发放 15 天试用。
- 然后下发与密码登录**完全相同**的会话 Cookie。

> 我们只验证 `id_token`，**不做** authorization-code 交换，因此 Google 不需要
> client secret，Apple 当前流程也不需要 `.p8` 私钥。

## 试用账号配额

OAuth 自动开号的默认权限（定义在 `app_auth.py` 的 `OAUTH_TRIAL_LIMITS`）：

| 项目 | 值 |
|---|---|
| 有效期 | 15 天 |
| 每日扫描 | 5 |
| 每日 AI 精扫 | 5（与扫描同额，让 5 次都能用 AI） |
| 每日 AI 对话 | 10 |
| 股票池 / 雷达 / 通知 / Longbridge 账号 | 0（关闭） |
| 实盘权限 `can_trade` | 否（需管理员后续放开） |

改这些数值只需改 `app_auth.py` 里 `OAUTH_TRIAL_LIMITS` / `OAUTH_TRIAL_DAYS` 一处。

## 前置条件：HTTPS + 真实域名

Google 和 Apple 的网页登录都要求 HTTPS + 真实域名（Google 仅 `http://localhost`
例外，Apple 连 localhost 都不行，且禁止裸 IP）。本项目生产域名示例：

```
https://your-domain.example
```

下面的「授权来源 / 回调」都用这个域名，请替换成你自己的。

## 一、Google（约 10 分钟，免费）

1. 打开 https://console.cloud.google.com ，选一个项目（或新建）。
2. **APIs & Services → OAuth consent screen**：
   - User Type 选 **External**，填应用名、support email、开发者邮箱。
   - Scopes 只加 `openid`、`email`、`profile`（基础信息，无需 Google 审核）。
   - 测试阶段为 "Testing"，仅 Test users 列表里的邮箱能登录；要对所有人开放
     点 **Publish app**（基础 scope 秒过）。
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**：
   - Application type 选 **Web application**。
   - **Authorized JavaScript origins**（精确到协议，无路径、无端口）：
     ```
     https://your-domain.example
     ```
   - **Authorized redirect URIs**：弹窗 token 模式可留空，保险也填同一域名。
4. 复制 **Client ID**（形如 `xxxx.apps.googleusercontent.com`），填入：
   ```bash
   GOOGLE_OAUTH_CLIENT_ID=xxxx.apps.googleusercontent.com
   ```
   Client secret 用不到，忽略。
<!-- DOC-CONTINUE -->

## 二、Apple（需付费 Developer 会员）

前提：**Apple Developer Program** 会员（$99/年）。

1. https://developer.apple.com → **Certificates, IDs & Profiles → Identifiers**。
2. 新建 **App ID**（Type: App），勾选 **Sign in with Apple** 能力。
3. 新建 **Services ID**（Type: Services IDs）：
   - Identifier 用反向域名，例如 `com.example.aioption.web`。
     **这个字符串就是 `APPLE_OAUTH_CLIENT_ID`**（即 token 的 `aud`）。
   - 勾选 **Sign in with Apple → Configure**：
     - Primary App ID 选第 2 步的 App ID。
     - **Domains and Subdomains**：`your-domain.example`
     - **Return URLs**：`https://your-domain.example`
4. **域名验证**：Apple 给一个 `apple-developer-domain-association.txt`。把它放到服务器，
   并在 `.env` 指向它：
   ```bash
   APPLE_OAUTH_CLIENT_ID=com.example.aioption.web
   APPLE_DOMAIN_ASSOCIATION_FILE=/opt/ai-option/secrets/apple-domain-association.txt
   ```
   后端会在 `https://<域名>/.well-known/apple-developer-domain-association.txt`
   提供该文件供 Apple 校验。
5. （本流程不需要）Keys 里的 `.p8` 私钥暂不用建——只有将来做服务端 token 刷新/
   授权码流程才需要。

## 三、填好后验证

```bash
# 端点应返回已启用的 provider 列表
curl https://your-domain.example/api/auth/oauth/config
# -> {"enabled": true, "providers": [{"provider": "google", "client_id": "..."}, ...]}

# Apple 域名验证文件可访问
curl https://your-domain.example/.well-known/apple-developer-domain-association.txt
```

登录页应出现对应按钮；勾选「同意条款」后点按钮即可走第三方登录。已登录用户可在
**账号设置 → 登录绑定** 里绑定/解绑。

## 安全说明

- OAuth 登录同样**强制「同意条款」**，与密码登录一致。
- 使用 **nonce 防重放**：前端生成、随 token 提交，后端再次校验。
- **verified-email 自动绑定**：仅当 provider 标记 `email_verified=true` 时，才把
  OAuth 身份匹配到同名已有账号。Apple 私有转发邮箱天然唯一，不会撞号。
- **解绑防锁死**：当账号既无可用密码、又只剩唯一一个 OAuth 绑定时，后端拒绝解绑。
- client id 是公开值（本就出现在浏览器里），可以提交；但 Apple 域名验证文件路径
  与任何 secret 仍只放服务器私有环境，不要提交仓库。

## 四、Cloudflare 机器人防护

域名已托管在 Cloudflare（LB 上是 Cloudflare 源站证书）。两层防护互补：
**Bot Fight Mode** 是全站、零代码的边缘拦截；**Turnstile** 是登录/注册表单上的
人机验证，专治撞库与试用小号农场。

### 4.1 Bot Fight Mode（仪表盘开关，约 1 分钟）

1. 登录 Cloudflare 仪表盘并选择你的站点域名。
2. 左侧 **Security → Bots**。
3. 打开 **Bot Fight Mode**（免费版即可）。付费版可用 **Super Bot Fight Mode**，
   能对「已验证机器人 / 疑似机器人 / 自动化流量」分别设置 Allow / Block / Challenge。
4. 无需改代码、无需部署，开启后即对全站自动化流量生效。

> 注意：Bot Fight Mode 会挑战可疑的自动化请求。若有需要放行的良性爬虫或自有监控，
> 在 **Security → WAF → Tools / Custom rules** 里按 User-Agent 或 IP 加放行规则。

### 4.2 Turnstile 人机验证（登录表单，需填两个 env）

Turnstile 是 Cloudflare 的隐私友好型验证码：浏览器渲染一个（通常隐形的）小组件，
拿到一次性 token，后端再用 token 向 Cloudflare `siteverify` 换取「确属真人」的结论。

获取密钥：

1. Cloudflare 仪表盘 → **Turnstile** → **Add site**。
2. 域名填 `your-domain.example`；Widget Mode 选 **Managed**（推荐，多数情况下隐形）。
3. 创建后得到一对密钥：
   - **Site Key**（公开，出现在浏览器里）→ `TURNSTILE_SITE_KEY`
   - **Secret Key**（服务器私有，切勿提交）→ `TURNSTILE_SECRET_KEY`
4. 把这两个值写入各节点的 `/opt/ai-option/current/.env` 与 `/opt/ai-option/.env`，
   重启服务（与 OAuth client id 的下发方式一致，详见部署脚本）。

行为约定：

- **两个 key 都设了才启用**。只设其一视为未配置，登录页不渲染组件、提交不带 token，
  与现状完全一致——这样生产（尚未配置）与本地开发不受影响。
- 启用后，密码登录与第三方登录**都**要求先通过验证；验证在校验密码/token 之前进行。
- **失败即拒（fail closed）**：siteverify 网络异常时拒绝而非放行，避免被「制造错误」绕过；
  用户重试即可。token 一次性、约 5 分钟过期，登录失败后前端会自动重置取新 token。

验证：

```bash
# 启用后应返回 site_key；未配置则 {"enabled": false}
curl https://your-domain.example/api/auth/turnstile/config
# -> {"enabled": true, "site_key": "0x4AAAAAAA..."}
```

## 相关代码

| 关注点 | 文件 |
|---|---|
| token 验签 | `ai_option_scanner/oauth_login.py` |
| `(provider, sub)` 绑定表 | `ai_option_scanner/oauth_store.py` |
| 开号 / 试用配额 / 密码判定 | `ai_option_scanner/app_auth.py` |
| 端点 `/api/auth/oauth/*` + Apple 域名文件路由 | `ai_option_scanner/web_api.py` |
| 前端按钮 / SDK 加载 | `web/src/pages/login-page.jsx`, `web/src/utils/oauth-clients.js` |
| 绑定管理 | `web/src/pages/accounts-page.jsx` |
| Turnstile 验签 + `siteverify` | `ai_option_scanner/turnstile.py` |
| Turnstile 组件加载 | `web/src/utils/turnstile-widget.js` |
| 测试 | `tests/test_oauth_login.py`, `tests/test_turnstile.py` |
