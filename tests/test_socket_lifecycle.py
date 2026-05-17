from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from autonomous_claude import (
    atomic_write_discovery,
    create_control_socket,
    remove_discovery_if_ours,
)


def test_socket_created_with_0600_perms(tmp_path: Path) -> None:
    sock_path = str(tmp_path / "ctl.sock")
    srv = create_control_socket(sock_path)
    try:
        st = os.stat(sock_path)
        # mask off file-type bits
        mode = stat.S_IMODE(st.st_mode)
        assert mode == 0o600
    finally:
        srv.close()
        os.unlink(sock_path)


def test_socket_listens_and_accepts(tmp_path: Path) -> None:
    sock_path = str(tmp_path / "ctl.sock")
    srv = create_control_socket(sock_path)
    try:
        srv.setblocking(True)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        client.sendall(b'{"cmd":"clear"}\n')
        conn, _ = srv.accept()
        data = conn.recv(64)
        assert data == b'{"cmd":"clear"}\n'
        conn.close()
        client.close()
    finally:
        srv.close()
        os.unlink(sock_path)


def test_create_socket_replaces_stale_file(tmp_path: Path) -> None:
    sock_path = str(tmp_path / "ctl.sock")
    Path(sock_path).write_text("stale")
    srv = create_control_socket(sock_path)
    try:
        # Now it must be a socket, not a regular file.
        assert stat.S_ISSOCK(os.stat(sock_path).st_mode)
    finally:
        srv.close()
        os.unlink(sock_path)


def test_discovery_atomic_write(tmp_path: Path) -> None:
    discovery = tmp_path / "discovery.path"
    atomic_write_discovery("/tmp/x.sock", discovery=discovery)
    assert discovery.read_text() == "/tmp/x.sock"
    # No .tmp left behind
    assert not (tmp_path / "discovery.path.tmp").exists()


def test_discovery_overwrite(tmp_path: Path) -> None:
    discovery = tmp_path / "discovery.path"
    atomic_write_discovery("/tmp/a.sock", discovery=discovery)
    atomic_write_discovery("/tmp/b.sock", discovery=discovery)
    assert discovery.read_text() == "/tmp/b.sock"


def test_remove_discovery_only_if_ours(tmp_path: Path) -> None:
    discovery = tmp_path / "discovery.path"
    discovery.write_text("/tmp/someone-else.sock")
    remove_discovery_if_ours("/tmp/mine.sock", discovery=discovery)
    assert discovery.exists()  # not ours, left alone

    discovery.write_text("/tmp/mine.sock")
    remove_discovery_if_ours("/tmp/mine.sock", discovery=discovery)
    assert not discovery.exists()


def test_remove_discovery_missing_is_ok(tmp_path: Path) -> None:
    discovery = tmp_path / "absent.path"
    remove_discovery_if_ours("/tmp/x.sock", discovery=discovery)  # should not raise
