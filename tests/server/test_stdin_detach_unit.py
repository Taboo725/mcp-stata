"""Regression tests for the worker-spawn stdin deadlock.

On Windows a spawned worker inherits the server's fd 0.  While the server is
parked in a synchronous read of the stdio pipe, the worker's interpreter
start-up blocks seeking that same pipe, so the first call on a fresh session
times out.  ``_detach_stdin_from_children`` points fd 0 at the null device and
keeps the transport on a private duplicate.
"""
import os
import sys

import pytest

from mcp_stata import server as server_module


@pytest.fixture
def restored_stdin():
    """Give the test a private fd 0 and put the real one back afterwards."""
    saved_fd = os.dup(0)
    saved_stdin = sys.stdin
    saved_flag = server_module._STDIN_DETACHED
    server_module._STDIN_DETACHED = False
    try:
        yield
    finally:
        os.dup2(saved_fd, 0)
        os.close(saved_fd)
        sys.stdin = saved_stdin
        server_module._STDIN_DETACHED = saved_flag


@pytest.mark.skipif(os.name != "nt", reason="the deadlock and the fix are Windows-only")
def test_detach_moves_transport_off_fd_zero(restored_stdin):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"hello\n")
    os.close(write_fd)
    os.dup2(read_fd, 0)
    os.close(read_fd)
    sys.stdin = os.fdopen(os.dup(0), "r")

    server_module._detach_stdin_from_children()

    # The transport still sees the piped bytes...
    assert sys.stdin.readline() == "hello\n"
    # ...but a child inheriting fd 0 gets the null device, not the pipe.
    assert os.read(0, 16) == b""
    assert server_module._STDIN_DETACHED is True


@pytest.mark.skipif(os.name != "nt", reason="the deadlock and the fix are Windows-only")
def test_detach_is_idempotent(restored_stdin):
    server_module._detach_stdin_from_children()
    first = sys.stdin
    server_module._detach_stdin_from_children()
    assert sys.stdin is first


@pytest.mark.skipif(os.name != "nt", reason="the deadlock and the fix are Windows-only")
def test_detach_can_be_disabled(monkeypatch, restored_stdin):
    monkeypatch.setenv("MCP_STATA_NO_STDIN_DETACH", "1")
    before = sys.stdin
    server_module._detach_stdin_from_children()
    assert sys.stdin is before
    assert server_module._STDIN_DETACHED is False
