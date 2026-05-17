from __future__ import annotations

from autonomous_claude import (
    ALLOWED_CMDS,
    SEND_KEYS_ALLOWED,
    validate_send_keys,
)


def test_allowed_cmds_exact_set() -> None:
    assert ALLOWED_CMDS == frozenset({"compact", "clear", "exit", "send_keys"})


def test_allowlist_includes_printable_and_cr_and_esc() -> None:
    assert 0x0D in SEND_KEYS_ALLOWED  # \r
    assert 0x1B in SEND_KEYS_ALLOWED  # \x1b
    assert 0x20 in SEND_KEYS_ALLOWED  # space
    assert 0x7E in SEND_KEYS_ALLOWED  # ~
    assert 0x0A not in SEND_KEYS_ALLOWED  # \n is NOT allowed
    assert 0x09 not in SEND_KEYS_ALLOWED  # tab
    assert 0x03 not in SEND_KEYS_ALLOWED  # ctrl-c
    assert 0x00 not in SEND_KEYS_ALLOWED  # NUL


def test_validate_accepts_csi_sequence() -> None:
    assert validate_send_keys("\x1b[A") is True
    assert validate_send_keys("\x1b[1;5C") is True  # ctrl+right
    assert validate_send_keys("\x1b[200~paste\x1b[201~") is True  # bracketed paste


def test_validate_accepts_slash_command_text() -> None:
    assert validate_send_keys("/compact some focus\r") is True


def test_validate_rejects_unicode_non_ascii() -> None:
    assert validate_send_keys("héllo") is False


def test_validate_rejects_empty_ok() -> None:
    # An empty string has no disallowed bytes; allowed.
    assert validate_send_keys("") is True


def test_validate_rejects_non_string() -> None:
    assert validate_send_keys(42) is False  # type: ignore[arg-type]
