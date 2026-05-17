"""Tests for autonomous-claude-ctl, the in-session controller."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ptyprocess import PtyProcess

import autonomous_claude_ctl as ctl

ROOT = Path(__file__).resolve().parent.parent
CTL_PATH = ROOT / "autonomous_claude_ctl.py"


# --- pure: argv parsing -------------------------------------------------------


def test_parse_compact_no_args() -> None:
    assert ctl._parse_argv(["compact"]) == {"cmd": "compact"}


def test_parse_compact_with_instructions() -> None:
    assert ctl._parse_argv(["compact", "keep X"]) == {
        "cmd": "compact",
        "instructions": "keep X",
    }


def test_parse_clear() -> None:
    assert ctl._parse_argv(["clear"]) == {"cmd": "clear"}


def test_parse_exit() -> None:
    assert ctl._parse_argv(["exit"]) == {"cmd": "exit"}


def test_parse_send_keys() -> None:
    assert ctl._parse_argv(["send-keys", "\x1b[A"]) == {
        "cmd": "send_keys",
        "data": "\x1b[A",
    }


def test_parse_unknown_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        ctl._parse_argv(["nuke"])
    assert exc.value.code == 2


def test_parse_empty_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        ctl._parse_argv([])
    assert exc.value.code == 2


def test_parse_clear_with_extra_args_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        ctl._parse_argv(["clear", "oops"])
    assert exc.value.code == 2


def test_parse_compact_empty_string_means_no_instructions() -> None:
    # Regression: don't send `/compact \r` (trailing space) when caller
    # passes "" — treat as the no-arg form.
    assert ctl._parse_argv(["compact", ""]) == {"cmd": "compact"}


def test_parse_send_keys_empty_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        ctl._parse_argv(["send-keys", ""])
    assert exc.value.code == 2


# --- env-var guard ------------------------------------------------------------


def test_resolve_socket_refuses_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AUTONOMOUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("AUTONOMOUS_CLAUDE_SOCK", raising=False)
    with pytest.raises(SystemExit) as exc:
        ctl._resolve_socket()
    assert exc.value.code == 1
    assert "not inside an autonomous-claude session" in capsys.readouterr().err


def test_resolve_socket_refuses_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTONOMOUS_CLAUDE_PID", "999")
    monkeypatch.setenv("AUTONOMOUS_CLAUDE_SOCK", "/tmp/wrong.sock")
    with pytest.raises(SystemExit) as exc:
        ctl._resolve_socket()
    assert exc.value.code == 1
    assert "PID/SOCK mismatch" in capsys.readouterr().err


def test_resolve_socket_refuses_missing_socket_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # PID and SOCK agree, but no file at that path.
    pid = "987654"
    sock = f"/tmp/autonomous-claude-{pid}.sock"
    monkeypatch.setenv("AUTONOMOUS_CLAUDE_PID", pid)
    monkeypatch.setenv("AUTONOMOUS_CLAUDE_SOCK", sock)
    # Make sure the file really doesn't exist.
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass
    with pytest.raises(SystemExit) as exc:
        ctl._resolve_socket()
    assert exc.value.code == 1
    assert "wrapper socket missing" in capsys.readouterr().err


def test_resolve_socket_accepts_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Use a PID-shaped suffix and create a real socket file.
    pid = "424242"
    sock = f"/tmp/autonomous-claude-{pid}.sock"
    # Create a real listening socket so the file exists.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        s.bind(sock)
        s.listen(1)
        monkeypatch.setenv("AUTONOMOUS_CLAUDE_PID", pid)
        monkeypatch.setenv("AUTONOMOUS_CLAUDE_SOCK", sock)
        assert ctl._resolve_socket() == sock
    finally:
        s.close()
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass


# --- end-to-end against a live wrapper ----------------------------------------


def _wait_for_sock(path: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).is_socket():
            return
        time.sleep(0.05)
    raise TimeoutError(f"socket never appeared: {path}")


def _shutdown(wrapper: PtyProcess, timeout: float = 5.0) -> None:
    try:
        os.kill(wrapper.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and wrapper.isalive():
        time.sleep(0.05)
    if wrapper.isalive():
        try:
            wrapper.terminate(force=True)
        except Exception:
            pass
    wrapper.wait()


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not on PATH")
def test_ctl_send_keys_against_real_wrapper(tmp_path: Path) -> None:
    """Spawn the wrapper with `cat` as child, then run ctl as a subprocess."""
    wrapper_env = os.environ.copy()
    wrapper_env["AUTONOMOUS_CLAUDE_BINARY"] = "cat"
    wrapper_env["HOME"] = str(tmp_path)
    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py")],
        env=wrapper_env,
        dimensions=(24, 80),
    )
    try:
        sock_path = f"/tmp/autonomous-claude-{wrapper.pid}.sock"
        _wait_for_sock(sock_path)

        # Run the controller as a separate process, with the env vars a child
        # of the wrapper would have inherited.
        ctl_env = os.environ.copy()
        ctl_env["AUTONOMOUS_CLAUDE_PID"] = str(wrapper.pid)
        ctl_env["AUTONOMOUS_CLAUDE_SOCK"] = sock_path
        # send-keys "hello\r" — the only command shape we can verify via cat
        result = subprocess.run(
            [sys.executable, str(CTL_PATH), "send-keys", "hello\r"],
            env=ctl_env,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode()
        assert result.stdout == b""  # quiet on success

        # Verify the bytes actually came back out of cat via the wrapper.
        deadline = time.monotonic() + 5.0
        buf = b""
        while time.monotonic() < deadline:
            try:
                chunk = wrapper.read(4096)
            except EOFError:
                break
            if chunk:
                buf += chunk
                if b"hello" in buf:
                    break
            else:
                time.sleep(0.05)
        assert b"hello" in buf
    finally:
        _shutdown(wrapper)


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not on PATH")
def test_ctl_does_not_stall_with_no_newline(tmp_path: Path) -> None:
    """Regression: the user reported `bash command stalls`.

    Confirm the controller (which uses SHUT_WR) returns promptly and
    doesn't deadlock waiting for either side to close.
    """
    wrapper_env = os.environ.copy()
    wrapper_env["AUTONOMOUS_CLAUDE_BINARY"] = "cat"
    wrapper_env["HOME"] = str(tmp_path)
    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py")],
        env=wrapper_env,
        dimensions=(24, 80),
    )
    try:
        sock_path = f"/tmp/autonomous-claude-{wrapper.pid}.sock"
        _wait_for_sock(sock_path)
        ctl_env = os.environ.copy()
        ctl_env["AUTONOMOUS_CLAUDE_PID"] = str(wrapper.pid)
        ctl_env["AUTONOMOUS_CLAUDE_SOCK"] = sock_path
        # 3-second hard timeout — if anything stalls, this fails loudly.
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(CTL_PATH), "send-keys", "x"],
            env=ctl_env,
            capture_output=True,
            timeout=3,
        )
        elapsed = time.monotonic() - start
        assert result.returncode == 0, result.stderr.decode()
        assert elapsed < 2.0, f"controller took {elapsed:.2f}s — too slow"
    finally:
        _shutdown(wrapper)


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not on PATH")
def test_ctl_reports_wrapper_refusal(tmp_path: Path) -> None:
    """Bad arg → wrapper replies {ok:false,...}; ctl exits non-zero with msg."""
    wrapper_env = os.environ.copy()
    wrapper_env["AUTONOMOUS_CLAUDE_BINARY"] = "cat"
    wrapper_env["HOME"] = str(tmp_path)
    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py")],
        env=wrapper_env,
        dimensions=(24, 80),
    )
    try:
        sock_path = f"/tmp/autonomous-claude-{wrapper.pid}.sock"
        _wait_for_sock(sock_path)
        ctl_env = os.environ.copy()
        ctl_env["AUTONOMOUS_CLAUDE_PID"] = str(wrapper.pid)
        ctl_env["AUTONOMOUS_CLAUDE_SOCK"] = sock_path
        # Send-keys with a disallowed byte (Ctrl-C) — wrapper validates & refuses.
        result = subprocess.run(
            [sys.executable, str(CTL_PATH), "send-keys", "\x03"],
            env=ctl_env,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert b"wrapper refused command" in result.stderr
        assert b"disallowed bytes" in result.stderr
    finally:
        _shutdown(wrapper)
