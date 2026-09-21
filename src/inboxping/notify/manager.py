from __future__ import annotations

from typing import Protocol

from inboxping.config import Settings, WeComAppChannel, WeComWebhookChannel
from inboxping.models import Analysis, Message
from inboxping.notify.wecom import WeComAppNotifier, WeComUser, WeComWebhookNotifier


class Notifier(Protocol):
    id: str
    type: str

    async def send(self, message: Message, analysis: Analysis) -> None: ...

    async def send_text(self, content: str) -> None: ...


class NotificationManager:
    def __init__(self, settings: Settings):
        self.default_channel_ids = settings.notifications.default_channels
        self.channels: dict[str, Notifier] = {}
        for config in settings.notifications.channels:
            if not config.enabled:
                continue
            if isinstance(config, WeComWebhookChannel):
                notifier: Notifier = WeComWebhookNotifier(config)
            elif isinstance(config, WeComAppChannel):
                notifier = WeComAppNotifier(config)
            else:  # pragma: no cover
                raise TypeError(f"不支持的通知通道类型: {config.type}")
            self.channels[config.id] = notifier

    @property
    def enabled(self) -> bool:
        return bool(self.default_channel_ids)

    def resolve(self, channel_id: str) -> Notifier:
        try:
            return self.channels[channel_id]
        except KeyError as exc:
            raise LookupError(f"通知通道不存在或未启用: {channel_id}") from exc

    async def send(self, channel_id: str, message: Message, analysis: Analysis) -> None:
        notifier = self.resolve(channel_id)
        await notifier.send(message, analysis)

    async def send_text(self, channel_id: str, content: str) -> None:
        notifier = self.resolve(channel_id)
        await notifier.send_text(content)

    async def list_wecom_users(self, channel_id: str) -> list[WeComUser]:
        notifier = self.resolve(channel_id)
        if not isinstance(notifier, WeComAppNotifier):
            raise TypeError(f"通道 {channel_id} 不是 wecom_app，无法查询成员")
        return await notifier.list_users()
