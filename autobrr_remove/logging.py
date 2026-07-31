from __future__ import annotations

import logging
import logging.config
from typing import Any

import structlog
from structlog.contextvars import merge_contextvars
from structlog.processors import CallsiteParameter, CallsiteParameterAdder
from structlog.typing import EventDict, Processor, WrappedLogger

from autobrr_remove.config import LoggingConfig


def _normalize_fields(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Normalize fields to the same shape used by wi1-bot."""
    event_dict["level"] = str(event_dict["level"]).upper()
    event_dict["src"] = f"{event_dict.pop('func_name')}:{event_dict.pop('lineno')}"

    if "exception" in event_dict:
        event_dict["exc_info"] = event_dict.pop("exception")

    return event_dict


def setup_logging(cfg: LoggingConfig) -> None:
    """Configure structlog and stdlib logging with a shared structured renderer."""
    shared_processors: list[Processor] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(
            fmt="%Y-%m-%d %H:%M:%S",
            utc=False,
            key="ts",
        ),
        CallsiteParameterAdder(
            [
                CallsiteParameter.FUNC_NAME,
                CallsiteParameter.LINENO,
            ]
        ),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _normalize_fields,
        structlog.processors.EventRenamer("msg"),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    renderers: dict[str, Processor] = {
        "logfmt": structlog.processors.LogfmtRenderer(
            key_order=["ts", "level", "logger", "src", "msg"],
            drop_missing=True,
            bool_as_flag=False,
        ),
        "json": structlog.processors.JSONRenderer(),
    }

    handlers: dict[str, Any] = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "level": cfg.level,
            "formatter": cfg.format,
        },
    }
    root_handlers = ["console"]

    if cfg.file is not None:
        cfg.file.parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(cfg.file),
            "maxBytes": 10 * 1024**2,  # 10 MiB
            "backupCount": cfg.file_count,
            "level": cfg.level,
            "formatter": cfg.format,
        }
        root_handlers.append("file")

    logging_config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            output_format: {
                "()": structlog.stdlib.ProcessorFormatter,
                "foreign_pre_chain": shared_processors,
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    renderer,
                ],
            }
            for output_format, renderer in renderers.items()
        },
        "handlers": handlers,
        "loggers": {
            "": {
                "level": cfg.level,
                "handlers": root_handlers,
            },
            "autobrr_remove": {
                "level": cfg.level,
                "handlers": [],
                "propagate": True,
            },
        },
    }

    logging.config.dictConfig(logging_config)
