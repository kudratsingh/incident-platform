import json
import logging
import os
import time
from contextvars import ContextVar
from typing import Any

# Per-request context propagated through the call stack via contextvars.
# Workers read these same vars so every log line carries the full trace.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")
job_id_var: ContextVar[str] = ContextVar("job_id", default="")


class JSONFormatter(logging.Formatter):
    """Emits one JSON object per log line with all structured fields included."""

    # Fields that are already captured elsewhere or not useful in JSON output
    _SKIP: frozenset[str] = frozenset(
        {
            "name", "msg", "args", "created", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs", "pathname",
            "process", "processName", "relativeCreated", "stack_info",
            "thread", "threadName", "exc_info", "exc_text", "message",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()

        log_entry: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            )
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "caller": f"{record.filename}:{record.lineno}",
            "message": record.message,
            # Structured context from the current async task / request
            "request_id": request_id_var.get("") or None,
            "trace_id": trace_id_var.get("") or None,
            "user_id": user_id_var.get("") or None,
            "job_id": job_id_var.get("") or None,
        }

        # Attach any extra= fields passed by the caller
        for key, value in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                log_entry[key] = value

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Drop None values to keep output compact
        return json.dumps({k: v for k, v in log_entry.items() if v is not None})


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    formatter = JSONFormatter()

    # Always log to stdout
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream_handler]

    # Optionally also write to a file (creates parent dirs automatically)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True) if os.path.dirname(log_file) else None
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.root.setLevel(level)
    logging.root.handlers = handlers

    # Route uvicorn loggers through our root handler so they emit JSON too
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Suppress uvicorn access logs — our RequestContextMiddleware handles request logging
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_uvicorn_log_config(level: str = "INFO") -> dict[str, Any]:
    """Return a uvicorn log_config dict that disables uvicorn's default handlers.

    Pass this to uvicorn.run() or the --log-config flag so uvicorn does not
    install its own ColorFormatter, letting our root JSONFormatter take over.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {},
        "loggers": {
            "uvicorn": {"handlers": [], "propagate": True, "level": level},
            "uvicorn.error": {"handlers": [], "propagate": True, "level": level},
            "uvicorn.access": {"handlers": [], "propagate": False, "level": "WARNING"},
        },
    }


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
