from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from inboxping.mail.recipients import RecipientSummary, summarize_recipients
from inboxping.models import AccountState, Analysis, Event, Message, Notification, Translation


def utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class StatsResponse(BaseModel):
    total: int
    pending: int
    failed: int


class ServicesResponse(BaseModel):
    ai_enabled: bool
    ai_model: str
    ai_base_url: str
    ai_response_format: str
    notifications_enabled: bool
    notification_channels: list[str]


class AccountResponse(BaseModel):
    id: str
    name: str
    username: str
    protocol: str
    status: str
    last_uid: int
    last_connected_at: datetime | None
    last_received_at: datetime | None
    last_error: str | None


class EventResponse(BaseModel):
    id: int
    level: str
    kind: str
    account_id: str | None
    message: str
    created_at: datetime


class OverviewResponse(BaseModel):
    stats: StatsResponse
    services: ServicesResponse
    accounts: list[AccountResponse]
    events: list[EventResponse]


class AnalysisResponse(BaseModel):
    title: str
    summary: str
    category: str
    importance_score: float | None
    risk_score: float | None
    risk_reason: str
    push_reason: str
    action_required: bool
    action_text: str
    deadline: str | None
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


class MessageSummaryResponse(BaseModel):
    id: int
    account_id: str
    subject: str
    sender_name: str
    sender_address: str
    recipient_count: int
    recipient_preview: str
    sent_at: datetime | None
    received_at: datetime
    displayed_at: datetime
    status: str
    should_push: bool | None
    analysis: AnalysisResponse | None


class NotificationResponse(BaseModel):
    channel: str
    channel_type: str
    target: str
    status: str
    attempts: int
    sent_at: datetime | None
    last_error: str | None


class TranslationResponse(BaseModel):
    text: str
    target_language: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    updated_at: datetime


class MessageDetailResponse(MessageSummaryResponse):
    uid: int
    folder: str
    recipient_addresses: list[str]
    text_body: str
    translation: TranslationResponse | None
    notifications: list[NotificationResponse]


class MessageListResponse(BaseModel):
    items: list[MessageSummaryResponse]
    total: int
    limit: int
    offset: int


class ActionResponse(BaseModel):
    status: str
    message_id: int


def analysis_response(analysis: Analysis | None) -> AnalysisResponse | None:
    if analysis is None:
        return None
    return AnalysisResponse(
        title=analysis.title_zh,
        summary=analysis.summary_zh,
        category=analysis.category,
        importance_score=analysis.importance_score,
        risk_score=analysis.risk_score,
        risk_reason=analysis.risk_reason,
        push_reason=analysis.push_reason,
        action_required=analysis.action_required,
        action_text=analysis.action_text,
        deadline=analysis.deadline,
        model=analysis.model,
        prompt_tokens=analysis.prompt_tokens,
        completion_tokens=analysis.completion_tokens,
    )


def message_summary(
    message: Message, recipients: RecipientSummary | None = None
) -> MessageSummaryResponse:
    sent_at = utc_datetime(message.sent_at)
    received_at = utc_datetime(message.received_at)
    assert received_at is not None
    recipient_summary = recipients or summarize_recipients(message.recipients or "")
    return MessageSummaryResponse(
        id=message.id,
        account_id=message.account_id,
        subject=message.subject,
        sender_name=message.sender_name,
        sender_address=message.sender_address,
        recipient_count=recipient_summary.count,
        recipient_preview=recipient_summary.preview,
        sent_at=sent_at,
        received_at=received_at,
        displayed_at=sent_at or received_at,
        status=message.status,
        should_push=message.analysis.should_push if message.analysis else None,
        analysis=analysis_response(message.analysis),
    )


def account_response(state: AccountState, account_config: dict[str, Any]) -> AccountResponse:
    return AccountResponse(
        id=state.account_id,
        name=state.display_name,
        username=account_config.get("username", ""),
        protocol=account_config.get("protocol", "unknown"),
        status=state.status,
        last_uid=state.last_uid,
        last_connected_at=utc_datetime(state.last_connected_at),
        last_received_at=utc_datetime(state.last_received_at),
        last_error=state.last_error,
    )


def event_response(event: Event) -> EventResponse:
    created_at = utc_datetime(event.created_at)
    assert created_at is not None
    return EventResponse(
        id=event.id,
        level=event.level,
        kind=event.kind,
        account_id=event.account_id,
        message=event.message,
        created_at=created_at,
    )


def notification_response(
    notification: Notification, channels: dict[str, dict[str, str]]
) -> NotificationResponse:
    channel = channels.get(notification.channel, {})
    return NotificationResponse(
        channel=notification.channel,
        channel_type=channel.get("type", "未知通道"),
        target=channel.get("target", ""),
        status=notification.status,
        attempts=notification.attempts,
        sent_at=utc_datetime(notification.sent_at),
        last_error=notification.last_error,
    )


def translation_response(translation: Translation | None) -> TranslationResponse | None:
    if translation is None:
        return None
    updated_at = utc_datetime(translation.updated_at)
    assert updated_at is not None
    return TranslationResponse(
        text=translation.translated_text,
        target_language=translation.target_language,
        model=translation.model,
        prompt_tokens=translation.prompt_tokens,
        completion_tokens=translation.completion_tokens,
        updated_at=updated_at,
    )


def serialize_message_detail(
    message: Message, channels: dict[str, dict[str, str]]
) -> MessageDetailResponse:
    recipients = summarize_recipients(message.recipients or "")
    summary = message_summary(message, recipients)
    return MessageDetailResponse(
        **summary.model_dump(),
        uid=message.uid,
        folder=message.folder,
        recipient_addresses=list(recipients.addresses),
        text_body=message.text_body,
        translation=translation_response(message.translation),
        notifications=[notification_response(item, channels) for item in message.notifications],
    )
