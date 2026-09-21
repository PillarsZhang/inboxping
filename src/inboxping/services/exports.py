from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import quote

import yaml

from inboxping.models import Message

SHANGHAI = timezone(timedelta(hours=8))


def safe_filename(value: str, fallback: str = "email") -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE).strip("-_")
    return (cleaned[:60] or fallback).rstrip("-_")


def attachment_header(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"


def render_original_markdown(message: Message) -> str:
    return _render_markdown(
        message=message,
        body=message.text_body,
        export_type="original",
    )


def render_translation_markdown(message: Message) -> str:
    if message.translation is None:
        raise LookupError("该邮件尚未生成中文翻译")
    return _render_markdown(
        message=message,
        body=message.translation.translated_text,
        export_type="translation",
    )


def _render_markdown(
    *,
    message: Message,
    body: str,
    export_type: str,
) -> str:
    metadata: dict[str, object] = {
        "subject": message.subject,
        "sender_name": message.sender_name or None,
        "sender_address": message.sender_address or None,
        "recipients": message.recipients or None,
        "sent_at": _iso_datetime(message.sent_at),
        "received_at": _iso_datetime(message.received_at),
        "message_id": message.message_id,
        "content_type": export_type,
    }
    heading = message.subject
    if export_type == "translation":
        translation = message.translation
        assert translation is not None
        heading = f"{heading}（中文翻译）"
        metadata.update(
            {
                "target_language": translation.target_language,
                "translation_model": translation.model,
                "translation_prompt_version": translation.prompt_version,
                "translated_at": _iso_datetime(translation.updated_at),
            }
        )
    front_matter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    safe_body = html.escape(body or "没有可显示的正文。", quote=False)
    return f"---\n{front_matter}\n---\n\n# {_escape_inline(heading)}\n\n{safe_body}\n"


def _escape_inline(value: str) -> str:
    compact = " ".join(value.splitlines()).strip()
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", compact)


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")
