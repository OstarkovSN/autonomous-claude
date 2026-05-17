"""Integration test: drive the wrapper end-to-end with `cat` as the child.

We spawn the wrapper itself inside a PTY (so it can put its own stdin in raw
mode), then connect to its control socket and assert that bytes sent via
`send_keys` come back out on the wrapper's stdout — that's the round-trip
through the wrapper's bridging logic and the child PTY's line discipline.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sys
import time
from pathlib import Path

import pytest
from ptyprocess import PtyProcess

ROOT = Path(__file__).resolve().parent.parent


def _shutdown(wrapper: PtyProcess, timeout: float = 5.0) -> None:
    """Send SIGTERM to the wrapper so its signal handler runs cleanup."""
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


def _wait_for_sock(path: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).is_socket():
            return
        time.sleep(0.05)
    raise TimeoutError(f"socket never appeared: {path}")


def _read_until(child: PtyProcess, needle: bytes, timeout: float = 5.0) -> bytes:
    """Read from `child` until `needle` appears in the accumulated buffer."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        try:
            chunk = child.read(4096)
        except EOFError:
            break
        if chunk:
            buf += chunk
            if needle in buf:
                return buf
        else:
            time.sleep(0.05)
    raise AssertionError(f"never saw {needle!r}; got {buf!r}")


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not on PATH")
def test_send_keys_roundtrip_through_cat(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["AUTONOMOUS_CLAUDE_BINARY"] = "cat"
    # Point discovery at a per-test path so we don't stomp on a real session.
    env["HOME"] = str(tmp_path)

    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py")],
        env=env,
        dimensions=(24, 80),
    )
    try:
        sock_path = f"/tmp/autonomous-claude-{wrapper.pid}.sock"
        _wait_for_sock(sock_path)

        # Verify discovery file was written atomically.
        discovery = tmp_path / ".claude" / "state" / "autonomous-claude.sock.path"
        assert discovery.read_text() == sock_path

        # Send a send_keys command through the control socket.
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(sock_path)
        client.sendall(b'{"cmd":"send_keys","data":"hello\\r"}\n')

        reply_buf = b""
        while b"\n" not in reply_buf:
            chunk = client.recv(256)
            if not chunk:
                break
            reply_buf += chunk
        client.close()
        reply = json.loads(reply_buf.decode())
        assert reply == {"ok": True}, reply

        # `cat` echoes input by default (line discipline) AND we wrote a CR.
        # Either the echo or the cat output will contain "hello".
        output = _read_until(wrapper, b"hello")
        assert b"hello" in output

        # Now ask the wrapper to exit cleanly via the control channel.
        # Use a raw escape sequence that cat will pass through; for true
        # shutdown we just terminate the child here.
    finally:
        _shutdown(wrapper)
        # Wrapper should have cleaned up its socket.
        assert not Path(f"/tmp/autonomous-claude-{wrapper.pid}.sock").exists()


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh not on PATH")
def test_signal_exit_code_propagated(tmp_path: Path) -> None:
    """Regression: bug #2 — wrapper used to return 0 for signal-killed child.

    Spawn a child that traps no signals and gets SIGKILL via the control
    socket's `exit` would be cleaner — but exit takes the slash-command path
    which `sh -c 'sleep 30'` won't honor. So kill the child directly and
    verify wrapper exits with 128+SIGKILL=137.
    """
    env = os.environ.copy()
    env["AUTONOMOUS_CLAUDE_BINARY"] = "sh"
    env["HOME"] = str(tmp_path)
    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py"), "-c", "sleep 30"],
        env=env,
        dimensions=(24, 80),
    )
    try:
        sock_path = f"/tmp/autonomous-claude-{wrapper.pid}.sock"
        _wait_for_sock(sock_path)
        # Find the child of the wrapper process and SIGKILL it.
        # On Linux we can read /proc/<pid>/task/<pid>/children.
        children_path = f"/proc/{wrapper.pid}/task/{wrapper.pid}/children"
        deadline = time.monotonic() + 3.0
        child_pid: int | None = None
        while time.monotonic() < deadline:
            try:
                with open(children_path) as fh:
                    raw = fh.read().split()
                if raw:
                    child_pid = int(raw[0])
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        assert child_pid is not None, "could not find child pid"
        os.kill(child_pid, signal.SIGKILL)

        # Wait for wrapper to exit on its own.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and wrapper.isalive():
            time.sleep(0.05)
        assert not wrapper.isalive(), "wrapper did not exit after child killed"
        wrapper.wait()
        # 128 + SIGKILL(9) = 137
        assert wrapper.exitstatus == 137, (
            f"expected exit 137, got exitstatus={wrapper.exitstatus} "
            f"signalstatus={wrapper.signalstatus}"
        )
    finally:
        _shutdown(wrapper)


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not on PATH")
def test_dispatch_on_eof_without_newline(tmp_path: Path) -> None:
    """Regression: `printf %s '{...}' | socat -` sends no newline then EOFs.

    The wrapper must still dispatch the buffered command. Previously it dropped
    the buffer on EOF, which silently broke the documented socat invocation.
    """
    env = os.environ.copy()
    env["AUTONOMOUS_CLAUDE_BINARY"] = "cat"
    env["HOME"] = str(tmp_path)
    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py")],
        env=env,
        dimensions=(24, 80),
    )
    try:
        sock_path = f"/tmp/autonomous-claude-{wrapper.pid}.sock"
        _wait_for_sock(sock_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(sock_path)
        client.sendall(b'{"cmd":"send_keys","data":"hello\\r"}')  # no \n
        client.shutdown(socket.SHUT_WR)  # signal EOF to server
        buf = b""
        while True:
            chunk = client.recv(256)
            if not chunk:
                break
            buf += chunk
        client.close()
        reply = json.loads(buf.decode())
        assert reply == {"ok": True}, reply
        output = _read_until(wrapper, b"hello")
        assert b"hello" in output
    finally:
        _shutdown(wrapper)


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not on PATH")
def test_rejected_command_returns_error(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["AUTONOMOUS_CLAUDE_BINARY"] = "cat"
    env["HOME"] = str(tmp_path)
    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py")],
        env=env,
        dimensions=(24, 80),
    )
    try:
        sock_path = f"/tmp/autonomous-claude-{wrapper.pid}.sock"
        _wait_for_sock(sock_path)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(sock_path)
        client.sendall(b'{"cmd":"nuke"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(256)
            if not chunk:
                break
            buf += chunk
        client.close()
        reply = json.loads(buf.decode())
        assert reply["ok"] is False
        assert "unknown cmd" in reply["error"]
    finally:
        _shutdown(wrapper)


@pytest.mark.skipif(shutil.which("cat") is None, reason="cat not on PATH")
def test_env_vars_injected_into_child(tmp_path: Path) -> None:
    """Spawn `env` as the child and verify AUTONOMOUS_CLAUDE_SOCK is set."""
    if shutil.which("env") is None:
        pytest.skip("env not on PATH")
    env = os.environ.copy()
    env["AUTONOMOUS_CLAUDE_BINARY"] = "env"
    env["HOME"] = str(tmp_path)
    wrapper = PtyProcess.spawn(
        [sys.executable, str(ROOT / "autonomous_claude.py")],
        env=env,
        dimensions=(24, 80),
    )
    try:
        # `env` prints then exits; drain until EOF so we see all vars.
        buf = b""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                chunk = wrapper.read(4096)
            except EOFError:
                break
            if chunk:
                buf += chunk
            else:
                time.sleep(0.05)
        assert b"AUTONOMOUS_CLAUDE_SOCK=/tmp/autonomous-claude-" in buf
        assert b"AUTONOMOUS_CLAUDE_PID=" in buf
    finally:
        _shutdown(wrapper)
