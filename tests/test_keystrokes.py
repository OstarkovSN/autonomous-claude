from __future__ import annotations

import pytest

from autonomous_claude import translate_command


def test_compact_no_instructions() -> None:
    # Immediate chunks: just the submit. The nudge is now SCHEDULED, not
    # appended to the keystroke stream, because keystrokes typed during
    # compaction are discarded by Claude Code (unlike during normal
    # generation, where they queue as the next user turn).
    out = translate_command({"cmd": "compact"})
    assert out == [b"/compact\r"]


def test_compact_with_instructions_uses_bracketed_paste() -> None:
    out = translate_command({"cmd": "compact", "instructions": "keep API contract"})
    # Two immediate chunks, written with a delay between:
    #   1. bracketed-paste-wrapped /compact + args (no trailing \r)
    #   2. the submit \r — must be a separate write so Ink processes it
    #      as a distinct input event, not merged into the paste
    assert out == [
        b"\x1b[200~/compact keep API contract\x1b[201~",
        b"\r",
    ]


def test_compact_resume_nudge_is_scheduled_not_streamed() -> None:
    """Regression: typing the resume nudge during compaction loses it.

    The nudge must come from get_delayed_writes, NOT from
    translate_command's immediate chunks.
    """
    from autonomous_claude import (
        COMPACT_RESUME_DELAY_S,
        POST_COMPACT_NUDGE,
        get_delayed_writes,
    )

    # Immediate chunks must not contain the nudge bytes.
    for payload in [{"cmd": "compact"}, {"cmd": "compact", "instructions": "x"}]:
        joined = b"".join(translate_command(payload))
        assert POST_COMPACT_NUDGE not in joined

    # But get_delayed_writes must schedule it.
    for payload in [{"cmd": "compact"}, {"cmd": "compact", "instructions": "x"}]:
        delayed = get_delayed_writes(payload)
        assert delayed == [(COMPACT_RESUME_DELAY_S, POST_COMPACT_NUDGE)]


def test_get_delayed_writes_empty_for_non_compact() -> None:
    from autonomous_claude import get_delayed_writes

    for cmd in (
        {"cmd": "clear"},
        {"cmd": "exit"},
        {"cmd": "send_keys", "data": "x"},
    ):
        assert get_delayed_writes(cmd) == []


def test_compact_with_instructions_submit_is_separate_chunk() -> None:
    """Regression: the user reported `text pasted, newline appended, no submit`
    when the trailing \\r was concatenated to the bracketed paste. The submit
    must be a separate write so Ink's stdin reader sees two input events."""
    out = translate_command({"cmd": "compact", "instructions": "focus"})
    # Find the paste chunk and ensure it does NOT end with \r.
    paste_chunks = [c for c in out if c.startswith(b"\x1b[200~")]
    assert len(paste_chunks) == 1
    assert not paste_chunks[0].endswith(b"\r")
    # The \r must be its own chunk immediately after the paste.
    paste_idx = out.index(paste_chunks[0])
    assert out[paste_idx + 1] == b"\r"


def test_compact_with_instructions_not_typed_charwise() -> None:
    """Regression: typing `/compact <args>` char-by-char makes the TUI's
    autocomplete eat the space as confirm-and-submit, splitting the args
    off into a new input. Bracketed paste must wrap them."""
    out = translate_command({"cmd": "compact", "instructions": "focus"})
    joined = b"".join(out)
    assert b"\x1b[200~" in joined
    assert b"\x1b[201~" in joined
    paste_start = joined.index(b"\x1b[200~") + len(b"\x1b[200~")
    paste_end = joined.index(b"\x1b[201~")
    assert joined[paste_start:paste_end] == b"/compact focus"


def test_clear() -> None:
    assert translate_command({"cmd": "clear"}) == [b"/clear\r"]


def test_exit() -> None:
    assert translate_command({"cmd": "exit"}) == [b"/exit\r"]


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
    # `compact` legitimately uses ESC inside its paste brackets, but the
    # FIRST chunk (which is what the TUI receives first) must not start
    # with a bare ESC for clear/exit; for compact's no-args form the first
    # chunk is /compact\r so it never starts with ESC either.
    for cmd in ("compact", "clear", "exit"):
        first_chunk = translate_command({"cmd": cmd})[0]
        assert not first_chunk.startswith(b"\x1b")


def test_send_keys_plain() -> None:
    assert translate_command({"cmd": "send_keys", "data": "hello\r"}) == [b"hello\r"]


def test_send_keys_csi_arrow() -> None:
    # Up arrow: ESC [ A
    assert translate_command({"cmd": "send_keys", "data": "\x1b[A"}) == [b"\x1b[A"]


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
