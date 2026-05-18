#!/usr/bin/env python3
"""PTY wrapper around `claude` exposing a Unix-socket control channel.

Lets Claude (running inside the child) call back into the wrapper via a
JSON-line protocol to issue slash commands (`/compact`, `/clear`, `/exit`)
or send arbitrary keystrokes. The wrapper is otherwise a transparent
passthrough between the user's terminal and the `claude` TUI.
"""
from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import select
import shutil
import signal
import socket
import struct
import sys
import termios
import tty
from pathlib import Path
from types import FrameType
from typing import Any

from ptyprocess import PtyProcess

log = logging.getLogger("autonomous_claude")

# --- constants ---------------------------------------------------------------

SOCK_PATH_TEMPLATE = "/tmp/autonomous-claude-{pid}.sock"
DISCOVERY_PATH = Path.home() / ".claude" / "state" / "autonomous-claude.sock.path"

ALLOWED_CMDS = frozenset({"compact", "clear", "exit", "send_keys"})

# Bytes accepted in `send_keys.data` after UTF-8 encoding:
#   - printable ASCII 0x20-0x7e
#   - carriage return 0x0d
#   - escape         0x1b   (covers CSI sequences \x1b[A etc.)
SEND_KEYS_ALLOWED: frozenset[int] = frozenset(
    {0x0D, 0x1B} | set(range(0x20, 0x7F))
)

MAX_REQUEST_BYTES = 64 * 1024

# After /compact, Claude Code returns to an idle prompt and waits for input —
# it does not auto-resume the prior task. We queue a one-line nudge that
# lands in the input buffer; the TUI processes it as the next user turn once
# compaction finishes. Plain text (no leading slash) so no autocomplete fires.
POST_COMPACT_NUDGE = b"Context was just compacted. Resume your previous task.\r"


# --- low-level write helper -------------------------------------------------


def write_all(fd: int, data: bytes) -> int:
    """`os.write` can short-write. Loop until everything's out.

    Returns total bytes written. Raises on hard failure. EAGAIN is treated as
    "buffer full, give up after writing what we already wrote" — the caller
    decides whether to retry; for our use (control-command keystrokes and
    bridged stdin) we just drop the unwritten tail rather than block the
    select loop.
    """
    view = memoryview(data)
    total = 0
    while view:
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            break
        if n <= 0:
            break
        view = view[n:]
        total += n
    return total


# --- pure helpers (unit-testable) -------------------------------------------


def validate_send_keys(data: str) -> bool:
    """Return True iff every byte of `data` is in the allowlist."""
    try:
        encoded = data.encode("utf-8")
    except (UnicodeEncodeError, AttributeError):
        return False
    return all(b in SEND_KEYS_ALLOWED for b in encoded)


def translate_command(payload: dict[str, Any]) -> bytes:
    """Translate a validated control message into bytes for the PTY master.

    Raises ValueError on invalid payloads. Callers should catch and report.
    """
    cmd = payload.get("cmd")
    if cmd not in ALLOWED_CMDS:
        raise ValueError(f"unknown cmd: {cmd!r}")

    # NOTE: do NOT prefix slash commands with ESC. In Claude Code's TUI, ESC
    # is "interrupt current generation" — it would abort the very turn that
    # fired this socket call. Keystrokes written while Claude is mid-turn
    # queue cleanly as the next user message, which is what we want.
    #
    # After /compact runs, the TUI returns to an idle input prompt — Claude
    # does NOT auto-resume. So every compact translation appends a follow-up
    # nudge that gets typed into the input buffer; once the TUI returns from
    # compaction it processes the queued bytes as the next user turn.
    if cmd == "compact":
        instructions = payload.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str):
                raise ValueError("instructions must be a string")
            if not validate_send_keys(instructions):
                raise ValueError("instructions contains disallowed bytes")
        if instructions is None:
            compact_seq = b"/compact\r"
        else:
            # Wrap /compact + args in bracketed paste (ESC[200~ … ESC[201~).
            # Without it, typing `/compact ` character-by-character lets the
            # TUI's slash-command autocomplete eat the space as "confirm &
            # submit," firing bare /compact and dumping the instructions into
            # the next input as plain text (where \r becomes a newline, not a
            # submit). Bracketed paste bypasses the autocomplete state machine
            # — the TUI treats the whole thing as one pasted line.
            body = b"/compact " + instructions.encode("utf-8")
            compact_seq = b"\x1b[200~" + body + b"\x1b[201~\r"
        return compact_seq + POST_COMPACT_NUDGE

    if cmd == "clear":
        return b"/clear\r"

    if cmd == "exit":
        return b"/exit\r"

    # send_keys
    data = payload.get("data")
    if not isinstance(data, str):
        raise ValueError("data must be a string")
    if not validate_send_keys(data):
        raise ValueError("data contains disallowed bytes")
    return data.encode("utf-8")


# --- socket lifecycle helpers -----------------------------------------------


def atomic_write_discovery(sock_path: str, discovery: Path = DISCOVERY_PATH) -> None:
    """Write the socket path to the discovery file atomically."""
    discovery.parent.mkdir(parents=True, exist_ok=True)
    tmp = discovery.with_suffix(discovery.suffix + ".tmp")
    tmp.write_text(sock_path)
    os.rename(tmp, discovery)


def remove_discovery_if_ours(sock_path: str, discovery: Path = DISCOVERY_PATH) -> None:
    """Remove the discovery file only if it still points at `sock_path`."""
    try:
        if discovery.read_text().strip() == sock_path:
            discovery.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("failed to clean discovery file: %s", exc)


def create_control_socket(sock_path: str) -> socket.socket:
    """Bind a 0600 AF_UNIX SOCK_STREAM listener at `sock_path`."""
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o077)
    try:
        srv.bind(sock_path)
    finally:
        os.umask(old_umask)
    os.chmod(sock_path, 0o600)
    srv.listen(8)
    srv.setblocking(False)
    return srv


# --- wrapper -----------------------------------------------------------------


class Wrapper:
    def __init__(self, claude_argv: list[str]) -> None:
        self.claude_argv = claude_argv
        self.sock_path = SOCK_PATH_TEMPLATE.format(pid=os.getpid())
        self.child: PtyProcess | None = None
        self.server: socket.socket | None = None
        self.clients: dict[int, socket.socket] = {}
        self.client_buffers: dict[int, bytes] = {}
        self._stdin_termios: list[Any] | None = None
        self._winch_pending = False

    # -- setup / teardown --

    def _save_termios(self) -> None:
        if sys.stdin.isatty():
            self._stdin_termios = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())

    def _restore_termios(self) -> None:
        if self._stdin_termios is not None:
            try:
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSADRAIN, self._stdin_termios
                )
            except (OSError, termios.error):
                pass
            self._stdin_termios = None

    def _initial_winsize(self) -> tuple[int, int]:
        try:
            sz = os.get_terminal_size()
            return sz.lines, sz.columns
        except OSError:
            return 24, 80

    def _push_winsize(self) -> None:
        if self.child is None:
            return
        try:
            packed = fcntl.ioctl(
                sys.stdin.fileno(), termios.TIOCGWINSZ, b"\x00" * 8
            )
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            fcntl.ioctl(self.child.fd, termios.TIOCSWINSZ, packed)
            self.child.setwinsize(rows, cols)
        except OSError as exc:
            log.debug("winsize push failed: %s", exc)

    def _install_signals(self) -> None:
        def on_winch(_signum: int, _frame: FrameType | None) -> None:
            self._winch_pending = True

        def on_term(_signum: int, _frame: FrameType | None) -> None:
            if self.child is not None and self.child.isalive():
                try:
                    self.child.terminate(force=False)
                except Exception:
                    pass

        signal.signal(signal.SIGWINCH, on_winch)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)

    def _spawn_child(self) -> None:
        binary = self.claude_argv[0]
        if shutil.which(binary) is None and not os.path.isabs(binary):
            raise FileNotFoundError(f"{binary!r} not found on PATH")
        rows, cols = self._initial_winsize()
        env = os.environ.copy()
        env["AUTONOMOUS_CLAUDE_SOCK"] = self.sock_path
        env["AUTONOMOUS_CLAUDE_PID"] = str(os.getpid())
        self.child = PtyProcess.spawn(
            self.claude_argv,
            env=env,
            dimensions=(rows, cols),
        )

    # -- control channel --

    def _accept_client(self) -> None:
        assert self.server is not None
        try:
            conn, _ = self.server.accept()
        except BlockingIOError:
            return
        conn.setblocking(False)
        self.clients[conn.fileno()] = conn
        self.client_buffers[conn.fileno()] = b""

    def _drop_client(self, fd: int) -> None:
        conn = self.clients.pop(fd, None)
        self.client_buffers.pop(fd, None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _reply(self, conn: socket.socket, payload: dict[str, Any]) -> None:
        try:
            conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError as exc:
            log.debug("reply failed: %s", exc)

    def _handle_client_data(self, fd: int) -> None:
        conn = self.clients[fd]
        try:
            chunk = conn.recv(4096)
        except BlockingIOError:
            return
        except OSError:
            self._drop_client(fd)
            return
        buf = self.client_buffers[fd] + chunk
        if len(buf) > MAX_REQUEST_BYTES:
            self._reply(conn, {"ok": False, "error": "request too large"})
            self._drop_client(fd)
            return
        # On EOF, dispatch whatever we have if non-empty — clients that send a
        # single JSON object and close (e.g. `printf %s '{...}' | socat -`)
        # must still be served.
        if not chunk:
            if buf:
                self._dispatch(conn, buf)
            self._drop_client(fd)
            return
        if b"\n" not in buf:
            self.client_buffers[fd] = buf
            return
        line, _, _ = buf.partition(b"\n")
        self._dispatch(conn, line)
        self._drop_client(fd)

    def _dispatch(self, conn: socket.socket, line: bytes) -> None:
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._reply(conn, {"ok": False, "error": f"bad json: {exc}"})
            return
        if not isinstance(payload, dict):
            self._reply(conn, {"ok": False, "error": "payload must be an object"})
            return
        try:
            keys = translate_command(payload)
        except ValueError as exc:
            self._reply(conn, {"ok": False, "error": str(exc)})
            return
        if self.child is None or not self.child.isalive():
            self._reply(conn, {"ok": False, "error": "child not running"})
            return
        try:
            written = write_all(self.child.fd, keys)
        except OSError as exc:
            self._reply(conn, {"ok": False, "error": f"write failed: {exc}"})
            return
        if written != len(keys):
            self._reply(
                conn,
                {"ok": False, "error": f"short write: {written}/{len(keys)} bytes"},
            )
            return
        self._reply(conn, {"ok": True})

    # -- main loop --

    def run(self) -> int:
        self._install_signals()
        self._save_termios()
        try:
            self._spawn_child()
            assert self.child is not None
            self.server = create_control_socket(self.sock_path)
            atomic_write_discovery(self.sock_path)
            self._push_winsize()
            return self._loop()
        finally:
            self._restore_termios()
            self._cleanup_socket()

    def _cleanup_socket(self) -> None:
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        for fd in list(self.clients):
            self._drop_client(fd)
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("failed to unlink socket: %s", exc)
        remove_discovery_if_ours(self.sock_path)

    def _loop(self) -> int:
        assert self.child is not None
        assert self.server is not None
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
        master_fd = self.child.fd
        server_fd = self.server.fileno()
        stdin_open = True  # cleared on EOF so select doesn't spin forever

        while True:
            if self._winch_pending:
                self._winch_pending = False
                self._push_winsize()

            if not self.child.isalive():
                # Drain any remaining output before exit.
                try:
                    while True:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        write_all(stdout_fd, data)
                except OSError:
                    pass
                self.child.wait()
                # Propagate signal deaths as 128+signal (shell convention) so
                # CI / supervisors see the real failure instead of a silent 0.
                if self.child.exitstatus is not None:
                    return int(self.child.exitstatus)
                if self.child.signalstatus is not None:
                    return 128 + int(self.child.signalstatus)
                return 0

            rlist = [master_fd, server_fd, *self.clients.keys()]
            if stdin_open:
                rlist.append(stdin_fd)
            try:
                ready, _, _ = select.select(rlist, [], [], 0.5)
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise

            for fd in ready:
                if fd == stdin_fd:
                    try:
                        data = os.read(stdin_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        # EOF on stdin — don't keep selecting on it (would
                        # spin the loop at 100% CPU).
                        stdin_open = False
                        continue
                    try:
                        write_all(master_fd, data)
                    except OSError:
                        pass
                elif fd == master_fd:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        # Child closed master; loop will catch !isalive next tick.
                        continue
                    write_all(stdout_fd, data)
                elif fd == server_fd:
                    self._accept_client()
                elif fd in self.clients:
                    self._handle_client_data(fd)


# --- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # AUTONOMOUS_CLAUDE_BINARY is an undocumented test hook; production callers
    # should rely on the default. Note the var name avoids the credential-scrub
    # patterns (_KEY/_TOKEN/_SECRET) per claude-code #32512 advice.
    binary = os.environ.get("AUTONOMOUS_CLAUDE_BINARY", "claude")
    claude_argv = [binary, *args]
    wrapper = Wrapper(claude_argv)
    return wrapper.run()


if __name__ == "__main__":
    sys.exit(main())
