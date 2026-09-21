from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

import typer
import uvicorn
import yaml
from loguru import logger
from pwdlib import PasswordHash
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import selectinload

from inboxping.ai.prompts import load_prompt, validate_prompt_files
from inboxping.config import LOG_LEVEL_ENV, config_path, get_settings, load_settings
from inboxping.db import Database
from inboxping.logging import configure_logging
from inboxping.mail.monitor import MailMonitor
from inboxping.mail.parser import normalize_utc, parse_message
from inboxping.models import AccountState, Message
from inboxping.services.pipeline import Pipeline
from inboxping.services.push_policy import push_decision_expression, should_push

app = typer.Typer(no_args_is_help=True, help="InboxPing 邮件分析与通知服务")
config_app = typer.Typer(no_args_is_help=True, help="检查和查看 YAML 配置")
notifications_app = typer.Typer(no_args_is_help=True, help="管理和测试通知通道")
app.add_typer(config_app, name="config")
app.add_typer(notifications_app, name="notifications")


def context() -> tuple[object, Database, Pipeline]:
    settings = get_settings()
    configure_logging(settings)
    db = Database(settings)
    db.init()
    return settings, db, Pipeline(settings, db)


def _public_http_url(value: str) -> str:
    """Remove credentials, query parameters and fragments from a displayed URL."""
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


@app.command("serve")
def serve(
    host: Annotated[str | None, typer.Option(help="覆盖配置中的监听地址")] = None,
    port: Annotated[int | None, typer.Option(help="覆盖配置中的监听端口")] = None,
    reload: Annotated[bool, typer.Option(help="开发模式自动重载")] = False,
    log_level: Annotated[
        str | None,
        typer.Option(help="覆盖日志级别：TRACE/DEBUG/INFO/WARNING/ERROR"),
    ] = None,
) -> None:
    if log_level:
        normalized_level = log_level.upper()
        if normalized_level not in {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise typer.BadParameter(f"不支持的日志级别: {log_level}", param_hint="--log-level")
        os.environ[LOG_LEVEL_ENV] = normalized_level
        get_settings.cache_clear()
    settings = get_settings()
    uvicorn.run(
        "inboxping.web.app:create_app",
        factory=True,
        host=host or settings.web.host,
        port=port or settings.web.port,
        reload=reload,
    )


@app.command("demo")
def demo(
    host: Annotated[str, typer.Option(help="演示页面监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="演示页面监听端口")] = 8001,
) -> None:
    """使用临时虚构数据启动安全的截图演示页面。"""
    from inboxping.demo import build_demo_settings, seed_demo_data
    from inboxping.web.app import create_app

    with tempfile.TemporaryDirectory(prefix="inboxping-demo-") as temp_dir:
        settings = build_demo_settings(Path(temp_dir) / "demo.db")
        seed_demo_data(settings)
        typer.echo("演示模式不读取 config.yaml，也不会连接邮箱、AI 或通知服务。")
        typer.echo(f"打开 http://{host}:{port}，按 Ctrl+C 退出并清理临时数据。")
        uvicorn.run(create_app(settings, demo_mode=True), host=host, port=port)


@app.command("init-db")
def init_db() -> None:
    settings = get_settings()
    configure_logging(settings)
    Database(settings).init()
    typer.echo("数据库已初始化")


@app.command("hash-password")
def hash_password() -> None:
    """交互式生成 Web 登录密码哈希，不在命令历史中留下明文。"""
    password = typer.prompt("登录密码", hide_input=True, confirmation_prompt=True)
    typer.echo(PasswordHash.recommended().hash(password))


@app.command("doctor")
def doctor() -> None:
    settings, db, pipeline = context()
    failures = 0
    database_url = make_url(settings.storage.database_url).render_as_string(hide_password=True)
    typer.echo(f"数据库: {database_url}")
    with db.session() as session:
        session.execute(select(1))
    typer.echo("  ✓ 可读写")
    typer.echo(f"AI: {_public_http_url(settings.ai.base_url)} / {settings.ai.model}")
    typer.echo("  ✓ 已启用" if pipeline.ai.enabled else "  ! 未启用，将使用本地降级分析")
    typer.echo("通知: " + ("✓ 已配置" if pipeline.notifier.enabled else "! 未配置"))
    for channel_id, notifier in pipeline.notifier.channels.items():
        default = " [默认]" if channel_id in pipeline.notifier.default_channel_ids else ""
        typer.echo(f"  ✓ {channel_id}: {notifier.type}{default}")
    typer.echo(f"配置文件: {config_path()}")
    typer.echo(f"邮箱账户: {len(settings.mail.accounts)}")
    for account in settings.mail.accounts:
        if not account.enabled:
            typer.echo(f"  - {account.id}: 已禁用 [{account.protocol}]")
            continue
        password_ok = bool(account.password.get_secret_value())
        mark = "✓" if password_ok else "✗"
        protocol_config = account.pop3 if account.protocol == "pop3_poll" else account.imap
        if protocol_config is None:  # 已由 Pydantic 校验，此分支仅防御运行时构造。
            raise RuntimeError(f"邮箱 {account.id} 缺少 {account.protocol} 配置")
        endpoint = protocol_config.host
        address = f"{account.username} / {endpoint}"
        typer.echo(f"  {mark} {account.id}: {address} [{account.protocol}]")
        failures += int(not password_ok)
    if settings.web.session_secret.get_secret_value() == "change-me-before-production":
        typer.echo("  ! 请修改 web.session_secret")
    if not settings.web.password_hash.get_secret_value():
        typer.echo("  ! Web 登录密码未配置，仅适合本机访问")
    if failures:
        raise typer.Exit(1)


@config_app.command("check")
def config_check() -> None:
    """校验配置结构，并检查运行必需的秘密。"""
    settings = load_settings()
    problems: list[str] = []
    if settings.ai.enabled and not settings.ai.api_key.get_secret_value():
        problems.append("ai.api_key 为空")
    for account in settings.mail.accounts:
        if account.enabled and not account.password.get_secret_value():
            problems.append(f"mail.accounts[{account.id}].password 为空")
    if not settings.web.password_hash.get_secret_value():
        problems.append("web.password_hash 为空，Web 登录认证未启用")
    try:
        validate_prompt_files(settings.analysis.prompt_dir)
    except FileNotFoundError as exc:
        problems.append(str(exc))
    typer.echo(f"✓ YAML 结构有效：{config_path()}")
    for problem in problems:
        typer.echo(f"! {problem}")
    if any(
        "password 为空" in item or "api_key 为空" in item or "Prompt 文件不存在" in item
        for item in problems
    ):
        raise typer.Exit(1)


@config_app.command("show")
def config_show() -> None:
    """显示最终配置；SecretStr 字段自动脱敏。"""
    settings = load_settings()
    safe = json.loads(settings.model_dump_json(by_alias=True))
    typer.echo(yaml.safe_dump(safe, allow_unicode=True, sort_keys=False))


@notifications_app.command("list")
def notification_channels() -> None:
    """列出已启用通道，不显示任何凭证。"""
    _, _, pipeline = context()
    if not pipeline.notifier.channels:
        typer.echo("没有已启用的通知通道")
        return
    for channel_id, notifier in pipeline.notifier.channels.items():
        default = "默认" if channel_id in pipeline.notifier.default_channel_ids else "非默认"
        typer.echo(f"{channel_id:20} {notifier.type:16} {default}")


@notifications_app.command("wecom-users")
def notification_wecom_users(
    channel_id: Annotated[str, typer.Argument(help="wecom_app 通道 ID")],
) -> None:
    """通过企业微信接口列出应用可见成员及其 UserID。"""
    _, _, pipeline = context()
    try:
        users = asyncio.run(pipeline.notifier.list_wecom_users(channel_id))
    except Exception as exc:
        typer.echo(f"查询失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    if not users:
        typer.echo("接口调用成功，但应用可见范围内没有成员")
        return
    for user in users:
        departments = ",".join(map(str, user.department)) or "-"
        typer.echo(f"{user.userid:24} {user.name}  部门={departments}")


@notifications_app.command("send")
def notification_send(
    channel_id: Annotated[str, typer.Argument(help="通知通道 ID")],
    text: Annotated[str, typer.Argument(help="要发送的文本")],
) -> None:
    """通过指定通知通道发送任意文本。"""
    _, _, pipeline = context()
    try:
        asyncio.run(pipeline.notifier.send_text(channel_id, text))
    except Exception as exc:
        typer.echo(f"发送失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"✓ 消息已发送到 {channel_id}")


@app.command("accounts")
def accounts() -> None:
    _, db, _ = context()
    with db.session() as session:
        states = session.scalars(select(AccountState).order_by(AccountState.account_id)).all()
        for state in states:
            prefix = f"{state.account_id:16} {state.status:12} uid={state.last_uid:<8}"
            typer.echo(f"{prefix} error={state.last_error or '-'}")
    if not states:
        typer.echo("尚无账户状态；启动服务后会自动创建。")


@app.command("messages")
def messages(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 20,
    account: Annotated[str | None, typer.Option(help="只显示指定账户")] = None,
    risk: Annotated[
        str | None, typer.Option(help="只显示指定风险：low/medium/high/unknown")
    ] = None,
    push_only: Annotated[bool, typer.Option(help="只显示系统判断需要推送的邮件")] = False,
) -> None:
    settings, db, _ = context()
    if risk and risk not in {"low", "medium", "high", "unknown"}:
        raise typer.BadParameter("风险必须是 low/medium/high/unknown", param_hint="--risk")
    conditions = []
    if account:
        conditions.append(Message.account_id == account)
    if risk:
        conditions.append(Message.analysis.has(risk_level=risk))
    if push_only:
        conditions.append(Message.analysis.has(push_decision_expression(settings)))
    with db.session() as session:
        rows = session.scalars(
            select(Message)
            .options(selectinload(Message.analysis))
            .where(*conditions)
            .order_by(
                func.coalesce(Message.sent_at, Message.received_at).desc(),
                Message.id.desc(),
            )
            .limit(limit)
        ).all()
        for row in rows:
            decision = "推送" if should_push(row.analysis, settings) else "-"
            prefix = f"{row.id:6} {row.account_id:16} {row.status:10} {decision:4}"
            typer.echo(f"{prefix} {row.sender_address[:28]:28} {row.subject[:60]}")
    if not rows:
        typer.echo("没有符合条件的邮件")


@app.command("sync")
def sync_mail(
    account_id: Annotated[
        str | None, typer.Argument(help="邮箱账户 ID；省略时同步全部已启用账户")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=500, help="每个账户本次最多同步数")] = 50,
    analyze: Annotated[bool, typer.Option(help="对新邮件调用 AI 分析")] = False,
    notify: Annotated[bool, typer.Option(help="按策略发送企业微信通知，需要 --analyze")] = False,
) -> None:
    """只读增量同步新邮件；可选执行 AI 分析和通知。"""
    if notify and not analyze:
        raise typer.BadParameter("--notify 必须与 --analyze 一起使用")
    settings, db, pipeline = context()
    monitor = MailMonitor(settings, db, pipeline)
    if account_id:
        account_ids = [account_id]
    else:
        account_ids = [account.id for account in settings.mail.accounts if account.enabled]

    async def run() -> bool:
        failed = False
        for current_id in account_ids:
            try:
                ids = await monitor.sync_once(current_id, limit=limit, schedule_analysis=analyze)
                typer.echo(f"{current_id}: 新增 {len(ids)} 封")
                if ids:
                    typer.echo(f"  本地邮件 ID：{', '.join(map(str, ids))}")
                if analyze:
                    for message_id in ids:
                        await pipeline.process(message_id, notify=notify)
                    typer.echo(f"  已分析 {len(ids)} 封，通知={'开启' if notify else '关闭'}")
            except Exception as exc:
                failed = True
                logger.bind(account=current_id).error("同步失败: {}", exc)
                typer.echo(f"{current_id}: 同步失败（{type(exc).__name__}: {exc}）")
        return failed

    failed = asyncio.run(run())
    if not analyze:
        typer.echo("本次仅同步邮件，未调用 AI，未发送通知。")
    if failed:
        raise typer.Exit(1)


@app.command("prompt-show")
def prompt_show(
    message_id: Annotated[int | None, typer.Option(help="用指定邮件渲染用户 Prompt")] = None,
) -> None:
    settings, db, pipeline = context()
    prompt = load_prompt(settings.analysis.prompt_dir)
    typer.echo(f"Prompt version: {prompt.version}\n\n[SYSTEM]\n{prompt.system}")
    if message_id is None:
        typer.echo(f"\n[USER TEMPLATE]\n{prompt.user_template}")
        return
    with db.session() as session:
        message = session.get(Message, message_id)
        if not message:
            raise typer.BadParameter(f"邮件 {message_id} 不存在")
        trust = pipeline.ai.trust_context(message)
        typer.echo(f"\n[RENDERED USER]\n{prompt.render(message, trust)}")


@app.command("analyze-message")
def analyze_message(
    message_id: int,
    dry_run: Annotated[bool, typer.Option(help="仅展示将发送的 Prompt")] = False,
    force: Annotated[bool, typer.Option(help="覆盖已有分析")] = False,
    notify: Annotated[bool, typer.Option(help="按规则发送通知")] = False,
) -> None:
    settings, db, pipeline = context()
    if dry_run:
        prompt = load_prompt(settings.analysis.prompt_dir)
        with db.session() as session:
            message = session.get(Message, message_id)
            if not message:
                raise typer.BadParameter(f"邮件 {message_id} 不存在")
            trust = pipeline.ai.trust_context(message)
            typer.echo(f"[SYSTEM]\n{prompt.system}\n\n[USER]\n{prompt.render(message, trust)}")
        return
    result = asyncio.run(pipeline.process(message_id, force=force, notify=notify))
    typer.echo(result.raw_json)


@app.command("translate-message")
def translate_message(
    message_id: Annotated[int, typer.Argument(help="本地邮件 ID")],
    force: Annotated[bool, typer.Option(help="覆盖已有翻译")] = False,
) -> None:
    """将邮件全文翻译为简体中文并缓存结果。"""
    _, _, pipeline = context()
    translation = asyncio.run(pipeline.translate_message(message_id, force=force))
    typer.echo(translation.translated_text)


@app.command("import-eml")
def import_eml(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    account: Annotated[str, typer.Option(help="用于标记样本来源的账户名")] = "local",
    analyze: Annotated[bool, typer.Option(help="导入后立即分析")] = False,
) -> None:
    """导入本地 EML 样本，便于离线迭代 Prompt。"""
    settings, db, pipeline = context()
    raw_eml = path.read_bytes()
    parsed = parse_message(raw_eml, settings.analysis.body_max_chars)
    timestamp = int(datetime.now(UTC).timestamp() * 1_000_000)
    with db.session() as session:
        message = Message(
            account_id=account,
            folder="LOCAL",
            uid_validity=0,
            uid=timestamp,
            message_id=parsed.message_id,
            subject=parsed.subject,
            sender_name=parsed.sender_name,
            sender_address=parsed.sender_address,
            recipients=parsed.recipients,
            sent_at=normalize_utc(parsed.sent_at),
            text_body=parsed.text_body,
            attachments_json=parsed.attachments_json(),
            authentication_json=json.dumps(parsed.authentication, ensure_ascii=False),
            status="pending" if analyze else "stored",
        )
        session.add(message)
        session.flush()
        message_id = message.id
    typer.echo(f"已导入邮件 ID={message_id}，主题：{parsed.subject}")
    if analyze:
        result = asyncio.run(pipeline.process(message_id, force=True, notify=False))
        typer.echo(result.raw_json)


@app.command("reprocess")
def reprocess(
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
    failed_only: Annotated[bool, typer.Option(help="仅重试降级/失败邮件")] = True,
) -> None:
    _, db, pipeline = context()
    with db.session() as session:
        query = select(Message.id).order_by(Message.received_at.desc()).limit(limit)
        if failed_only:
            query = query.where(Message.status.in_(["fallback", "error", "pending"]))
        ids = list(session.scalars(query).all())

    async def run() -> None:
        for message_id in ids:
            try:
                await pipeline.process(message_id, force=True, notify=False)
                logger.info("已重处理邮件 {}", message_id)
            except Exception:
                logger.exception("重处理邮件 {} 失败", message_id)

    asyncio.run(run())
    typer.echo(f"完成 {len(ids)} 封邮件的重处理")


@app.command("clear-data")
def clear_data(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="确认服务已停止并跳过交互确认"),
    ] = False,
) -> None:
    """清空全部运行数据和邮箱游标，保留数据库结构。"""
    if not yes and not typer.confirm(
        "请先停止 InboxPing 服务。确定清空邮件、分析、翻译、通知、事件和邮箱游标吗？"
    ):
        raise typer.Abort()
    settings = get_settings()
    configure_logging(settings)
    db = Database(settings)
    db.init()
    counts = db.clear_all_data()
    typer.echo(
        "已清空："
        f"邮件 {counts['messages']}，"
        f"分析 {counts['analyses']}，"
        f"翻译 {counts['translations']}，"
        f"通知 {counts['notifications']}，"
        f"事件 {counts['events']}，"
        f"账户游标 {counts['account_states']}"
    )


if __name__ == "__main__":
    app()
