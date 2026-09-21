from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import Message as EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup


@dataclass
class ParsedMail:
    message_id: str | None
    subject: str
    sender_name: str
    sender_address: str
    recipients: str
    sent_at: datetime | None
    text_body: str
    attachments: list[dict[str, object]] = field(default_factory=list)
    authentication: dict[str, str] = field(default_factory=dict)

    def attachments_json(self) -> str:
        return json.dumps(self.attachments, ensure_ascii=False)


def normalize_utc(value: datetime | None) -> datetime | None:
    """Store datetimes as naive UTC because SQLite does not preserve offsets."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeDecodeError):
        return value.strip()


def _payload_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "head", "noscript"]):
        node.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()


def parse_message(raw: bytes, max_chars: int = 20_000) -> ParsedMail:
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    from_addresses = getaddresses([mail.get("From", "")])
    sender_name, sender_address = from_addresses[0] if from_addresses else ("", "")
    recipients = ", ".join(
        address for _, address in getaddresses(mail.get_all("To", []) + mail.get_all("Cc", []))
    )

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, object]] = []
    for part in mail.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = _decode(part.get_filename())
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": filename or "unnamed",
                    "content_type": part.get_content_type(),
                    "size": len(payload),
                }
            )
            continue
        content = _payload_text(part)
        if part.get_content_type() == "text/plain":
            text_parts.append(content)
        elif part.get_content_type() == "text/html":
            html_parts.append(content)

    html_body = "\n".join(html_parts)
    text_body = "\n".join(text_parts).strip() or html_to_text(html_body)
    text_body = text_body[:max_chars]
    authentication_headers = " ".join(mail.get_all("Authentication-Results", []))
    authentication: dict[str, str] = {}
    for method in ("spf", "dkim", "dmarc"):
        if match := re.search(
            rf"(?:^|[;\s]){method}=([a-zA-Z]+)", authentication_headers, re.IGNORECASE
        ):
            authentication[method] = match.group(1).lower()

    sent_at = None
    if date_header := mail.get("Date"):
        try:
            sent_at = parsedate_to_datetime(date_header)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            pass

    return ParsedMail(
        message_id=(mail.get("Message-ID") or "").strip() or None,
        subject=_decode(mail.get("Subject")) or "(无主题)",
        sender_name=_decode(sender_name),
        sender_address=sender_address.strip().lower(),
        recipients=recipients,
        sent_at=sent_at,
        text_body=text_body,
        attachments=attachments,
        authentication=authentication,
    )
