from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pwdlib import PasswordHash
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware

from inboxping.config import Settings, get_settings
from inboxping.db import Database
from inboxping.logging import configure_logging
from inboxping.mail.monitor import MailMonitor
from inboxping.mail.source_fetch import fetch_message_eml
from inboxping.models import AccountState, Analysis, Event, Message
from inboxping.services.exports import (
    attachment_header,
    render_original_markdown,
    render_translation_markdown,
    safe_filename,
)
from inboxping.services.maintenance import MaintenanceWorker
from inboxping.services.pipeline import NotificationSuppressedError, Pipeline
from inboxping.services.push_policy import push_decision_expression
from inboxping.web.schemas import (
    AccountResponse,
    ActionResponse,
    EventResponse,
    MessageDetailResponse,
    MessageListResponse,
    OverviewResponse,
    ServicesResponse,
    StatsResponse,
    account_response,
    event_response,
    message_summary,
    serialize_message_detail,
    translation_response,
)

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
password_hash = PasswordHash.recommended()
templates.env.globals["asset_version"] = max(
    path.stat().st_mtime_ns for path in (PACKAGE_DIR / "static").iterdir() if path.is_file()
)


def create_app(settings: Settings | None = None, *, demo_mode: bool = False) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    db = Database(settings)
    pipeline = Pipeline(settings, db)
    monitor = MailMonitor(settings, db, pipeline)
    maintenance = MaintenanceWorker(settings, db, pipeline)
    notification_channels = {
        channel.id: {
            "type": "企业微信应用" if channel.type == "wecom_app" else "企业微信群机器人",
            "target": (
                "、".join(channel.to_user) if channel.type == "wecom_app" else "Webhook 所在群聊"
            ),
        }
        for channel in settings.notifications.channels
    }
    push_decision = push_decision_expression(settings)
    ai_url = urlsplit(settings.ai.base_url)
    public_ai_base_url = urlunsplit((ai_url.scheme, ai_url.netloc, ai_url.path, "", ""))
    auth_enabled = bool(settings.web.password_hash.get_secret_value())
    page_context = {
        "auth_enabled": auth_enabled,
        "viewer": settings.web.username,
        "demo_mode": demo_mode,
    }
    session_days = settings.web.session_max_age_days

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init()
        app.state.db = db
        app.state.pipeline = pipeline
        app.state.monitor = monitor
        maintenance_task = None
        if not demo_mode:
            await monitor.start()
            maintenance_task = asyncio.create_task(maintenance.run(), name="maintenance")
        with db.session() as session:
            session.add(
                Event(
                    level="info",
                    kind="service_started",
                    message=(
                        "演示服务启动完成"
                        if demo_mode
                        else "服务启动完成，邮箱监听任务="
                        f"{sum(account.enabled for account in settings.mail.accounts)}"
                    ),
                )
            )
        logger.info("InboxPing 启动完成")
        try:
            yield
        finally:
            if maintenance_task is not None:
                maintenance_task.cancel()
                await asyncio.gather(maintenance_task, return_exceptions=True)
                await monitor.stop()
            with db.session() as session:
                session.add(Event(level="info", kind="service_stopped", message="服务已正常停止"))
            logger.info("InboxPing 已停止")

    app = FastAPI(title="InboxPing", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.web.session_secret.get_secret_value(),
        same_site="strict",
        https_only=False,
        max_age=session_days * 24 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    def authenticated(request: Request) -> bool:
        if not auth_enabled:
            return True
        session_user = request.session.get("user")
        valid = session_user == settings.web.username
        if valid and settings.web.session_rolling:
            # Reassigning marks the session as modified, refreshing both the
            # signed timestamp and the browser cookie expiration.
            request.session["user"] = session_user
        return valid

    def require_auth(request: Request) -> None:
        if not authenticated(request):
            raise HTTPException(status_code=401, detail="请先登录")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None, "username": ""})

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, username: str = Form(), password: str = Form()):
        password_digest = settings.web.password_hash.get_secret_value()
        valid = username == settings.web.username
        if valid and password_digest:
            try:
                valid = password_hash.verify(password, password_digest)
            except Exception:
                valid = False
        if valid:
            request.session["user"] = username
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误", "username": username},
            status_code=401,
        )

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "dashboard.html", page_context)

    @app.get("/messages/{message_id}", response_class=HTMLResponse)
    async def message_page(request: Request, message_id: int):
        require_auth(request)
        return templates.TemplateResponse(
            request,
            "message.html",
            {**page_context, "message_id": message_id},
        )

    @app.get("/api/v1/overview", response_model=OverviewResponse)
    async def api_overview(request: Request) -> OverviewResponse:
        require_auth(request)
        account_configs = {
            account.id: {"protocol": account.protocol, "username": account.username}
            for account in settings.mail.accounts
        }
        with db.session() as session:
            states = session.scalars(select(AccountState).order_by(AccountState.account_id)).all()
            events = session.scalars(select(Event).order_by(Event.created_at.desc()).limit(8)).all()
            stats = StatsResponse(
                total=session.scalar(select(func.count(Message.id))) or 0,
                pending=session.scalar(
                    select(func.count(Message.id)).where(Message.status == "pending")
                )
                or 0,
                fallback=session.scalar(
                    select(func.count(Message.id)).where(Message.status == "fallback")
                )
                or 0,
            )
            accounts = [
                account_response(state, account_configs.get(state.account_id, {}))
                for state in states
            ]
            recent_events = [event_response(event) for event in events]
        return OverviewResponse(
            stats=stats,
            services=ServicesResponse(
                ai_enabled=pipeline.ai.enabled,
                ai_model=settings.ai.model,
                ai_base_url=public_ai_base_url,
                ai_response_format=settings.ai.response_format,
                notifications_enabled=pipeline.notifier.enabled,
                notification_channels=pipeline.notifier.default_channel_ids,
            ),
            accounts=accounts,
            events=recent_events,
        )

    @app.get("/api/v1/messages", response_model=MessageListResponse)
    async def api_messages(
        request: Request,
        account: str | None = None,
        risk: Literal["high", "medium", "low", "unknown"] | None = None,
        push: bool = False,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> MessageListResponse:
        require_auth(request)
        conditions = []
        if account:
            conditions.append(Message.account_id == account)
        if risk:
            conditions.append(Message.analysis.has(Analysis.risk_level == risk))
        if push:
            conditions.append(Message.analysis.has(push_decision))
        query = (
            select(Message)
            .options(selectinload(Message.analysis))
            .where(*conditions)
            .order_by(
                func.coalesce(Message.sent_at, Message.received_at).desc(),
                Message.received_at.desc(),
                Message.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        with db.session() as session:
            total = session.scalar(select(func.count(Message.id)).where(*conditions)) or 0
            messages = session.scalars(query).all()
            items = [message_summary(message, settings) for message in messages]
        return MessageListResponse(items=items, total=total, limit=limit, offset=offset)

    @app.get("/api/v1/accounts", response_model=list[AccountResponse])
    async def api_accounts(request: Request) -> list[AccountResponse]:
        require_auth(request)
        account_configs = {
            account.id: {"protocol": account.protocol, "username": account.username}
            for account in settings.mail.accounts
        }
        with db.session() as session:
            states = session.scalars(select(AccountState).order_by(AccountState.account_id)).all()
            return [
                account_response(state, account_configs.get(state.account_id, {}))
                for state in states
            ]

    @app.get("/api/v1/events", response_model=list[EventResponse])
    async def api_events(
        request: Request, limit: int = Query(default=20, ge=1, le=200)
    ) -> list[EventResponse]:
        require_auth(request)
        with db.session() as session:
            events = session.scalars(
                select(Event).order_by(Event.created_at.desc()).limit(limit)
            ).all()
            return [event_response(event) for event in events]

    @app.get("/api/v1/messages/{message_id}", response_model=MessageDetailResponse)
    async def api_message_detail(request: Request, message_id: int) -> MessageDetailResponse:
        require_auth(request)
        with db.session() as session:
            message = session.scalar(
                select(Message)
                .options(
                    selectinload(Message.analysis),
                    selectinload(Message.translation),
                    selectinload(Message.notifications),
                )
                .where(Message.id == message_id)
            )
            if message is None:
                raise HTTPException(404, "邮件不存在")
            return serialize_message_detail(message, settings, notification_channels)

    @app.get("/api/v1/messages/{message_id}/export/eml")
    async def export_eml(request: Request, message_id: int) -> Response:
        require_auth(request)
        if demo_mode:
            raise HTTPException(403, "演示模式不会连接外部邮箱")
        try:
            fetched = await asyncio.to_thread(fetch_message_eml, settings, db, message_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        filename = f"{safe_filename(fetched.subject)}.eml"
        return Response(
            fetched.content,
            media_type="message/rfc822",
            headers={"Content-Disposition": attachment_header(filename)},
        )

    @app.get("/api/v1/messages/{message_id}/export/original.md")
    async def export_original_markdown(request: Request, message_id: int) -> Response:
        require_auth(request)
        with db.session() as session:
            message = session.get(Message, message_id)
            if message is None:
                raise HTTPException(404, "邮件不存在")
            filename = f"{safe_filename(message.subject)}-原文.md"
            return Response(
                render_original_markdown(message),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": attachment_header(filename)},
            )

    @app.get("/api/v1/messages/{message_id}/export/translation.md")
    async def export_translation_markdown(request: Request, message_id: int) -> Response:
        require_auth(request)
        with db.session() as session:
            message = session.scalar(
                select(Message)
                .options(selectinload(Message.translation))
                .where(Message.id == message_id)
            )
            if message is None:
                raise HTTPException(404, "邮件不存在")
            try:
                content = render_translation_markdown(message)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            filename = f"{safe_filename(message.subject)}-中文翻译.md"
            return Response(
                content,
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": attachment_header(filename)},
            )

    @app.post("/api/v1/messages/{message_id}/reanalyze", response_model=ActionResponse)
    async def api_reanalyze(request: Request, message_id: int) -> ActionResponse:
        require_auth(request)
        if demo_mode:
            raise HTTPException(403, "演示模式不会调用外部服务")
        try:
            await pipeline.process(message_id, force=True, notify=False)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return ActionResponse(status="completed", message_id=message_id)

    @app.post("/api/v1/messages/{message_id}/notify", response_model=ActionResponse)
    async def api_notify(request: Request, message_id: int) -> ActionResponse:
        require_auth(request)
        if demo_mode:
            raise HTTPException(403, "演示模式不会调用外部服务")
        try:
            await pipeline.send_notification(message_id, force=True)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except NotificationSuppressedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        return ActionResponse(status="completed", message_id=message_id)

    @app.post("/api/v1/messages/{message_id}/translate")
    async def api_translate(
        request: Request, message_id: int, force: bool = False
    ) -> StreamingResponse:
        require_auth(request)
        if demo_mode:
            raise HTTPException(403, "演示模式不会调用外部服务")

        async def stream():
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=32)

            async def produce() -> None:
                try:
                    async for chunk in pipeline.stream_translation(message_id, force=force):
                        await queue.put({"type": "delta", "text": chunk})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await queue.put({"type": "error", "message": str(exc)})
                else:
                    with db.session() as session:
                        message = session.get(Message, message_id)
                        translation = translation_response(message.translation) if message else None
                    await queue.put(
                        {
                            "type": "complete",
                            "translation": (
                                translation.model_dump(mode="json") if translation else None
                            ),
                        }
                    )

            task = asyncio.create_task(produce(), name=f"translate-{message_id}")
            yield _ndjson({"type": "start", "message_id": message_id})
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10)
                    except TimeoutError:
                        yield _ndjson({"type": "heartbeat"})
                        continue
                    yield _ndjson(event)
                    if event["type"] in {"complete", "error"}:
                        break
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.delete("/api/v1/messages/{message_id}/translation", response_model=ActionResponse)
    async def api_delete_translation(request: Request, message_id: int) -> ActionResponse:
        require_auth(request)
        if demo_mode:
            raise HTTPException(403, "演示模式不会修改演示数据")
        try:
            pipeline.delete_translation(message_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return ActionResponse(status="completed", message_id=message_id)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            with db.session() as session:
                session.execute(select(1))
            return {"status": "ready", "accounts": len(settings.mail.accounts)}
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).exception("数据库就绪检查失败")
            raise HTTPException(503, "database unavailable") from exc

    return app


def _ndjson(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
