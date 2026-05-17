from __future__ import annotations

import pytest

from autonomous_claude import translate_command


def test_compact_no_instructions() -> None:
    assert translate_command({"cmd": "compact"}) == b"/compact\r"


def test_compact_with_instructions() -> None:
    out = translate_command({"cmd": "compact", "instructions": "keep API contract"})
    assert out == b"/compact keep API contract\r"


def test_clear() -> None:
    assert translate_command({"cmd": "clear"}) == b"/clear\r"


def test_exit() -> None:
    assert translate_command({"cmd": "exit"}) == b"/exit\r"


def test_write_all_loops_on_short_writes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression: bug #3 — os.write can short-write; write_all must loop."""
    from autonomous_claude import write_all

    calls: list[int] = []

    def fake_write(fd: int, data: bytes | memoryview) -> int:
        # Always write at most 3 bytes per call.
        n = min(3, len(data))
        calls.append(n)
        return n

    monkeypatch.setattr("autonomous_claude.os.write", fake_write)
    payload = b"hello world!"
    assert write_all(99, payload) == len(payload)
    assert sum(calls) == len(payload)
    assert max(calls) <= 3


def test_write_all_stops_on_eagain(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from autonomous_claude import write_all

    def fake_write(fd: int, data: bytes | memoryview) -> int:
        raise BlockingIOError()

    monkeypatch.setattr("autonomous_claude.os.write", fake_write)
    # Should return 0 (nothing written), not raise.
    assert write_all(99, b"abc") == 0


def test_no_esc_prefix_on_slash_commands() -> None:
    # Regression: ESC = "interrupt" in Claude Code TUI, must not prefix.
    for cmd in ("compact", "clear", "exit"):
        assert not translate_command({"cmd": cmd}).startswith(b"\x1b")


def test_send_keys_plain() -> None:
    assert translate_command({"cmd": "send_keys", "data": "hello\r"}) == b"hello\r"


def test_send_keys_csi_arrow() -> None:
    # Up arrow: ESC [ A
    assert translate_command({"cmd": "send_keys", "data": "\x1b[A"}) == b"\x1b[A"


def test_unknown_cmd_rejected() -> None:
    with pytest.raises(ValueError, match="unknown cmd"):
        translate_command({"cmd": "nuke"})


def test_missing_cmd_rejected() -> None:
    with pytest.raises(ValueError):
        translate_command({})


def test_compact_rejects_non_string_instructions() -> None:
    with pytest.raises(ValueError, match="instructions must be a string"):
        translate_command({"cmd": "compact", "instructions": 42})


def test_compact_rejects_control_bytes_in_instructions() -> None:
    with pytest.raises(ValueError, match="disallowed bytes"):
        translate_command({"cmd": "compact", "instructions": "bad\x03char"})


def test_send_keys_rejects_non_string_data() -> None:
    with pytest.raises(ValueError, match="data must be a string"):
        translate_command({"cmd": "send_keys", "data": 7})


def test_send_keys_rejects_ctrl_c() -> None:
    with pytest.raises(ValueError, match="disallowed bytes"):
        translate_command({"cmd": "send_keys", "data": "\x03"})


def test_send_keys_rejects_tab() -> None:
    # Tab is not in the allowlist (only 0x20-0x7e + \r + \x1b).
    with pytest.raises(ValueError, match="disallowed bytes"):
        translate_command({"cmd": "send_keys", "data": "\t"})


def test_send_keys_rejects_newline() -> None:
    with pytest.raises(ValueError, match="disallowed bytes"):
        translate_command({"cmd": "send_keys", "data": "x\n"})
