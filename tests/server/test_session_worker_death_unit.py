"""A dead worker or a stopped session must not strand pending requests.

The parent keeps its own copy of the child's pipe end, so a worker that dies
(crash, OOM kill, taskkill) did not always surface as EOF, and ``stop()``
never resolved what was in flight. Either way the caller's future never
completed and a background task reported "running" forever.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


class _FakeProcess:
    def __init__(self, alive=True):
        self._alive = alive
        self.exitcode = None if alive else 1

    def is_alive(self):
        return self._alive

def _bare_session():
    from mcp_stata.sessions import StataSession

    s = StataSession.__new__(StataSession)
    s.id = "t"
    s.status = "running"
    s._stopping = False
    s._listener_running = True
    s._pending_requests = {}
    s._log_listeners = {}
    s._progress_listeners = {}
    s._process = _FakeProcess(alive=True)
    return s


@pytest.mark.asyncio
async def test_closed_pipe_fails_pending_requests():
    s = _bare_session()
    conn = MagicMock()
    conn.poll.side_effect = EOFError()
    s._parent_conn = conn
    fut = asyncio.get_running_loop().create_future()
    s._pending_requests["m1"] = fut
    await s._listen_to_worker()
    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        fut.result()
    assert s._pending_requests == {}


@pytest.mark.asyncio
async def test_broken_pipe_on_recv_fails_pending_requests():
    s = _bare_session()
    conn = MagicMock()
    conn.poll.return_value = True
    conn.recv.side_effect = BrokenPipeError()
    s._parent_conn = conn
    fut = asyncio.get_running_loop().create_future()
    s._pending_requests["m1"] = fut
    await s._listen_to_worker()
    assert isinstance(fut.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_raising_callback_does_not_kill_listener():
    s = _bare_session()
    msgs = [
        {"event": "log", "id": "m1", "text": "hi"},
        {"event": "progress", "id": "m1", "progress": 1, "total": 2, "message": "x"},
        {"event": "result", "id": "m1", "result": {"ok": 1}},
    ]
    conn = MagicMock()
    conn.poll.side_effect = [True, True, True] + [False] * 1000
    conn.recv.side_effect = msgs
    s._parent_conn = conn
    fut = asyncio.get_running_loop().create_future()
    s._pending_requests["m1"] = fut

    async def bad(*_a):
        raise ValueError("consumer bug")

    s._log_listeners["m1"] = [bad]
    s._progress_listeners["m1"] = [bad]
    listener = asyncio.create_task(s._listen_to_worker())
    result = await asyncio.wait_for(fut, timeout=5)
    s._listener_running = False
    await asyncio.wait_for(listener, timeout=5)
    assert result == {"ok": 1}
    assert s.status != "error"


@pytest.mark.asyncio
async def test_listener_replaced_while_worker_alive_keeps_pending():
    """Cancelling the listener (e.g. loop change) must not fail live requests."""
    s = _bare_session()
    conn = MagicMock()
    conn.poll.return_value = False
    s._parent_conn = conn
    fut = asyncio.get_running_loop().create_future()
    s._pending_requests["m1"] = fut
    listener = asyncio.create_task(s._listen_to_worker())
    await asyncio.sleep(0.1)
    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listener
    assert not fut.done()


@pytest.mark.asyncio
async def test_stop_fails_pending_requests():
    s = _bare_session()
    s._parent_conn = MagicMock()
    s._process = _FakeProcess(alive=False)
    s._history = []
    s._last_diff_snapshot = None
    s._listener_task = None
    fut = asyncio.get_running_loop().create_future()
    s._pending_requests["m1"] = fut
    await s.stop()
    with pytest.raises(RuntimeError, match="was stopped"):
        fut.result()


@pytest.mark.asyncio
async def test_dead_worker_without_eof_fails_pending_requests():
    """No EOF ever arrives (Windows), but the process is gone."""
    s = _bare_session()
    conn = MagicMock()
    conn.poll.return_value = False
    s._parent_conn = conn
    s._process = _FakeProcess(alive=False)
    fut = asyncio.get_running_loop().create_future()
    s._pending_requests["m1"] = fut
    await asyncio.wait_for(s._listen_to_worker(), timeout=5)
    with pytest.raises(RuntimeError, match="exit code 1"):
        fut.result()


@pytest.mark.asyncio
async def test_idle_dead_worker_does_not_spin_listener_into_error():
    """With nothing pending the listener just keeps polling; get_or_create handles rebuilds."""
    s = _bare_session()
    conn = MagicMock()
    conn.poll.return_value = False
    s._parent_conn = conn
    s._process = _FakeProcess(alive=False)
    listener = asyncio.create_task(s._listen_to_worker())
    await asyncio.sleep(0.2)
    assert not listener.done()
    s._listener_running = False
    await asyncio.wait_for(listener, timeout=5)
