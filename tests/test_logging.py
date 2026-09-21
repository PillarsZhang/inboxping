import logging

from inboxping.config import Settings
from inboxping.logging import SuccessfulHealthCheckFilter, configure_logging, text_log_format


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


def test_successful_health_checks_are_suppressed() -> None:
    log_filter = SuccessfulHealthCheckFilter()
    successful_probe = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/health/live", "1.1", 200),
        None,
    )
    failed_probe = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/health/live", "1.1", 503),
        None,
    )
    normal_request = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/api/v1/overview", "1.1", 200),
        None,
    )

    assert not log_filter.filter(successful_probe)
    assert log_filter.filter(failed_probe)
    assert log_filter.filter(normal_request)
