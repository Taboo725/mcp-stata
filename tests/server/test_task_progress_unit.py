"""Live progress for long-running executions (fork patch 10).

Covers the progress record itself, the CPU probe behind the stall diagnosis,
the capped ``stata_task_status`` wait, and auto-promotion of slow synchronous
runs to background tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from mcp_stata import task_progress
from mcp_stata.task_progress import TaskProgress, build_snapshot


def _unwrap(result):
    return result.model_dump() if hasattr(result, "model_dump") else json.loads(result)


# --------------------------------------------------------------------------
# TaskProgress


def test_chunk_tracks_last_line_and_current_command():
    rec = TaskProgress()
    rec.observe_chunk(". use auto, clear\n(1978 automobile data)\n")
    rec.observe_chunk(". regress price mpg weight\n\n      Source |       SS\n")
    assert rec.current_command == "regress price mpg weight"
    assert rec.last_line == "Source |       SS"
    assert rec.output_chars > 0
    assert rec.last_output_mono is not None


def test_chunk_strips_smcl_and_keeps_command_from_earlier_line_in_chunk():
    rec = TaskProgress()
    rec.observe_chunk("{com}. reghdfe y x, absorb(firm)\n{txt}(dropped 3 singletons)\n")
    assert rec.current_command == "reghdfe y x, absorb(firm)"
    assert rec.last_line == "(dropped 3 singletons)"


def test_json_events_are_not_output():
    rec = TaskProgress()
    rec.observe_chunk(json.dumps({"event": "log_path", "path": "x.log"}))
    assert rec.output_chars == 0
    assert rec.last_output_mono is None


def test_observers_never_raise():
    rec = TaskProgress()
    rec.observe_chunk(None)  # type: ignore[arg-type]
    rec.observe_chunk(12345)  # type: ignore[arg-type]
    rec.observe_progress("not-a-number", object(), None)
    build_snapshot(rec, status="running")


def test_long_lines_are_clipped():
    rec = TaskProgress()
    rec.observe_chunk(". " + "x" * 1000 + "\n")
    assert len(rec.current_command) <= 160


def test_snapshot_reports_do_file_fraction():
    rec = TaskProgress()
    rec.observe_progress(37, 120, "Executing do-file: 37/120")
    snap = build_snapshot(rec, status="running")
    assert (snap["lines_done"], snap["lines_total"]) == (37, 120)
    assert snap["percent"] == pytest.approx(30.8)
    assert "37/120" in snap["summary"]


def test_snapshot_omits_meaningless_fraction():
    rec = TaskProgress()
    rec.observe_progress(0, None, "Running command")
    snap = build_snapshot(rec, status="running")
    assert "percent" not in snap
    rec.observe_progress(1, 1, "Finished")
    assert "percent" not in build_snapshot(rec, status="running")


def test_finished_snapshot_freezes_elapsed_and_drops_quiet():
    rec = TaskProgress(started_mono=time.monotonic() - 10)
    rec.mark_finished()
    snap = build_snapshot(rec, status="done", now=time.monotonic() + 1000)
    assert snap["elapsed_s"] == pytest.approx(10, abs=0.5)
    assert "quiet_for_s" not in snap
    assert snap["state"] == "done"


@pytest.mark.parametrize(
    "quiet, alive, cpu, expected",
    [
        (5, True, None, "running"),
        (500, True, 180.0, "busy_silent"),
        (500, True, 0.4, "possibly_stuck"),
        (500, True, None, "quiet"),
        (5, False, None, "worker_dead"),
    ],
)
def test_state_classification(monkeypatch, quiet, alive, cpu, expected):
    monkeypatch.setenv(task_progress.STALL_SECONDS_ENV, "60")
    now = time.monotonic()
    rec = TaskProgress(started_mono=now - quiet - 1, last_output_mono=now - quiet)
    snap = build_snapshot(rec, status="running", worker_alive=alive, cpu_percent=cpu, now=now)
    assert snap["state"] == expected
    advice = task_progress.stall_advice(snap, "s1")
    if expected == "possibly_stuck":
        assert "stata_control(action='break', id='s1')" in advice
    if expected == "running":
        assert advice is None


def test_bad_stall_env_falls_back(monkeypatch):
    for raw in ("abc", "-5", "0"):
        monkeypatch.setenv(task_progress.STALL_SECONDS_ENV, raw)
        assert task_progress.stall_seconds() == task_progress.DEFAULT_STALL_SECONDS


# --------------------------------------------------------------------------
# CPU probe


@pytest.mark.skipif(sys.platform == "darwin", reason="no CPU probe on macOS")
def test_cpu_seconds_of_self_grows_when_busy():
    before = task_progress.process_cpu_seconds(os.getpid())
    assert before is not None
    end = time.perf_counter() + 0.3
    while time.perf_counter() < end:
        pass
    after = task_progress.process_cpu_seconds(os.getpid())
    assert after - before >= 0.1


def test_cpu_seconds_unknown_pid_is_none():
    assert task_progress.process_cpu_seconds(None) is None
    assert task_progress.process_cpu_seconds(2**30) is None


@pytest.mark.skipif(sys.platform == "darwin", reason="no CPU probe on macOS")
@pytest.mark.asyncio
async def test_cpu_percent_distinguishes_busy_from_idle():
    import subprocess

    # A Windows venv's python.exe is a launcher that idles while a child
    # interpreter does the work; measure the real interpreter instead.
    exe = getattr(sys, "_base_executable", None) or sys.executable
    busy = subprocess.Popen([exe, "-c", "while True: pass"])
    idle = subprocess.Popen([exe, "-c", "import time; time.sleep(30)"])
    try:
        await asyncio.sleep(0.3)
        busy_pct = await task_progress.sample_cpu_percent(TaskProgress(), busy.pid)
        idle_pct = await task_progress.sample_cpu_percent(TaskProgress(), idle.pid)
    finally:
        busy.kill()
        idle.kill()
        busy.wait()
        idle.wait()
    assert busy_pct > 50
    assert idle_pct < 5


# --------------------------------------------------------------------------
# Server: stata_task_status


class _FakeProcess:
    def __init__(self, alive=True, pid=None):
        self._alive = alive
        self.pid = pid or os.getpid()

    def is_alive(self):
        return self._alive


class _FakeWorker:
    def __init__(self, alive=True):
        self._process = _FakeProcess(alive)


def _register(task_id, **kw):
    from mcp_stata.server import BackgroundTask, _background_tasks

    info = BackgroundTask(
        task_id=task_id, kind="do_file", task=None, created_at=datetime.now(timezone.utc), **kw
    )
    _background_tasks[task_id] = info
    return info


@pytest.fixture
def cleanup_tasks():
    from mcp_stata.server import _background_tasks

    before = set(_background_tasks)
    yield
    for key in set(_background_tasks) - before:
        _background_tasks.pop(key, None)


@pytest.mark.asyncio
async def test_status_includes_progress_snapshot(cleanup_tasks):
    from mcp_stata.server import stata_task_status

    info = _register("prog1", worker_session=_FakeWorker())
    info.progress.observe_progress(12, 48, "Executing do-file: 12/48")
    info.progress.observe_chunk(". xtreg y x, fe\n")
    res = _unwrap(await stata_task_status("prog1"))
    progress = res["data"]["progress"]
    assert res["data"]["status"] == "running"
    assert progress["percent"] == 25.0
    assert progress["current_command"] == "xtreg y x, fe"
    assert progress["worker_alive"] is True
    assert progress["state"] == "running"
    assert any("Still running" in a for a in res["next_actions"])


@pytest.mark.asyncio
async def test_wait_is_capped_so_long_jobs_report_back(cleanup_tasks, monkeypatch):
    from mcp_stata.server import TASK_WAIT_MAX_ENV, stata_task_status

    monkeypatch.setenv(TASK_WAIT_MAX_ENV, "0.3")
    _register("capped", worker_session=_FakeWorker())
    start = time.monotonic()
    res = _unwrap(await stata_task_status("capped", wait=True, timeout=600, poll_interval=0.05))
    assert time.monotonic() - start < 3
    assert res["data"]["status"] == "timeout"
    assert "still running" in res["data"]["error"]
    assert "progress" in res["data"]


@pytest.mark.asyncio
async def test_wait_cap_zero_disables_cap(cleanup_tasks, monkeypatch):
    from mcp_stata.server import TASK_WAIT_MAX_ENV, stata_task_status

    monkeypatch.setenv(TASK_WAIT_MAX_ENV, "0")
    info = _register("uncapped", worker_session=_FakeWorker())

    async def finish():
        await asyncio.sleep(0.4)
        info.progress.mark_finished()
        info.done = True

    t = asyncio.create_task(finish())
    res = _unwrap(await stata_task_status("uncapped", wait=True, timeout=5, poll_interval=0.05))
    await t
    assert res["data"]["status"] == "done"
    assert res["data"]["progress"]["state"] == "done"


@pytest.mark.asyncio
async def test_silent_idle_worker_is_flagged_possibly_stuck(cleanup_tasks, monkeypatch):
    from mcp_stata.server import stata_task_status

    monkeypatch.setenv(task_progress.STALL_SECONDS_ENV, "1")
    info = _register("stuck", worker_session=_FakeWorker(), session_id="sx")
    info.progress.started_mono -= 30
    with patch.object(task_progress, "sample_cpu_percent", return_value=0.0) as probe:
        res = _unwrap(await stata_task_status("stuck"))
    probe.assert_awaited_once()
    assert res["data"]["progress"]["state"] == "possibly_stuck"
    assert any("stata_control(action='break', id='sx')" in a for a in res["next_actions"])


@pytest.mark.asyncio
async def test_dead_worker_is_reported(cleanup_tasks):
    from mcp_stata.server import stata_task_status

    _register("dead", worker_session=_FakeWorker(alive=False))
    res = _unwrap(await stata_task_status("dead"))
    assert res["data"]["progress"]["state"] == "worker_dead"


@pytest.mark.asyncio
async def test_cpu_not_probed_while_output_is_flowing(cleanup_tasks):
    from mcp_stata.server import stata_task_status

    info = _register("chatty", worker_session=_FakeWorker())
    info.progress.observe_chunk(". summarize\n")
    with patch.object(task_progress, "sample_cpu_percent") as probe:
        await stata_task_status("chatty")
    probe.assert_not_called()


# --------------------------------------------------------------------------
# Server: stata_run auto-background


class _ScriptedSession:
    """Stands in for a StataSession: streams output, then returns a result."""

    def __init__(self, duration: float, total_lines: int = 4):
        self.duration = duration
        self.total_lines = total_lines
        self._process = _FakeProcess()
        self.cancelled = False

    async def call(self, method, params, notify_log=None, notify_progress=None):
        try:
            await notify_log(json.dumps({"event": "log_path", "path": "fake.smcl"}))
            for i in range(1, self.total_lines + 1):
                await notify_log(f". display {i}\n{i}\n")
                await notify_progress(float(i), float(self.total_lines), f"Executing do-file: {i}/{self.total_lines}")
                await asyncio.sleep(self.duration / self.total_lines)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"command": "fake", "success": True, "rc": 0, "stdout": "all done", "log_path": "fake.smcl"}


@pytest.mark.asyncio
async def test_fast_sync_run_returns_inline(cleanup_tasks, monkeypatch):
    from mcp_stata.server import AUTO_BACKGROUND_ENV, stata_run

    monkeypatch.setenv(AUTO_BACKGROUND_ENV, "5")
    fake = _ScriptedSession(duration=0.05)
    with _install(fake):
        res = _unwrap(await stata_run("display 1"))
    assert res["success"] is True
    assert not (res.get("data") or {}).get("auto_backgrounded")


@pytest.mark.asyncio
async def test_slow_sync_run_is_promoted_and_completes(cleanup_tasks, monkeypatch):
    from mcp_stata.server import AUTO_BACKGROUND_ENV, stata_run, stata_task_status

    monkeypatch.setenv(AUTO_BACKGROUND_ENV, "0.2")
    fake = _ScriptedSession(duration=1.2, total_lines=6)
    with _install(fake):
        start = time.monotonic()
        res = _unwrap(await stata_run("display 1"))
        assert time.monotonic() - start < 1.0
        data = res["data"]
        assert data["auto_backgrounded"] is True
        assert data["status"] == "started"
        assert data["progress"]["elapsed_s"] >= 0.2
        task_id = data["task_id"]

        mid = _unwrap(await stata_task_status(task_id))
        assert mid["data"]["status"] == "running"
        assert mid["data"]["progress"]["lines_done"] >= 1

        final = _unwrap(await stata_task_status(task_id, wait=True, timeout=10, poll_interval=0.05))
    assert final["data"]["status"] == "done"
    assert final["data"]["progress"]["state"] == "done"
    assert final["data"]["progress"]["lines_done"] == 6
    assert final["data"]["result"] is not None


@pytest.mark.asyncio
async def test_auto_background_disabled_waits_inline(cleanup_tasks, monkeypatch):
    from mcp_stata.server import AUTO_BACKGROUND_ENV, stata_run

    monkeypatch.setenv(AUTO_BACKGROUND_ENV, "0")
    fake = _ScriptedSession(duration=0.6)
    with _install(fake):
        res = _unwrap(await stata_run("display 1"))
    assert not (res.get("data") or {}).get("auto_backgrounded")
    assert res["success"] is True


@pytest.mark.asyncio
async def test_cancelling_sync_request_cancels_the_stata_call(cleanup_tasks, monkeypatch):
    from mcp_stata.server import AUTO_BACKGROUND_ENV, stata_run

    monkeypatch.setenv(AUTO_BACKGROUND_ENV, "30")
    fake = _ScriptedSession(duration=20)
    with _install(fake):
        call = asyncio.create_task(stata_run("display 1"))
        await asyncio.sleep(0.3)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0.05)
    assert fake.cancelled


@pytest.mark.asyncio
async def test_cancel_tool_stops_promoted_task(cleanup_tasks, monkeypatch):
    from mcp_stata.server import AUTO_BACKGROUND_ENV, stata_control, stata_run, stata_task_status

    monkeypatch.setenv(AUTO_BACKGROUND_ENV, "0.2")
    fake = _ScriptedSession(duration=20)
    with _install(fake):
        res = _unwrap(await stata_run("display 1"))
        task_id = res["data"]["task_id"]
        await stata_control(action="cancel", id=task_id)
        final = _unwrap(await stata_task_status(task_id, wait=True, timeout=5, poll_interval=0.05))
    assert final["data"]["status"] == "failed"
    assert fake.cancelled


@pytest.mark.asyncio
async def test_error_before_budget_still_raises(cleanup_tasks, monkeypatch):
    from mcp_stata.server import AUTO_BACKGROUND_ENV, stata_run

    monkeypatch.setenv(AUTO_BACKGROUND_ENV, "5")

    async def boom(_sid):
        raise RuntimeError("session failed to start")

    with patch("mcp_stata.server.session_manager.get_or_create_session", side_effect=boom):
        with pytest.raises(RuntimeError, match="failed to start"):
            await stata_run("display 1")


def _awaitable(value):
    async def _get(*_a, **_k):
        return value

    return _get


# patch(..., return_value=coroutine-function) would hand back the function
# itself; wrap so get_or_create_session(...) awaits to the fake.
def _install(fake):
    return patch("mcp_stata.server.session_manager.get_or_create_session", new=_awaitable(fake))
