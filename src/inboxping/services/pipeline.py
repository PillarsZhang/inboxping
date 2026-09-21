from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from inboxping.ai.client import AIClient
from inboxping.ai.prompts import load_prompt, load_translation_prompt, validate_prompt_files
from inboxping.ai.schema import AnalysisResult, Risk
from inboxping.config import Settings
from inboxping.db import Database
from inboxping.models import Analysis, Event, Message, Notification, Translation
from inboxping.notify.manager import NotificationManager
from inboxping.services.push_policy import notification_reasons, suppression_reason


class NotificationSuppressedError(RuntimeError):
    """Raised when an explicit delivery request violates the safety policy."""


def local_fallback(message: Message, reason: str) -> AnalysisResult:
    text = f"{message.subject}\n{message.text_body}".lower()
    risk_terms = (
        "验证码",
        "password",
        "密码",
        "转账",
        "汇款",
        "账号冻结",
        "verify account",
        "登录验证",
    )
    action_terms = ("截止", "请于", "必须", "提交", "确认", "deadline", "required")
    risky = any(term in text for term in risk_terms)
    action = any(term in text for term in action_terms)
    urls = re.findall(r"https?://[^\s<>]+", text)
    summary = (message.text_body.strip() or message.subject)[:180].replace("\n", " ")
    return AnalysisResult(
        title_zh=f"需人工查看：{message.subject}"[:80],
        summary_zh=f"[本地降级分析] {summary}",
        importance=0.75 if action or risky else 0.35,
        risk=Risk(
            level="medium" if risky else "unknown",
            types=["suspicious_request"] if risky else [],
            reason=f"AI 不可用（{reason[:80]}）；本地规则发现 {len(urls)} 个链接"
            if risky
            else "AI 不可用，仅完成本地基础检查",
        ),
        action_required=action,
        action_text="请人工核对邮件内容与发件人" if action or risky else "",
        push_recommended=action or risky,
    )


class Pipeline:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        validate_prompt_files(settings.analysis.prompt_dir)
        self.ai = AIClient(settings)
        self.notifier = NotificationManager(settings)
        # Completed messages must not leave one lock per database row forever.
        self._translation_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def process(
        self, message_id: int, *, force: bool = False, notify: bool = True
    ) -> Analysis:
        with self.db.session() as session:
            message = session.get(Message, message_id)
            if message is None:
                raise LookupError(f"邮件 {message_id} 不存在")
            log = logger.bind(
                account=message.account_id,
                message_id=message.id,
                uid=message.uid,
            )
            if message.analysis and not force:
                analysis = message.analysis
                should_notify = self._should_notify(analysis)
                log.info("复用已有分析结果")
            else:
                analysis = None
                should_notify = False

        if analysis is None:
            log.bind(model=self.settings.ai.model).info("开始分析邮件")
            prompt = load_prompt(self.settings.analysis.prompt_dir)
            with self.db.session() as session:
                message = session.get(Message, message_id)
                assert message is not None
            try:
                result = await self.ai.analyze(message, prompt)
                model = self.settings.ai.model
                analysis_error = None
            except Exception as exc:
                log.exception("AI 分析失败，使用本地规则降级")
                result = local_fallback(message, str(exc))
                model = "local-fallback"
                analysis_error = str(exc)

            with self.db.session() as session:
                message = session.get(Message, message_id)
                if message is None:
                    raise LookupError(f"邮件 {message_id} 在分析期间被删除")
                analysis = message.analysis or Analysis(message_id=message.id)
                analysis.model = model
                analysis.prompt_version = prompt.version
                analysis.title_zh = result.title_zh
                analysis.summary_zh = result.summary_zh
                analysis.category = result.category
                analysis.importance = result.importance
                analysis.risk_level = result.risk.level
                analysis.risk_types_json = json.dumps(result.risk.types, ensure_ascii=False)
                analysis.risk_reason = result.risk.reason
                analysis.action_required = result.action_required
                analysis.action_text = result.action_text
                analysis.deadline = result.deadline
                analysis.push_recommended = result.push_recommended
                analysis.raw_json = result.model_dump_json()
                if not message.analysis:
                    session.add(analysis)
                message.status = "analyzed" if analysis_error is None else "fallback"
                message.error = analysis_error
                should_notify = self._should_notify(analysis)
                session.add(
                    Event(
                        level="info" if analysis_error is None else "warning",
                        kind="analysis_completed",
                        account_id=message.account_id,
                        message=(
                            f"邮件分析完成，本地ID={message.id}，UID={message.uid}，"
                            f"分类={analysis.category}，风险={analysis.risk_level}，模型={model}"
                        ),
                    )
                )
                log.bind(
                    model=model,
                    category=analysis.category,
                    risk=analysis.risk_level,
                    importance=f"{analysis.importance:.2f}",
                ).info("邮件分析完成")

        suppression_reason = self._notification_suppression_reason(analysis)
        reasons = self._notification_reasons(analysis)
        if not notify:
            log.info("通知决策: 不发送（本次处理关闭通知）")
        elif suppression_reason:
            log.info("通知决策: 不发送（{}）", suppression_reason)
        elif should_notify:
            log.info("通知决策: 发送（{}）", "、".join(reasons))
        else:
            log.info("通知决策: 不发送（未命中推送规则）")

        if notify and should_notify:
            try:
                await self.send_notification(message_id)
            except Exception:
                # 投递状态已入库并会独立重试，不能因此中断邮箱监听。
                logger.bind(message_id=message_id).exception("邮件通知未全部送达")
        return analysis

    async def stream_translation(
        self, message_id: int, *, force: bool = False
    ) -> AsyncIterator[str]:
        with self.db.session() as session:
            message = session.get(Message, message_id)
            if message is None:
                raise LookupError(f"邮件 {message_id} 不存在")
            if message.translation is not None and not force:
                yield message.translation.translated_text
                return
            if not message.text_body.strip():
                raise RuntimeError("邮件正文为空，无法翻译")
            account_id = message.account_id
            uid = message.uid

        lock = self._translation_locks.setdefault(message_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("该邮件正在翻译，请等待当前任务完成")

        prompt = load_translation_prompt(self.settings.analysis.prompt_dir)
        log = logger.bind(
            account=account_id,
            message_id=message_id,
            uid=uid,
            model=self.settings.ai.model,
        )
        started_at = time.monotonic()
        chunks: list[str] = []
        async with lock:
            with self.db.session() as session:
                session.add(
                    Event(
                        level="info",
                        kind="translation_started",
                        account_id=account_id,
                        message=(
                            f"开始流式翻译邮件，本地ID={message_id}，UID={uid}，"
                            f"模型={self.settings.ai.model}"
                        ),
                    )
                )
            log.info("开始流式翻译邮件全文")
            try:
                async for chunk in self.ai.stream_translation(message, prompt):
                    if not chunks:
                        log.info(
                            "收到首个翻译片段，首字延迟={:.2f}s",
                            time.monotonic() - started_at,
                        )
                    chunks.append(chunk)
                    yield chunk
            except asyncio.CancelledError:
                self._record_translation_failure(
                    account_id=account_id,
                    message_id=message_id,
                    uid=uid,
                    kind="translation_cancelled",
                    level="warning",
                    error="客户端断开连接",
                )
                log.warning("邮件全文翻译已取消（已接收字符={}）", len("".join(chunks)))
                raise
            except Exception as exc:
                self._record_translation_failure(
                    account_id=account_id,
                    message_id=message_id,
                    uid=uid,
                    kind="translation_failed",
                    level="error",
                    error=type(exc).__name__,
                )
                log.exception("邮件全文翻译失败（已接收字符={}）", len("".join(chunks)))
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(f"AI 翻译请求失败（{type(exc).__name__}）") from exc

            translated_text = "".join(chunks).strip()
            if not translated_text:
                self._record_translation_failure(
                    account_id=account_id,
                    message_id=message_id,
                    uid=uid,
                    kind="translation_failed",
                    level="error",
                    error="EmptyResponse",
                )
                raise RuntimeError("AI 没有返回译文")
            with self.db.session() as session:
                message = session.get(Message, message_id)
                if message is None:
                    raise LookupError(f"邮件 {message_id} 在翻译期间被删除")
                translation = message.translation or Translation(message_id=message.id)
                translation.model = self.settings.ai.model
                translation.prompt_version = prompt.version
                translation.target_language = "zh-CN"
                translation.translated_text = translated_text
                if message.translation is None:
                    session.add(translation)
                session.add(
                    Event(
                        level="info",
                        kind="translation_completed",
                        account_id=message.account_id,
                        message=(
                            f"邮件全文翻译完成，本地ID={message.id}，UID={message.uid}，"
                            f"模型={translation.model}"
                        ),
                    )
                )
            log.info(
                "邮件全文翻译完成（耗时={:.2f}s，字符={}）",
                time.monotonic() - started_at,
                len(translated_text),
            )

    async def translate_message(self, message_id: int, *, force: bool = False) -> Translation:
        async for _ in self.stream_translation(message_id, force=force):
            pass
        with self.db.session() as session:
            message = session.get(Message, message_id)
            if message is None or message.translation is None:
                raise RuntimeError("翻译完成但未找到缓存结果")
            return message.translation

    def delete_translation(self, message_id: int) -> bool:
        lock = self._translation_locks.get(message_id)
        if lock is not None and lock.locked():
            raise RuntimeError("该邮件正在翻译，完成或取消后才能删除")
        with self.db.session() as session:
            message = session.get(Message, message_id)
            if message is None:
                raise LookupError(f"邮件 {message_id} 不存在")
            if message.translation is None:
                return False
            session.delete(message.translation)
            session.add(
                Event(
                    level="info",
                    kind="translation_deleted",
                    account_id=message.account_id,
                    message=f"已删除邮件翻译，本地ID={message.id}，UID={message.uid}",
                )
            )
            account_id = message.account_id
            uid = message.uid
        logger.bind(account=account_id, message_id=message_id, uid=uid).info("已删除邮件翻译")
        return True

    def _record_translation_failure(
        self,
        *,
        account_id: str,
        message_id: int,
        uid: int,
        kind: str,
        level: str,
        error: str,
    ) -> None:
        with self.db.session() as session:
            session.add(
                Event(
                    level=level,
                    kind=kind,
                    account_id=account_id,
                    message=(f"邮件全文翻译未完成，本地ID={message_id}，UID={uid}，错误={error}"),
                )
            )

    def _should_notify(self, analysis: Analysis) -> bool:
        return bool(self._notification_reasons(analysis))

    def _notification_suppression_reason(self, analysis: Analysis) -> str | None:
        return suppression_reason(analysis, self.settings)

    def _notification_reasons(self, analysis: Analysis) -> list[str]:
        return notification_reasons(analysis, self.settings)

    async def send_notification(
        self, message_id: int, *, force: bool = False, channel_id: str | None = None
    ) -> None:
        with self.db.session() as session:
            message = session.get(Message, message_id)
            if message is None or message.analysis is None:
                raise LookupError("邮件或分析结果不存在")
            suppression_reason = self._notification_suppression_reason(message.analysis)
            account_id = message.account_id
            uid = message.uid
        if suppression_reason:
            logger.bind(account=account_id, message_id=message_id, uid=uid).info(
                "通知已抑制: {}", suppression_reason
            )
            raise NotificationSuppressedError(suppression_reason)

        channel_ids = [channel_id] if channel_id else self.notifier.default_channel_ids
        if not channel_ids:
            raise RuntimeError("没有配置默认通知通道")
        failures: list[str] = []
        for current_channel_id in channel_ids:
            try:
                await self._send_to_channel(message_id, current_channel_id, force=force)
            except Exception as exc:
                failures.append(f"{current_channel_id}: {exc}")
        if failures:
            raise RuntimeError("；".join(failures))

    async def _send_to_channel(
        self, message_id: int, channel_id: str, *, force: bool = False
    ) -> None:
        with self.db.session() as session:
            message = session.get(Message, message_id)
            if message is None or message.analysis is None:
                raise LookupError("邮件或分析结果不存在")
            notification = session.scalar(
                select(Notification).where(
                    Notification.message_id == message_id,
                    Notification.channel == channel_id,
                )
            )
            if notification and notification.status == "sent" and not force:
                logger.bind(message_id=message_id, channel=channel_id).info(
                    "通知已发送，跳过重复投递"
                )
                return
            if notification is None:
                notification = Notification(message_id=message_id, channel=channel_id)
                session.add(notification)
                session.flush()
            notification.attempts += 1
            notification_id = notification.id
            attempt = notification.attempts

        try:
            logger.bind(
                message_id=message_id,
                channel=channel_id,
                notification_id=notification_id,
            ).info("开始发送通知（第 {} 轮）", attempt)
            with self.db.session() as session:
                message = session.scalar(
                    select(Message)
                    .options(selectinload(Message.analysis))
                    .where(Message.id == message_id)
                )
                if message is None or message.analysis is None:
                    raise LookupError("邮件或分析结果不存在")
            await self.notifier.send(channel_id, message, message.analysis)
            with self.db.session() as session:
                notification = session.get(Notification, notification_id)
                assert notification is not None
                notification.status = "sent"
                notification.sent_at = datetime.now(UTC)
                notification.last_error = None
                session.add(
                    Event(
                        level="info",
                        kind="notification_sent",
                        account_id=message.account_id,
                        message=(
                            f"通知发送成功，本地ID={message.id}，UID={message.uid}，"
                            f"通道={channel_id}"
                        ),
                    )
                )
            logger.bind(
                message_id=message_id,
                channel=channel_id,
                notification_id=notification_id,
            ).info("通知发送成功")
        except Exception as exc:
            with self.db.session() as session:
                notification = session.get(Notification, notification_id)
                assert notification is not None
                notification.status = "failed"
                notification.last_error = str(exc)
                session.add(Event(level="error", kind="notification", message=str(exc)))
            logger.bind(message_id=message_id, channel=channel_id).warning("通知发送失败: {}", exc)
            raise
