"""Bounded, session-only diagnostics with no free-form or document text."""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from enum import Enum


class Event(Enum):
    SESSION_STARTED = "session_started"
    SESSION_STOPPED = "session_stopped"
    STARTUP_FAILED = "startup_failed"
    ARTICLE_READ_FAILED = "article_read_failed"
    THEME_INVALID = "theme_invalid"
    SEARCH_START_FAILED = "search_start_failed"
    SEARCH_PIPE_FAILED = "search_pipe_failed"
    SEARCH_QUERY_FAILED = "search_query_failed"
    INTERNAL_ERROR = "internal_error"


# Fixed labels, never a traceback filename, source line, local, or error message.
_MODULES = {f"zimtty.{name}": name for name in
            ("app", "zimdoc", "paginate", "theme", "suggestd")}


class DiagnosticTrace:
    """Keep only the last 32 events; exceptions and frames are never retained."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._events: deque[str] = deque(maxlen=32)
        self._lock = threading.Lock()

    def record(self, event: Event, error: BaseException | None = None) -> None:
        if not isinstance(event, Event):
            raise TypeError("diagnostic events must be fixed event codes")
        locations: deque[str] = deque(maxlen=3)
        tb = error.__traceback__ if error is not None else None
        while tb is not None:
            module = tb.tb_frame.f_globals.get("__name__")
            if module in _MODULES:
                locations.append(f"{_MODULES[module]}:{tb.tb_lineno}")
            tb = tb.tb_next
        elapsed = int((time.monotonic() - self._start) * 1000)
        line = f"+{elapsed:06d}ms {event.value}"
        if locations:
            line += " " + " ".join(locations)
        with self._lock:
            self._events.append(line)

    def render(self) -> str:
        with self._lock:
            return "\n".join(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


def disable_framework_logging() -> None:
    """Ignore inherited Textual logging/devtools/automatic screen capture.

    Textual's event logs include keys and Input values. Run before importing it,
    and again before constructing the app for callers that imported it first.
    These changes affect this process only, not the user's shell settings.
    """
    for key in ("TEXTUAL_LOG", "TEXTUAL_DEBUG", "TEXTUAL_SCREENSHOT"):
        os.environ.pop(key, None)
    if "TEXTUAL" in os.environ:
        os.environ["TEXTUAL"] = ",".join(
            feature for feature in os.environ["TEXTUAL"].split(",")
            if feature.strip().lower() not in {"debug", "devtools"}
        )
    constants = sys.modules.get("textual.constants")
    if constants is not None:
        constants.LOG_FILE = None
        constants.DEBUG = False
        constants.SCREENSHOT_DELAY = -1
