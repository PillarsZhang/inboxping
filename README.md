# InboxPing

> [!WARNING]
> 本项目主要通过 Vibe Coding 完成，尚未经过人工代码审查。请在部署前自行检查代码与配置，尤其不要在未经评估的公网环境或重要邮箱中直接使用。

面向个人使用的轻量级邮件监听、AI 理解与通知服务。InboxPing 会以只读方式持续监听多个邮箱，对新邮件进行中文摘要、重要度与安全风险判断，再将值得关注的内容推送到企业微信。同时提供动态 Web 界面、流式全文翻译、邮件导出和完整 CLI。

## 主要功能

- 多邮箱监听：支持 IMAP IDLE、持久 IMAP 轮询和 POP3 轮询，每个账户显式选择协议。
- 原邮箱只读：IMAP 使用只读文件夹和 `BODY.PEEK[]`，POP3 不发送 `DELE`，不改变已读状态。
- AI 邮件理解：输出概括标题、中文摘要、分类、重要度、风险、待办与截止日期。
- AI 决定推送：分别评估重要性与风险，并给出是否即时通知及理由；Prompt 要求危险邮件不推送。
- 可替换 AI：使用 OpenAI-compatible Chat Completions API，模型、接口和 Prompt 都可配置。
- 流式全文翻译：在邮件详情中边生成边显示中文译文，支持重新翻译、删除翻译和超时反馈。
- 企业微信通知：支持自建应用和群机器人，按通道去重、记录结果并重试失败投递。
- 动态 Web 界面：查看服务状态、邮箱连接、最近事件和邮件，支持筛选、分页、重新分析与手动推送。
- 按需导出：原文和译文可导出为带 YAML Front Matter 的 Markdown；EML 仅在下载时从原邮箱只读拉取。
- 轻量部署：Python 3.12、uv、SQLite 和 Docker Compose，无 Node 构建链路。

## 界面预览

服务、邮箱连接与 AI/通知配置状态：

![InboxPing 首页概览](docs/images/dashboard-overview.avif)

邮件列表、风险分数、推送决策与最近事件：

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

AI 会收到邮件的发件人、正文、附件元数据和 SPF/DKIM/DMARC 认证结果。可在 `trust` 中添加已确认的发件地址、发件域名或链接域名；每项只需 `value`，可选 `note` 记录确认依据。命中名单仅供 AI 参考，不自动放行、不覆盖恶意证据，也不能单凭可伪造的发件人地址认定可信。

群发邮件的完整收件人名单仍保存在本地，详情页默认只显示前几位和总人数，需要时可展开；邮件列表 API 只返回概况。AI 分析也只接收人数、少量地址示例与主要域名统计，不会把整份名单放进 Prompt。原文 Markdown 导出仍保留完整收件人信息。

详情页将邮件分类显示为中文，并记录 AI 分析和全文翻译的输入、输出 Token 用量。用量取自模型服务返回的 `usage`，服务未提供时显示“未提供”，不会按文本长度估算。默认设置 `ai.stream_usage: true` 请求流式用量；若其他 OpenAI-compatible 服务不支持 `stream_options`，可设为 `false`（服务若仍主动返回用量，程序也会记录）。

AI 会分别输出 0～1 的重要性分和风险分，并结合内容、时效及打扰成本给出 `should_push` 与具体理由。Prompt 要求高风险和疑似钓鱼、病毒邮件不推送；程序直接采用 AI 的决定，不再以分数阈值或风险等级重新计算。AI 分析失败或输出无效时，邮件保留在 Web 中等待重新分析，推送决定显示为“待判定”，不会自动发送。重新推送只重试 AI 决定要推送的邮件。分数仅用于展示与筛选，不代表实际投递成功。

本次分析表结构已改变。升级已有安装时，请先停服并备份、移走 `data/inboxping.db` 及可能存在的 `-wal`、`-shm` 文件，再启动新版本创建数据库；`clear-data` 保留旧表结构，不能用于此次重建。具体说明见 [运维文档](docs/operations.md)。

### 邮件接收协议

每个账户必须显式设置 `protocol`，InboxPing 不会自动猜测协议，也不会悄悄降级：

- `imap_idle`：使用标准 IMAP IDLE。仅用于服务器明确支持 IDLE 的账户。
- `imap_poll`：保持一个 IMAP 会话，每 `monitor.poll_interval_seconds` 秒检查一次；适合没有推送协议的服务器。
- `pop3_poll`：保持一个 POP3 会话并通过 `UIDL` 识别新邮件；只执行 `NOOP/UIDL/RETR`，不删除服务器邮件。

每个账户只配置所选协议对应的 `imap` 或 `pop3` 块，不能同时配置。IMAP 使用只读文件夹和 `BODY.PEEK[]`；POP3 从不发送 `DELE`。协议出错会明确写入账户状态和日志，由原协议重连，不跨协议自动降级。POP3 服务器必须支持 `UIDL`，否则 InboxPing 会拒绝运行，以免重复收取。

`monitor.max_message_bytes` 限制单封原始邮件大小，默认为 25 MiB。InboxPing 会先读取服务器报告的大小，超限时不下载正文、不调用 AI，仅在最近事件和日志中记录已跳过，并推进邮箱游标以避免反复处理。

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

仓库中的前端静态依赖可以通过辅助脚本校验，或按固定版本重新下载。脚本会校验 SHA-256，只有所有文件通过后才替换现有资源：

```bash
# 只校验当前文件，不联网
uv run python scripts/vendor_web_assets.py --check

# 重新下载 Bootstrap、Bootstrap Icons、Alpine.js 及其许可证
uv run python scripts/vendor_web_assets.py
```

```text
GET  /api/v1/overview
GET  /api/v1/accounts
GET  /api/v1/messages?account=&min_risk_score=&push=&limit=&offset=
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

API 与 Web 使用同一登录会话。响应中的 `should_push` 是 AI 的推送决定；`notifications` 只表示实际渠道及投递结果，两者语义相互独立。

### 全文翻译与导出

邮件详情页可按需调用当前 AI 模型，将已保存的纯文本正文翻译为简体中文。译文通过 NDJSON 流在正文下方实时展开，完成时不会重载页面或改变滚动位置。只有完整译文会写入数据库；请求失败、超时或浏览器中断都不会保存残缺内容。重新翻译失败时保留上一份完整译文；“删除翻译”只删除本地译文缓存，不修改原邮件。

`ai.max_output_chars` 限制单次 AI 流式输出的累计字符数，默认为 100,000。超限请求会立即中止，分析标记失败且不自动推送，翻译则在页面显示错误且不保存未完成内容。

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
uv run inboxping messages --limit 20 --offset 20
uv run inboxping messages --account university --min-risk-score 0.5
uv run inboxping messages --push-only

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
