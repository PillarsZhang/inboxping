# 运维说明

## 安全

- `config.yaml` 包含邮箱密码、API Key、企业微信 Secret 等秘密，权限建议设为 `0600`，不要提交版本库或复制进镜像。
- Web 默认仅绑定本机；公网访问必须增加 HTTPS 与额外访问控制。
- 邮箱账户最好使用应用专用密码。服务不会记录密码与完整 AI Key。
- Prompt 与邮件正文属于不可信输入；AI 结果只用于分类和通知，不会执行邮件中的指令。

## 数据

持久数据位于 `./data/inboxping.db`。SQLite 使用 WAL，备份时可先停止容器，再复制整个 `data` 目录。邮件正文、元数据、分析、翻译和通知记录默认永久保留，只有主动执行 `clear-data` 才会清空。原始 EML（包括附件）仅在用户下载时从原邮箱只读获取，不写入数据库。

AI 决策版调整了分析表结构。首次升级前先停止服务，备份并移走 `data/inboxping.db` 及同名的 `-wal`、`-shm` 文件，再启动服务创建新库。`clear-data` 只清空数据并保留原表结构，不能用于本次结构切换。新库首次监听按各账户的 `initial_sync_limit` 建立游标，不会重新处理全部历史邮件。

AI 使用流式响应。`connect_timeout_seconds` 限制建立连接的等待时间，`read_timeout_seconds` 限制连续没有新数据的等待时间，`max_stream_seconds` 限制单次请求总时长。翻译过程中只在内存和浏览器中保留增量文本，完整结束后才写入数据库。

## 常用命令

```bash
uv run inboxping doctor
uv run inboxping init-db
uv run inboxping accounts
uv run inboxping messages --limit 20
uv run inboxping messages --account university --push-only
uv run inboxping notifications list
uv run inboxping notifications wecom-users wecom_app
uv run inboxping notifications send wecom_app "这是一条通知"
# 必须先停止服务；清空全部运行数据和邮箱游标
uv run inboxping clear-data --yes
docker compose logs -f --tail=200
```
