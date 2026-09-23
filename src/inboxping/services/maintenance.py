from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select

from inboxping.config import Settings
from inboxping.db import Database
from inboxping.models import Analysis, Message, Notification
from inboxping.services.pipeline import Pipeline
from inboxping.services.push_policy import push_decision_expression


class MaintenanceWorker:
    """Recover interrupted analysis jobs and retry failed deliveries."""

    def __init__(self, settings: Settings, db: Database, pipeline: Pipeline):
        self.settings = settings
        self.db = db
        self.pipeline = pipeline

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("后台维护任务失败")
            await asyncio.sleep(300)

    async def run_once(self) -> None:
        pending_cutoff = datetime.now(UTC) - timedelta(minutes=2)
        with self.db.session() as session:
            pending_ids = list(
                session.scalars(
                    select(Message.id).where(
                        Message.status == "pending", Message.created_at < pending_cutoff
                    )
                ).all()
            )
            retry_items = list(
                session.execute(
                    select(Notification.message_id, Notification.channel)
                    .join(Message, Notification.message_id == Message.id)
                    .join(Analysis, Analysis.message_id == Message.id)
                    .where(
                        Notification.status == "failed",
                        Notification.attempts < self.settings.notifications.retry.max_attempts,
                        push_decision_expression(),
                    )
                ).all()
            )
        for message_id in pending_ids:
            await self.pipeline.process(message_id)
        for message_id, channel_id in retry_items:
            try:
                await self.pipeline.send_notification(message_id, channel_id=channel_id)
            except Exception:
                logger.bind(message_id=message_id, channel=channel_id).exception("通知后台重试失败")
        if pending_ids or retry_items:
            logger.info(
                "维护完成: 恢复={}，通知重试={}",
                len(pending_ids),
                len(retry_items),
            )
