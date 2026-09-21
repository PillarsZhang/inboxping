import logging

from inboxping.config import Settings
from inboxping.logging import configure_logging, text_log_format


def test_http_client_request_logs_are_suppressed() -> None:
    configure_logging(Settings())
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_text_log_format_renders_mail_context() -> None:
    rendered = text_log_format(
        {"extra": {"account": "university", "message_id": 42, "channel": "wecom_app"}}
    )
    assert "[account=university message_id=42 channel=wecom_app]" in rendered


def test_json_logging_can_be_configured() -> None:
    configure_logging(Settings(logging={"json": True}))
