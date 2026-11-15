"""Queue visibility and queue-safe cancellation (fork patch 15).

A Stata worker runs one request at a time. These tests pin down that the
session knows which request is executing and which are waiting, that the
server reports a waiting task as ``queued`` rather than ``running``, and
that cancelling a waiting request withdraws it instead of sending a break
that would kill the request ahead of it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest


def _session():
    from mcp_stata.sessions import StataSession

    s = StataSession.__new__(StataSession)
    s.id = "q"
    s.status = "running"
    s._stopping = False
    s._listener_running = True
    s._listener_task = None
    s._pending_requests = {}
    s._log_listeners = {}
    s._progress_listeners = {}
    s._process = MagicMock()
    s._process.is_alive.return_value = True
    s._parent_conn = MagicMock()

    async def _no_listener():
        return None

    s._ensure_listener = _no_listener
    return s


def _sent(s):
    return [c.args[0] for c in s._parent_conn.send.call_args_list]


async def _start(s, label):
    task = asyncio.create_task(s._call_raw("run_command", {"code": label}, label=label))
    await asyncio.sleep(0)
    return task


def _msg_id(s, label):
    return next(m["id"] for m in _sent(s) if m.get("args", {}).get("code") == label)


@pytest.mark.asyncio
async def test_queue_order_follows_send_order_and_completion():
    s = _session()
    a = await _start(s, "A")
    b = await _start(s, "B")
    c = await _start(s, "C")
    assert s.queue_state("A") == {"position": 0, "executing": "A"}
    assert s.queue_state("B") == {"position": 1, "executing": "A"}
    assert s.queue_state("C") == {"position": 2, "executing": "A"}
    assert s.busy_with() == "A"

    await s._handle_worker_msg({"event": "result", "id": _msg_id(s, "A"), "result": {"ok": "A"}})
    assert await a == {"ok": "A"}
    assert s.queue_state("B") == {"position": 0, "executing": "B"}
    assert s.queue_state("C") == {"position": 1, "executing": "B"}
    assert s.queue_state("A") is None

    await s._handle_worker_msg({"event": "error", "id": _msg_id(s, "B"), "message": "r(198)"})
    with pytest.raises(RuntimeError):
        await b
    assert s.queue_state("C") == {"position": 0, "executing": "C"}
    c.cancel()


@pytest.mark.asyncio
async def test_cancelling_queued_request_withdraws_it_without_break():
    s = _session()
    a = await _start(s, "A")
    b = await _start(s, "B")
    b.cancel()
    # _call_raw waits up to 3s for the worker's answer; supply it.
    await asyncio.sleep(0.05)
    await s._handle_worker_msg({"event": "error", "id": _msg_id(s, "B"), "message": "Cancelled while queued"})
    with pytest.raises(asyncio.CancelledError):
        await b
    kinds = [m.get("type") for m in _sent(s)]
    assert "break" not in kinds, "a break would have interrupted A"
    assert {"type": "cancel", "target": _msg_id(s, "B")} in _sent(s)
    assert s.queue_state("A") == {"position": 0, "executing": "A"}
    a.cancel()


@pytest.mark.asyncio
async def test_cancelling_executing_request_sends_break():
    s = _session()
    a = await _start(s, "A")
    await _start(s, "B")
    a.cancel()
    await asyncio.sleep(0.05)
    await s._handle_worker_msg({"event": "error", "id": _msg_id(s, "A"), "message": "--Break-- r(1)"})
    with pytest.raises(asyncio.CancelledError):
        await a
    assert {"type": "break"} in _sent(s)
    assert not any(m.get("type") == "cancel" for m in _sent(s))
    assert s.queue_state("B") == {"position": 0, "executing": "B"}


@pytest.mark.asyncio
async def test_failing_all_pending_clears_the_queue():
    s = _session()
    await _start(s, "A")
    await _start(s, "B")
    s._fail_pending("gone")
    assert s.busy_with() is None
    assert s.queue_state("B") is None


@pytest.mark.asyncio
async def test_unlabelled_calls_are_reported_by_method():
    s = _session()
    t = asyncio.create_task(s._call_raw("get_data", {}))
    await asyncio.sleep(0)
    await _start(s, "B")
    assert s.queue_state("B") == {"position": 1, "executing": "get_data"}
    t.cancel()


# ------------------------------------------------------------------ server


class _QueuedWorker:
    def __init__(self, position=1, executing="other-task"):
        self.position = position
        self.executing = executing
        self._process = MagicMock()
        self._process.is_alive.return_value = True
        self._process.pid = 1

    def queue_state(self, label):
        if self.position is None:
            return None
        return {"position": self.position, "executing": self.executing}


@pytest.fixture
def tasks():
    from mcp_stata.server import BackgroundTask, _background_tasks

    made = []

    def make(task_id, **kw):
        info = BackgroundTask(task_id=task_id, kind="command", task=None, created_at=datetime.now(timezone.utc), **kw)
        _background_tasks[task_id] = info
        made.append(task_id)
        return info

    yield make
    for task_id in made:
        _background_tasks.pop(task_id, None)


def _unwrap(result):
    return result.model_dump() if hasattr(result, "model_dump") else json.loads(result)


@pytest.mark.asyncio
async def test_status_reports_queued_behind_running_task(tasks, monkeypatch):
    from mcp_stata import task_progress
    from mcp_stata.server import stata_task_status

    monkeypatch.setenv(task_progress.STALL_SECONDS_ENV, "1")
    tasks("ahead")
    info = tasks("waiting", worker_session=_QueuedWorker(executing="ahead"))
    info.progress.started_mono -= 600  # queued for ten minutes: must not read as stuck
    res = _unwrap(await stata_task_status("waiting"))
    assert res["success"] is True
    assert res["data"]["status"] == "queued"
    progress = res["data"]["progress"]
    assert progress["state"] == "queued"
    assert progress["queued_behind"] == "task ahead"
    assert "quiet_for_s" not in progress
    assert "waiting for task ahead" in progress["summary"]


@pytest.mark.asyncio
async def test_queued_wait_returns_queued_not_timeout(tasks, monkeypatch):
    from mcp_stata.server import TASK_WAIT_MAX_ENV, stata_task_status

    monkeypatch.setenv(TASK_WAIT_MAX_ENV, "0.2")
    tasks("w2", worker_session=_QueuedWorker(executing="get_data"))
    res = _unwrap(await stata_task_status("w2", wait=True, timeout=30, poll_interval=0.05))
    assert res["data"]["status"] == "queued"
    assert res["data"]["progress"]["queued_behind"] == "another request (get_data)"


@pytest.mark.asyncio
async def test_executing_task_is_running(tasks):
    from mcp_stata.server import stata_task_status

    tasks("head", worker_session=_QueuedWorker(position=0, executing="head"))
    res = _unwrap(await stata_task_status("head"))
    assert res["data"]["status"] == "running"
    assert res["data"]["progress"]["state"] == "running"


@pytest.mark.asyncio
async def test_wait_for_log_path_returns_once_queued(tasks):
    from mcp_stata.server import _wait_for_log_path

    info = tasks("lp", worker_session=_QueuedWorker())
    await asyncio.wait_for(_wait_for_log_path(info), timeout=2)
    assert info.log_path is None


def test_queued_time_is_reported_once_execution_starts():
    import time

    from mcp_stata.task_progress import TaskProgress, build_snapshot

    rec = TaskProgress(started_mono=time.monotonic() - 30)
    rec.observe_progress(0, None, "Running command")
    snap = build_snapshot(rec, status="running")
    assert snap["queued_s"] == pytest.approx(30, abs=1)
    assert snap["quiet_for_s"] < 2, "quiet time must count from execution start, not submission"
