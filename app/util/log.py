import logging
import traceback
from collections import deque
from datetime import datetime
from typing import cast

import structlog
from pydantic import BaseModel
from structlog.typing import EventDict
from typing_extensions import override

from app.internal.env_settings import Settings

BUFFER_SIZE = 2000
"""Log lines kept in memory for the System > Logs page."""


class LogLine(BaseModel):
    time: str
    level: str
    event: str
    fields: dict[str, str]
    exception: str | None = None


recent: deque[LogLine] = deque(maxlen=BUFFER_SIZE)

_keep_debug = Settings().app.log_level.strip().upper() == "DEBUG"

_SKIP = {"event", "level", "timestamp", "exc_info", "exception", "stack_info"}


def _short(value: object) -> str:
    text = str(value)
    return text if len(text) <= 500 else text[:500] + "…"


def _remember(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Keeps a copy of each line before it is printed.

    Debug lines are only kept when the log level asks for them; otherwise one
    busy page of auth checks would push everything useful out of the buffer.
    """
    del logger
    if method_name == "debug" and not _keep_debug:
        return event_dict
    values = cast(dict[str, object], event_dict)
    exception = None
    exc_info = values.get("exc_info")
    if isinstance(exc_info, BaseException):
        exception = "".join(traceback.format_exception(exc_info))
    elif isinstance(exc_info, tuple) and exc_info and exc_info[0] is not None:
        exception = "".join(traceback.format_exception(*exc_info))  # pyright: ignore[reportUnknownArgumentType]
    recent.append(
        LogLine(
            time=str(
                values.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ),
            level=str(values.get("level", "info")),
            event=_short(values.get("event", "")),
            fields={k: _short(v) for k, v in values.items() if k not in _SKIP},
            exception=exception,
        )
    )
    return event_dict


class _BufferHandler(logging.Handler):
    """Catches warnings and errors from libraries that use plain logging."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        recent.append(
            LogLine(
                time=datetime.fromtimestamp(record.created).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                level=record.levelname.lower(),
                event=_short(record.getMessage()),
                fields={"logger": record.name},
                exception="".join(traceback.format_exception(*record.exc_info))
                if record.exc_info and record.exc_info[0] is not None
                else None,
            )
        )


_handler = _BufferHandler(level=logging.WARNING)
if not any(isinstance(h, _BufferHandler) for h in logging.getLogger().handlers):
    logging.getLogger().addHandler(_handler)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        _remember,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
)

logger = structlog.stdlib.get_logger()
