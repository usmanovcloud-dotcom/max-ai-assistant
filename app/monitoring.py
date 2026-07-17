from __future__ import annotations

import logging
import re
import threading
from collections import deque
from dataclasses import asdict, dataclass


SECRET_PATTERNS = (
    re.compile(r"sk-(?:or-v1-)?[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,]+"),
    re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,]+"),
)


def redact(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(r"\1[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


@dataclass(frozen=True, slots=True)
class LogEntry:
    created_at: int
    level: str
    logger: str
    message: str


class RingLogHandler(logging.Handler):
    def __init__(self, capacity: int = 300) -> None:
        super().__init__(logging.INFO)
        self.entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(record.getMessage())
            entry = LogEntry(
                created_at=int(record.created),
                level=record.levelname,
                logger=record.name,
                message=message[:1000],
            )
            with self._lock:
                self.entries.append(entry)
        except Exception:
            self.handleError(record)

    def snapshot(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock:
            selected = list(self.entries)[-max(1, min(limit, 300)) :]
        return [asdict(entry) for entry in reversed(selected)]
