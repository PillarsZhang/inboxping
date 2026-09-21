import logging
import sys

from loguru import logger

from inboxping.config import Settings

CONTEXT_FIELDS = (
    "account",
    "protocol",
    "uid",
    "uid_validity",
    "message_id",
    "channel",
    "notification_id",
    "model",
    "category",
    "risk",
    "importance",
)


def text_log_format(record: dict) -> str:
    context = " ".join(
        f"{key}={str(record['extra'][key]).replace(chr(10), ' ')[:120]}"
        for key in CONTEXT_FIELDS
        if key in record["extra"]
    )
    suffix = f" [{context}]" if context else ""
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        f"<level>{{message}}</level>{suffix}\n{{exception}}"
    )


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


class SuccessfulHealthCheckFilter(logging.Filter):
    """Suppress routine successful probes while retaining failures for diagnosis."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path = str(args[2]).partition("?")[0]
        try:
            status_code = int(args[4])
        except (TypeError, ValueError):
            return True
        return path != "/health/live" or status_code >= 400


def configure_logging(settings: Settings) -> None:
    logger.remove()
    sink_options = {
        "level": settings.logging.level.upper(),
        "serialize": settings.logging.json_output,
        "backtrace": False,
        "diagnose": False,
        "enqueue": True,
    }
    if not settings.logging.json_output:
        sink_options["format"] = text_log_format
    logger.add(sys.stderr, **sink_options)
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    # httpx logs complete URLs at INFO, while several APIs necessarily put tokens or
    # webhook keys in their query string. Never allow those request lines into logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        target = logging.getLogger(name)
        target.handlers = [InterceptHandler()]
        target.propagate = False
    access_handler = InterceptHandler()
    access_handler.addFilter(SuccessfulHealthCheckFilter())
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = [access_handler]
    access_logger.propagate = False
