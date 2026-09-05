import logging
import socket
from pathlib import Path

import pytest

from viewwall.journal import JournalHandler, format_fields, install


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "viewwall.test", logging.INFO, __file__, 10, "metrics %s", ("x",), None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _parse(datagram: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in datagram.split(b"\n"):
        if b"=" in line:
            key, _, value = line.partition(b"=")
            fields[key.decode()] = value.decode()
    return fields


def _handler_and_socket(tmp_path: Path) -> tuple[JournalHandler, socket.socket]:
    path = str(tmp_path / "journal.sock")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(path)
    receiver.settimeout(1)
    handler = JournalHandler(path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler, receiver


def test_extra_fields_are_sent_as_journal_fields(tmp_path: Path) -> None:
    # The point of the handler: a number stays a number and needs no parser.
    handler, receiver = _handler_and_socket(tmp_path)
    try:
        handler.emit(_record(VW_FPS="29.6", VW_VIEWPORT="upper_left"))
        fields = _parse(receiver.recv(4096))
    finally:
        handler.close()
        receiver.close()
    assert fields["VW_FPS"] == "29.6"
    assert fields["VW_VIEWPORT"] == "upper_left"
    assert fields["PRIORITY"] == "6"
    assert fields["MESSAGE"] == "metrics x"


def test_record_internals_are_not_forwarded(tmp_path: Path) -> None:
    handler, receiver = _handler_and_socket(tmp_path)
    try:
        handler.emit(_record(VW_FPS="1.0"))
        fields = _parse(receiver.recv(4096))
    finally:
        handler.close()
        receiver.close()
    for reserved in ("ARGS", "MSG", "LEVELNO", "CREATED"):
        assert reserved not in fields


def test_a_multiline_value_uses_the_binary_encoding(tmp_path: Path) -> None:
    # A newline in a value would otherwise be read as the end of the field.
    handler, receiver = _handler_and_socket(tmp_path)
    try:
        handler.emit(_record(VW_NOTE="first\nsecond"))
        datagram = receiver.recv(4096)
    finally:
        handler.close()
        receiver.close()
    assert b"VW_NOTE\n" in datagram
    assert b"first\nsecond" in datagram
    # Length prefix, little-endian 64-bit.
    assert (12).to_bytes(8, "little") in datagram


def test_logging_falls_back_to_stderr_without_systemd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Docker and hand-run sessions have no journal socket; logging must work.
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    install(logging.INFO, "%(message)s")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert not isinstance(handlers[0], JournalHandler)


def test_logging_falls_back_when_the_socket_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    monkeypatch.setattr("viewwall.journal._SOCKET_PATH", "/nonexistent/journal.sock")
    install(logging.INFO, "%(message)s")
    assert not isinstance(logging.getLogger().handlers[0], JournalHandler)


def test_fields_render_readably_for_the_stderr_fallback() -> None:
    assert format_fields({"VW_VIEWPORT": "upper_left", "VW_FPS": "29.6"}) == (
        "viewport=upper_left fps=29.6"
    )
