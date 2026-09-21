# InboxPing

> [!WARNING]
> 本项目主要通过 Vibe Coding 完成，尚未经过人工代码审查。请在部署前自行检查代码与配置，尤其不要在未经评估的公网环境或重要邮箱中直接使用。

面向个人使用的轻量级邮件监听、AI 理解与通知服务。InboxPing 会以只读方式持续监听多个邮箱，对新邮件进行中文摘要、重要度与安全风险判断，再将值得关注的内容推送到企业微信。同时提供动态 Web 界面、流式全文翻译、邮件导出和完整 CLI。

## 主要功能

- 多邮箱监听：支持 IMAP IDLE、持久 IMAP 轮询和 POP3 轮询，每个账户显式选择协议。
- 原邮箱只读：IMAP 使用只读文件夹和 `BODY.PEEK[]`，POP3 不发送 `DELE`，不改变已读状态。
- AI 邮件理解：输出概括标题、中文摘要、分类、重要度、风险、待办与截止日期。
- 安全推送策略：风险抑制优先于 AI 的推送建议，疑似钓鱼、病毒或恶意邮件只在 Web 中留存审阅。
- 可替换 AI：使用 OpenAI-compatible Chat Completions API，模型、接口和 Prompt 都可配置。
- 流式全文翻译：在邮件详情中边生成边显示中文译文，支持重新翻译、删除翻译和超时反馈。
- 企业微信通知：支持自建应用和群机器人，按通道去重、记录结果并重试失败投递。
- 动态 Web 界面：查看服务状态、邮箱连接、最近事件和邮件，支持筛选、分页、重新分析与手动推送。
- 按需导出：原文和译文可导出为带 YAML Front Matter 的 Markdown；EML 仅在下载时从原邮箱只读拉取。
- 轻量部署：Python 3.12、uv、SQLite 和 Docker Compose，无 Node 构建链路。

## 界面预览

服务、邮箱连接与 AI/通知配置状态：

![InboxPing 首页概览](docs/images/dashboard-overview.avif)

邮件列表、风险等级、推送决策与最近事件：

![InboxPing 邮件列表](docs/images/dashboard-messages.avif)

邮件详情、投递状态、AI 分析、导出与流式翻译入口：

![InboxPing 邮件详情](docs/images/message-detail.avif)

## 配置

所有配置集中在 `config.yaml`，包括邮箱密码、AI API Key 和企业微信通道凭证；不再依赖 `.env`。仓库提供两份文件：

- `config.example.yaml`：可公开提交的通用模板。
- `config.yaml`：本机正式配置，已被 Git 和 Docker 构建上下文忽略。

示例配置默认使用 DeepSeek `deepseek-flash`、官方 `https://api.deepseek.com` 接口和 JSON Output。启动前请复制配置并填写 AI Key、邮箱账户及通知凭证：

```bash
cp config.example.yaml config.yaml
```

通知采用可扩展的命名通道。

### 通知通道

`notifications.channels` 可以保存多个推送目标，`type` 决定配置结构；当前支持企业微信自建应用 `wecom_app` 和群机器人 `wecom_webhook`。只有启用且被 `default_channels` 引用的通道才会收到自动通知：

```yaml
notifications:
  default_channels:
    - wecom_app
  channels:
    - id: wecom_app
      type: wecom_app
      enabled: true
      corp_id: "你的企业 ID"
      corp_secret: "你的应用 Secret"
      agent_id: 1000001
      to_user:
        - "@all"
    - id: wecom_group
      type: wecom_webhook
      enabled: false
      webhook_url: ""
  retry:
    max_attempts: 3
```

两个通道可以同时加入 `default_channels`，此时同一邮件会各推送一次。应用 Access Token 会缓存并在过期前刷新；每个通道在数据库中分别去重和记录每次真实发送，失败后由后台维护任务继续尝试，达到 `max_attempts` 后停止。可用以下命令检查通道、查询应用可见成员，或将它作为通用文本推送工具使用；命令不会显示凭证：

```bash
uv run inboxping notifications list
uv run inboxping notifications wecom-users wecom_app
uv run inboxping notifications send wecom_app "这是一条通知"
```

自建应用使用兼容性更好的纯文本消息；群机器人使用 Markdown 消息。
自动邮件通知保持为单条消息：按 UTF-8 字节预算优先保留概括标题、原主题、发件人、发件时间和截止时间，摘要在剩余空间内安全截断。`notifications send` 发送的任意文本超过限制时会自动编号分段；企业微信应用文本按 2048 字节、群机器人 Markdown 按 4096 字节处理。

生成 Web 登录密码哈希：

```bash
uv run inboxping hash-password
```

按提示隐藏输入并确认密码；命令不会把明文密码写入 shell 历史。将输出填入 `web.password_hash`，然后限制配置文件权限并检查配置：

```bash
chmod 600 config.yaml
uv run inboxping config check
uv run inboxping config show
```

Web 会话的两个选项相互独立：`web.session_max_age_days` 设置单次登录有效期，范围为 1～400 天，默认 400 天；`web.session_rolling` 控制访问时是否重新计算有效期，默认开启。关闭滚动续期后，会话会在首次登录满指定天数时失效。未设置 `password_hash` 时，页面会明确显示“匿名访问”。

`config show` 会自动脱敏所有秘密。只有配置文件路径仍可通过 `INBOXPING_CONFIG` 环境变量覆盖，例如：

```bash
INBOXPING_CONFIG=/etc/inboxping.yaml uv run inboxping doctor
```

可信来源保持为两类简单规则：

```yaml
rules:
  trusted_senders:
    - your-personal-address@example.com
  trusted_domains:
    - example.edu
    - example.org
```

信任规则只辅助风险分析，不会跳过存储或 AI，也不会强制推送。域名或地址命中后仍需邮件的 SPF、DKIM 或 DMARC 认证结果支持，避免仅凭可伪造的 `From` 地址直接信任。

推送决策联合使用 AI 的风险等级、推送建议和重要度：AI 判为 `medium` 或 `high` 的邮件默认只入库并展示在 Web；其余邮件在 AI 建议推送或重要度达到阈值时发送。禁止风险等级由 `analysis.suppress_risk_levels` 配置。若模型给出“高风险但建议推送”之类的矛盾结果，安全限制优先；后台重试和 Web 手动重推也不能绕过它。

### 邮件接收协议

每个账户必须显式设置 `protocol`，InboxPing 不会自动猜测协议，也不会悄悄降级：

- `imap_idle`：使用标准 IMAP IDLE。仅用于服务器明确支持 IDLE 的账户。
- `imap_poll`：保持一个 IMAP 会话，每 `monitor.poll_interval_seconds` 秒检查一次；适合没有推送协议的服务器。
- `pop3_poll`：保持一个 POP3 会话并通过 `UIDL` 识别新邮件；只执行 `NOOP/UIDL/RETR`，不删除服务器邮件。

每个账户只配置所选协议对应的 `imap` 或 `pop3` 块，不能同时配置。IMAP 使用只读文件夹和 `BODY.PEEK[]`；POP3 从不发送 `DELE`。协议出错会明确写入账户状态和日志，由原协议重连，不跨协议自动降级。POP3 服务器必须支持 `UIDL`，否则 InboxPing 会拒绝运行，以免重复收取。

## 本地运行

要求 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --extra dev
uv run inboxping init-db
uv run inboxping doctor
uv run inboxping serve --reload
```

打开 <http://127.0.0.1:8000>。监听地址和端口来自 `config.yaml` 的 `web.host` 与 `web.port`。
首页和邮件详情使用 Bootstrap、Bootstrap Icons 与 Alpine.js，通过同源 JSON API 动态加载；筛选、分页、刷新、重新分析和重新推送均无需整页刷新。前端依赖固定版本并随应用本地提供，不依赖公网 CDN，也不需要 Node 构建环境。API 文档位于 <http://127.0.0.1:8000/docs>，主要端点如下：

```text
GET  /api/v1/overview
GET  /api/v1/accounts
GET  /api/v1/messages?account=&risk=&push=&limit=&offset=
GET  /api/v1/messages/{id}
GET  /api/v1/messages/{id}/export/eml
GET  /api/v1/messages/{id}/export/original.md
GET  /api/v1/messages/{id}/export/translation.md
GET  /api/v1/events
POST /api/v1/messages/{id}/reanalyze
POST /api/v1/messages/{id}/notify
POST /api/v1/messages/{id}/translate
DELETE /api/v1/messages/{id}/translation
```

API 与 Web 使用同一登录会话。响应中的 `should_push` 表示系统判断该邮件是否需要推送；`notifications` 只表示实际渠道及投递结果，两者语义相互独立。

### 全文翻译与导出

邮件详情页可按需调用当前 AI 模型，将已保存的纯文本正文翻译为简体中文。译文通过 NDJSON 流在正文下方实时展开，完成时不会重载页面或改变滚动位置。只有完整译文会写入数据库；请求失败、超时或浏览器中断都不会保存残缺内容。重新翻译失败时保留上一份完整译文；“删除翻译”只删除本地译文缓存，不修改原邮件。

命令行也可生成或更新译文：

```bash
uv run inboxping translate-message 12
uv run inboxping translate-message 12 --force
```

原文和译文可分别下载为 Markdown，文件使用扁平的 YAML Front Matter 保存主题、发件人、收件人、邮件时间和 Message-ID。Markdown 由后端生成并直接下载，前端不引入额外渲染库。EML 不在本地持久化；用户点击下载时，系统才会按原账户与邮件标识从原邮箱只读获取。如果邮件已被移动或删除，或 IMAP UIDVALIDITY 已变化，就无法再可靠拉取。

临时开启最详细日志并同时保存到项目内：

```bash
uv run inboxping serve --log-level TRACE 2>&1 | tee logs/inboxping-latest.log
```

## Docker Compose

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f --tail=200
```

Compose 将 `config.yaml` 和 `prompts/` 只读挂载进容器，数据保存在 `./data`。镜像保持容器 UID `0`，在 rootless Docker 中会映射为宿主机当前普通用户，便于读写挂载数据。宿主机端口默认只绑定 `127.0.0.1`；需要远程访问时，请通过可信反向代理、Tailscale 或 Tunnel 提供 TLS 和访问控制。

## 安全生成界面截图

不要直接对真实邮箱页面截图后打码。演示命令会创建一套临时的虚构配置和邮件数据，不读取 `config.yaml`，也不会连接邮箱、AI 或企业微信：

```bash
uv run inboxping demo
```

打开 <http://127.0.0.1:8001> 截图。页面使用 `example.com`、`example.org` 和 `example.edu` 保留域名；按 `Ctrl+C` 退出后，临时数据库会自动删除。
本 README 中的界面预览也由该模式生成，不包含真实邮件数据。

## Prompt 调试

`prompts/` 中的分析与翻译 Prompt 可直接编辑，它们是程序使用的唯一 Prompt 来源；任何必需文件缺失都会明确报错。可以导入本地 `.eml` 或对已保存邮件反复分析：

```bash
# 只读增量同步，不调用 AI、不发送通知
uv run inboxping sync university --limit 20
uv run inboxping messages --limit 20
uv run inboxping messages --account university --risk low --push-only

# 同步全部已启用邮箱，并分析新邮件
uv run inboxping sync --analyze

# 完整执行同步、分析和通知
uv run inboxping sync --analyze --notify

uv run inboxping prompt-show
uv run inboxping import-eml samples/example.eml --analyze
uv run inboxping analyze-message 12 --dry-run
uv run inboxping analyze-message 12 --force
uv run inboxping reprocess --limit 20
```

停止服务后，可清空全部本地邮件、分析、翻译、通知、事件和邮箱游标，同时保留数据库结构：

```bash
uv run inboxping clear-data
# 脚本中明确跳过确认：
uv run inboxping clear-data --yes
```

邮件正文、元数据、分析、翻译及通知记录都会永久保留，只有主动执行 `clear-data` 才会清空。HTML 邮件会转换出用于阅读和分析的纯文本；完整原始 EML 不在本地保存。

完整需求与运维说明见 `docs/requirements.md` 和 `docs/operations.md`。
