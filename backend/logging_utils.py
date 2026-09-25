"""
logging_utils.py — structured (JSON) logging with call correlation (VA-A5
fix).

main.py used to call logging.basicConfig with a plain text format, and
every call site that wanted a call id in its log line had to remember to
write it as an f-string prefix (logger.info(f"[{call_id}] ...")). That
means there's no way to *query* logs by call_id — only grep the message
text — and any log line from code that forgot the prefix (or that runs
deep inside brain/orchestrator/rag, far from the call_id variable) has no
correlation at all.

Instead, call_id (and turn_id, for the one active turn) are bound once via
a contextvar at the natural start of a call/turn (voice/stream.py) and a
logging.Filter stamps them onto every LogRecord emitted anywhere during
that call — including from modules that never see call_id as an argument —
without touching the ~60 individual message strings. The JSON formatter
then emits both as real, queryable fields alongside the human-readable
message.
"""
import json
import logging
from contextvars import ContextVar
from typing import Optional

call_id_ctx: ContextVar[Optional[str]] = ContextVar("call_id", default=None)
turn_id_ctx: ContextVar[Optional[str]] = ContextVar("turn_id", default=None)


class CallCorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = call_id_ctx.get()
        record.turn_id = turn_id_ctx.get()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "call_id": getattr(record, "call_id", None),
            "turn_id": getattr(record, "turn_id", None),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
