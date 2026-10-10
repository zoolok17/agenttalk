"""Somewhere for a windowless ``gateway run`` to write.

The gateway's scheduled task starts Python with pythonw.exe, which has no stdout and no
stderr, so a print or an error message would raise and the message would be lost. This
module imports only the standard library and agenttalk's redaction, so ``__main__`` can set
it up before the CLI and the gateway code load: a failure while they load is logged too.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .redaction import redact_diagnostic_text

# The same bounds as the LiteLLM log beside it.
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 2
RECORD_MAX_BYTES = 64 * 1024


def default_gateway_log_path() -> Path:
    # ovh_gateway.default_secret_dir() / "gateway", which this module must not import.
    appdata = os.environ.get("LOCALAPPDATA")
    base = Path(appdata) if appdata else Path.home() / ".local" / "share"
    return base / "agenttalk-ovh" / "gateway" / "gateway.log"


def _bounded(line: str) -> str:
    clean = redact_diagnostic_text(line.rstrip("\r"))
    encoded = clean.encode("utf-8", "replace")
    if len(encoded) <= RECORD_MAX_BYTES:
        return clean
    marker = " [truncated]"
    return encoded[: RECORD_MAX_BYTES - len(marker)].decode("utf-8", "ignore") + marker


class _LineLog(io.TextIOBase):
    """A text stream that writes each complete line to a bounded log file."""

    def __init__(self, handler: RotatingFileHandler) -> None:
        super().__init__()
        self._handler = handler
        self._pending = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            *lines, self._pending = (self._pending + str(text)).split("\n")
        for line in lines:
            self._log(line)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            line, self._pending = self._pending, ""
        if line:
            self._log(line)

    def close(self) -> None:
        if not self.closed:
            self.flush()
            self._handler.close()
        super().close()

    def _log(self, line: str) -> None:
        clean = _bounded(line)
        if clean:
            self._handler.handle(
                logging.LogRecord(
                    "agenttalk.ovh_gateway.run", logging.INFO, __file__, 0, clean, (), None
                )
            )


def route_missing_output_to_log(path: Path | None = None) -> None:
    """Point a missing stdout or stderr at the gateway log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        path = Path(path or default_gateway_log_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
        os.chmod(path, 0o600)
    except OSError:
        # With no log to write, losing the output is better than losing the gateway.
        stream = open(os.devnull, "w", encoding="utf-8")
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        # A failed write has nowhere left to go: logging would report it on
        # stderr, which is this same log.
        handler.handleError = lambda _record: None
        stream = _LineLog(handler)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
