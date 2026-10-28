"""Structured JSON logging with secret redaction (F13, F17)."""

import json
import logging
import logging.handlers
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MASK = "[REDACTED]"

# Patterns with two groups keep the key name and separator and mask only the value.
_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]+"),
    re.compile(r"\bxapp-[A-Za-z0-9-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|x-api-key|client[_-]?secret|password)(\"?\s*[=:]\s*\"?)[^\s\",]+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)

_runtime_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Mask this exact value in every later log line."""
    if len(value) >= 4:
        with _lock:
            _runtime_secrets.add(value)


def redact(text: str) -> str:
    with _lock:
        secrets = sorted(_runtime_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, MASK)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
        else:
            text = pattern.sub(MASK, text)
    return text


class RedactionFilter(logging.Filter):
    """Rewrites the record message so no handler sees a secret."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(entry, default=str, ensure_ascii=False))


def setup_logging(log_dir: Path | None = Path("logs"), level: int = logging.INFO) -> None:
    """JSON to stderr and, if log_dir is set, to a daily rotating file kept for 14 days."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.TimedRotatingFileHandler(
                log_dir / "agent.log", when="midnight", backupCount=14, encoding="utf-8", utc=True
            )
        )
    for handler in handlers:
        handler.addFilter(RedactionFilter())
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
