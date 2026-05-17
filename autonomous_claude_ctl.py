#!/usr/bin/env python3
"""Controller for the autonomous-claude wrapper.

Invoked from inside a wrapped Claude session:

    autonomous-claude-ctl compact
    autonomous-claude-ctl compact "keep API contract and open bugs"
    autonomous-claude-ctl clear
    autonomous-claude-ctl exit
    autonomous-claude-ctl send-keys $'\\x1b[A'

Hides all the JSON / socket / quoting / env-var guard logic from the caller.
Exits 0 on `{"ok": true}`, 1 on any error (missing env, mismatched PID, bad
reply, refused command). Prints a one-line diagnostic on stderr on failure.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any, NoReturn

USAGE = (
    "usage: autonomous-claude-ctl <compact [instructions] | clear | exit | "
    "send-keys <data>>"
)
RECV_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 2.0


def _die(msg: str, code: int = 1) -> NoReturn:
    sys.stderr.write(f"autonomous-claude-ctl: {msg}\n")
    sys.exit(code)


def _resolve_socket() -> str:
    """Return the verified socket path for THIS session, or exit."""
    pid = os.environ.get("AUTONOMOUS_CLAUDE_PID")
    sock = os.environ.get("AUTONOMOUS_CLAUDE_SOCK")
    if not pid or not sock:
        _die("not inside an autonomous-claude session (env vars unset)")
    derived = f"/tmp/autonomous-claude-{pid}.sock"
    if derived != sock:
        _die(
            f"PID/SOCK mismatch — refusing "
            f"(PID={pid} SOCK={sock} derived={derived})"
        )
    if not os.path.exists(sock):
        _die(f"wrapper socket missing: {sock}")
    return sock


def _parse_argv(argv: list[str]) -> dict[str, Any]:
    if not argv:
        _die(USAGE, code=2)
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "compact":
        if not rest:
            return {"cmd": "compact"}
        if len(rest) > 1:
            _die("compact takes at most one positional argument (instructions)", code=2)
        # Treat empty-string instructions as "no instructions" so callers don't
        # accidentally send `/compact \r` (trailing space) into the TUI.
        if rest[0] == "":
            return {"cmd": "compact"}
        return {"cmd": "compact", "instructions": rest[0]}
    if cmd == "clear":
        if rest:
            _die("clear takes no arguments", code=2)
        return {"cmd": "clear"}
    if cmd == "exit":
        if rest:
            _die("exit takes no arguments", code=2)
        return {"cmd": "exit"}
    if cmd == "send-keys":
        if len(rest) != 1:
            _die("send-keys takes exactly one argument (raw key sequence)", code=2)
        if rest[0] == "":
            _die("send-keys data must be non-empty", code=2)
        return {"cmd": "send_keys", "data": rest[0]}
    _die(f"unknown command: {cmd!r}\n{USAGE}", code=2)
    raise AssertionError  # for type-checker — _die calls sys.exit


def _request(sock_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON request, return parsed reply. Raises on protocol errors."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT_S)
    try:
        s.connect(sock_path)
    except OSError as exc:
        raise RuntimeError(f"connect failed: {exc}") from exc
    s.settimeout(RECV_TIMEOUT_S)
    try:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        s.shutdown(socket.SHUT_WR)  # tell server we're done writing
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = s.recv(4096)
            except socket.timeout as exc:
                raise RuntimeError("timed out waiting for reply") from exc
            if not chunk:
                break
            buf += chunk
            if len(buf) > 1024 * 1024:
                raise RuntimeError("reply too large")
    finally:
        try:
            s.close()
        except OSError:
            pass
    if not buf:
        raise RuntimeError("empty reply from wrapper")
    line = buf.split(b"\n", 1)[0]
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"bad reply from wrapper: {exc}") from exc
    if not isinstance(reply, dict):
        raise RuntimeError("malformed reply (not an object)")
    return reply


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    payload = _parse_argv(args)
    sock_path = _resolve_socket()
    try:
        reply = _request(sock_path, payload)
    except RuntimeError as exc:
        _die(str(exc))
    if reply.get("ok") is True:
        # Quiet success — callers (and the LLM) don't need any output.
        return 0
    err = reply.get("error") or "unknown error"
    _die(f"wrapper refused command: {err}")
    return 1  # unreachable, for type checker


if __name__ == "__main__":
    sys.exit(main())
