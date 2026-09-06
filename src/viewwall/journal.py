"""Structured logging to the systemd journal, without a library.

Metrics are numbers, and a log line is prose. Formatting a float into a
sentence and recovering it later with a regex means the parser breaks whenever
someone rewords the message. The journal accepts arbitrary fields alongside the
message, so the numbers can stay numbers:

    journalctl -u viewwall -o json --output-fields=VW_FEED,VW_QUEUED_FPS,VW_PRESENTED_FPS

python3-systemd would provide this, but it would also be viewwall's first
runtime Python dependency, and the native protocol is a datagram on a well
known socket. So this speaks it directly and falls back to ordinary stderr
logging when the socket is absent, which is the case under Docker and when
running the wall by hand.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any


_SOCKET_PATH = "/run/systemd/journal/socket"

# syslog priorities, which is what the journal expects in PRIORITY.
_PRIORITIES = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}

# Everything LogRecord carries that is not ours to forward.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


def _encode(name: str, value: object) -> bytes:
    """Encode one field in the journal's native wire format.

    A value with no newline is sent as "NAME=value\\n". Anything else needs the
    binary form: the name, a newline, a little-endian 64-bit length, the raw
    value, and a trailing newline.
    """
    key = name.upper().encode("ascii", "replace")
    data = str(value).encode("utf-8")
    if b"\n" in data:
        return key + b"\n" + len(data).to_bytes(8, "little") + data + b"\n"
    return key + b"=" + data + b"\n"


class JournalHandler(logging.Handler):
    """Send records to the journal, with `extra=` keys as queryable fields."""

    def __init__(self, socket_path: str | None = None) -> None:
        super().__init__()
        # Resolved at call time rather than bound as a default, so the path
        # stays a single source of truth.
        socket_path = socket_path or _SOCKET_PATH
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.connect(socket_path)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = [
                _encode("MESSAGE", self.format(record)),
                _encode("PRIORITY", _PRIORITIES.get(record.levelno, 6)),
                _encode("LOGGER", record.name),
                _encode("CODE_FILE", record.pathname),
                _encode("CODE_LINE", record.lineno),
                _encode("CODE_FUNC", record.funcName),
            ]
            for key, value in vars(record).items():
                # Anything passed as extra={...}, which is what carries the
                # metrics. Underscore-prefixed names are reserved for fields
                # the journal itself trusts, so they are not ours to set.
                if key not in _RESERVED and not key.startswith("_"):
                    fields.append(_encode(key, value))
            self._socket.send(b"".join(fields))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def close(self) -> None:
        try:
            self._socket.close()
        finally:
            super().close()


def install(level: int, fmt: str) -> None:
    """Log to the journal when it is there, and to stderr when it is not.

    Under systemd the handler adds structured fields; everywhere else this is
    an ordinary stderr setup, so nothing depends on the journal being present.
    """
    handler: logging.Handler
    try:
        if not os.environ.get("JOURNAL_STREAM"):
            # Not started by systemd. Its stderr goes somewhere a person is
            # watching, so plain text is the more useful output.
            raise OSError("not running under systemd")
        handler = JournalHandler()
        # The journal records its own timestamp and level.
        handler.setFormatter(logging.Formatter("%(message)s"))
    except OSError:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def format_fields(fields: dict[str, Any]) -> str:
    """Render metric fields for a human, for the stderr fallback."""
    return " ".join(f"{key.removeprefix('VW_').lower()}={value}" for key, value in fields.items())
