from __future__ import annotations

import asyncio
import json
import random
from contextlib import suppress
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from inboxping.config import MailAccount, Settings
from inboxping.db import Database
from inboxping.mail.parser import normalize_utc, parse_message
from inboxping.mail.receivers import RawMessage, create_receiver
from inboxping.models import AccountState, Event, Message
from inboxping.services.pipeline import Pipeline


class MailMonitor:
    def __init__(self, settings: Settings, db: Database, pipeline: Pipeline):
        self.settings = settings
        self.db = db
        self.pipeline = pipeline
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for account in self.settings.mail.accounts:
            self._ensure_state(account)
            if account.enabled:
                self._tasks.append(
                    asyncio.create_task(
                        self._account_loop(account), name=f"mail-{account.protocol}-{account.id}"
                    )
                )
        logger.info("已启动 {} 个邮箱监听任务", len(self._tasks))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def sync_once(
        self, account_id: str, *, limit: int, schedule_analysis: bool = False
    ) -> list[int]:
        """Synchronize new messages for one account and return local message IDs."""
        account = next(
            (item for item in self.settings.mail.accounts if item.id == account_id), None
        )
        if account is None:
            raise LookupError(f"邮箱账户不存在：{account_id}")
        self._ensure_state(account)
        receiver = create_receiver(account, self.db, self.settings)
        try:
            raw_messages = await asyncio.to_thread(receiver.receive, limit=limit, wait=False)
            message_ids: list[int] = []
            for raw in raw_messages:
                status = "pending" if schedule_analysis else "stored"
                if message_id := self._save_message(account, raw, status=status):
                    message_ids.append(message_id)
            receiver.acknowledge()
        finally:
            receiver.close()
        return message_ids

    def _ensure_state(self, account: MailAccount) -> None:
        with self.db.session() as session:
            state = session.get(AccountState, account.id)
            if state is None:
                session.add(
                    AccountState(
                        account_id=account.id,
                        display_name=account.name,
                        status="starting" if account.enabled else "disabled",
                    )
                )
            else:
                state.display_name = account.name
                state.status = "starting" if account.enabled else "disabled"
                if not account.enabled:
                    state.last_error = None

    async def _account_loop(self, account: MailAccount) -> None:
        failures = 0
        receiver = create_receiver(account, self.db, self.settings)
        try:
            while not self._stop.is_set():
                try:
                    raw_messages = await asyncio.to_thread(
                        receiver.receive,
                        limit=self.settings.monitor.initial_sync_limit,
                        wait=True,
                    )
                    failures = 0
                    message_ids: list[int] = []
                    for raw in raw_messages:
                        message_id = self._save_message(account, raw)
                        if message_id:
                            message_ids.append(message_id)
                    receiver.acknowledge()
                    for message_id in message_ids:
                        await self.pipeline.process(message_id)
                    if account.protocol in {"imap_poll", "pop3_poll"}:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.settings.monitor.poll_interval_seconds
                        )
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    receiver.close()
                    delay = min(300, (2 ** min(failures, 7)) + random.random() * 3)
                    self._set_error(account, exc)
                    logger.bind(account=account.id, protocol=account.protocol).warning(
                        "邮箱监听异常，{:.1f}s 后重试: {}", delay, exc
                    )
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
        finally:
            receiver.close()

    def _save_message(
        self, account: MailAccount, raw: RawMessage, *, status: str = "pending"
    ) -> int | None:
        if raw.body is None:
            with self.db.session() as session:
                state = session.get(AccountState, account.id)
                assert state is not None
                state.last_uid = max(state.last_uid, raw.uid)
                session.add(
                    Event(
                        level="warning",
                        kind="mail_skipped",
                        account_id=account.id,
                        message=(
                            f"邮件超过大小上限，已跳过，UID={raw.uid}，"
                            f"大小={raw.size} 字节"
                        ),
                    )
                )
            logger.bind(account=account.id, uid=raw.uid).warning(
                "邮件超过大小上限，已跳过（大小={} 字节）", raw.size
            )
            return None
        parsed = parse_message(raw.body, self.settings.analysis.body_max_chars)
        try:
            with self.db.session() as session:
                existing = session.scalar(
                    select(Message.id).where(
                        Message.account_id == account.id,
                        Message.folder == raw.folder,
                        Message.uid_validity == raw.uid_validity,
                        Message.uid == raw.uid,
                    )
                )
                state = session.get(AccountState, account.id)
                assert state is not None
                state.last_uid = max(state.last_uid, raw.uid)
                state.last_received_at = datetime.now(UTC)
                if existing:
                    return None
                message = Message(
                    account_id=account.id,
                    folder=raw.folder,
                    uid_validity=raw.uid_validity,
                    uid=raw.uid,
                    message_id=parsed.message_id,
                    subject=parsed.subject,
                    sender_name=parsed.sender_name,
                    sender_address=parsed.sender_address,
                    recipients=parsed.recipients,
                    sent_at=normalize_utc(parsed.sent_at),
                    text_body=parsed.text_body,
                    attachments_json=parsed.attachments_json(),
                    authentication_json=json.dumps(parsed.authentication, ensure_ascii=False),
                    status=status,
                )
                session.add(message)
                session.flush()
                session.add(
                    Event(
                        level="info",
                        kind="mail_received",
                        account_id=account.id,
                        message=(f"邮件已入库，本地ID={message.id}，UID={raw.uid}，状态={status}"),
                    )
                )
                logger.bind(
                    account=account.id,
                    message_id=message.id,
                    uid=raw.uid,
                ).info("邮件已入库: {}（状态={}）", message.subject, status)
                return message.id
        except IntegrityError:
            return None

    def _set_error(self, account: MailAccount, exc: Exception) -> None:
        with self.db.session() as session:
            state = session.get(AccountState, account.id)
            if state:
                state.status = "error"
                state.last_error = str(exc)[:1000]
            session.add(
                Event(
                    level="error",
                    kind=account.protocol,
                    account_id=account.id,
                    message=str(exc)[:2000],
                )
            )
