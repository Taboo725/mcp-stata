"""Automatic session fan-out and the all-tasks view (fork patch 16)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import mcp_stata.server as server


def _unwrap(result):
    return result.model_dump() if hasattr(result, "model_dump") else json.loads(result)


class _Worker:
    """Minimal stand-in for a pooled StataSession."""

    def __init__(self, busy=None):
        self.busy = busy
        self.status = "running"
        self._process = MagicMock()
        self._process.is_alive.return_value = True
        self._process.pid = 1

    def busy_with(self):
        return self.busy

    def queue_state(self, label):
        return None


@pytest.fixture
def pool(monkeypatch):
    """A fake session pool plus clean claim/task state."""
    sessions: dict[str, _Worker] = {}
    monkeypatch.setattr(server.session_manager, "_sessions", sessions)
    monkeypatch.setattr(server, "_auto_claims", {})
    monkeypatch.setattr(server, "_auto_capacity", None)
    monkeypatch.setenv(server.MAX_AUTO_SESSIONS_ENV, "3")
    before = set(server._background_tasks)
    yield sessions
    for key in set(server._background_tasks) - before:
        server._background_tasks.pop(key, None)


# ------------------------------------------------------------ claiming


def test_first_claims_create_sessions_up_to_the_cap(pool):
    names = [server._claim_auto_session() for _ in range(3)]
    assert names == ["auto-1", "auto-2", "auto-3"]
    # At the cap: queue on the least loaded, never on "default".
    fourth = server._claim_auto_session()
    assert fourth in names
    assert server._auto_claims[fourth] == 2


def test_idle_existing_session_is_reused(pool):
    pool["auto-1"] = _Worker()
    pool["default"] = _Worker()
    assert server._claim_auto_session() == "auto-1"


def test_busy_session_is_skipped_even_without_claims(pool):
    pool["auto-1"] = _Worker(busy="manual-task")
    assert server._claim_auto_session() == "auto-2"


def test_release_makes_session_idle_again(pool):
    a = server._claim_auto_session()
    server._release_auto_session(a)
    server._release_auto_session(a)  # idempotent past zero
    assert a not in server._auto_claims
    pool[a] = _Worker()
    assert server._claim_auto_session() == a


def test_least_loaded_wins_at_the_cap(pool):
    for name in ("auto-1", "auto-2", "auto-3"):
        pool[name] = _Worker()
    server._auto_claims.update({"auto-1": 2, "auto-2": 1, "auto-3": 3})
    assert server._claim_auto_session() == "auto-2"


def test_cap_env_is_validated(pool, monkeypatch):
    for raw, expected in (("", 3), ("abc", 3), ("0", 1), ("-2", 1), ("8", 8)):
        monkeypatch.setenv(server.MAX_AUTO_SESSIONS_ENV, raw)
        assert server._max_auto_sessions() == expected


def test_learned_capacity_caps_creation(pool):
    pool["auto-1"] = _Worker(busy="x")
    server._auto_capacity = 1
    assert server._claim_auto_session() == "auto-1"


# ------------------------------------------------------- stata_run(auto)


class _Scripted:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.status = "running"
        self._process = MagicMock()
        self._process.is_alive.return_value = True
        self._process.pid = 1

    def busy_with(self):
        return None

    def queue_state(self, label):
        return None

    async def call(self, method, params, notify_log=None, notify_progress=None, **_kw):
        await notify_log(json.dumps({"event": "log_path", "path": "x.smcl"}))
        await asyncio.sleep(self.delay)
        return {"command": params.get("code", ""), "success": True, "rc": 0, "stdout": "ok"}


@pytest.mark.asyncio
async def test_auto_run_reports_the_session_it_used_and_releases_it(pool):
    created = []

    async def get_or_create(sid, *a, **k):
        created.append(sid)
        pool.setdefault(sid, _Scripted())
        return pool[sid]

    with patch.object(server.session_manager, "get_or_create_session", side_effect=get_or_create):
        res = _unwrap(await server.stata_run("display 1", session_id="auto"))
    assert res["session_id"] == "auto-1"
    assert created == ["auto-1"]
    assert server._auto_claims == {}


@pytest.mark.asyncio
async def test_concurrent_auto_runs_land_on_distinct_sessions(pool):
    async def get_or_create(sid, *a, **k):
        await asyncio.sleep(0.05)  # creation takes time; claims must already be held
        pool.setdefault(sid, _Scripted(delay=0.3))
        return pool[sid]

    with patch.object(server.session_manager, "get_or_create_session", side_effect=get_or_create):
        results = await asyncio.gather(*(server.stata_run(f"display {i}", session_id="auto") for i in range(3)))
    used = sorted(_unwrap(r)["session_id"] for r in results)
    assert used == ["auto-1", "auto-2", "auto-3"]
    assert server._auto_claims == {}


@pytest.mark.asyncio
async def test_failed_creation_falls_back_and_lowers_capacity(pool):
    pool["auto-1"] = _Scripted()

    async def get_or_create(sid, *a, **k):
        if sid not in pool:
            raise RuntimeError("license: no free seats")
        return pool[sid]

    pool["auto-1"].busy_with = lambda: "someone"  # force a new session to be tried
    with patch.object(server.session_manager, "get_or_create_session", side_effect=get_or_create):
        res = _unwrap(await server.stata_run("display 1", session_id="auto"))
    assert res["success"] is True
    assert res["session_id"] == "auto-1"
    assert any("could not start" in w for w in res["warnings"])
    assert server._auto_capacity == 1
    assert server._auto_claims == {}


@pytest.mark.asyncio
async def test_failed_creation_with_nothing_to_fall_back_to_raises(pool):
    async def get_or_create(sid, *a, **k):
        raise RuntimeError("license: no free seats")

    with patch.object(server.session_manager, "get_or_create_session", side_effect=get_or_create):
        with pytest.raises(RuntimeError, match="no free seats"):
            await server.stata_run("display 1", session_id="auto")
    assert server._auto_claims == {}


@pytest.mark.asyncio
async def test_explicit_session_ids_are_untouched(pool):
    seen = []

    async def get_or_create(sid, *a, **k):
        seen.append(sid)
        pool.setdefault(sid, _Scripted())
        return pool[sid]

    with patch.object(server.session_manager, "get_or_create_session", side_effect=get_or_create):
        await server.stata_run("display 1")
        await server.stata_run("display 1", session_id="mine")
    assert seen == ["default", "mine"]
    assert server._auto_claims == {}


# --------------------------------------------------------- all-tasks view


def _task(task_id, *, done=False, error=None, session_id="default"):
    info = server.BackgroundTask(
        task_id=task_id, kind="command", task=None, created_at=datetime.now(timezone.utc), session_id=session_id
    )
    if done:
        info.done = True
        info.error = error
        info.progress.mark_finished()
    server._background_tasks[task_id] = info
    return info


@pytest.mark.asyncio
async def test_all_lists_active_and_newly_finished_once(pool):
    _task("r1", session_id="auto-1")
    _task("d1", done=True)
    _task("f1", done=True, error="r(198)")
    first = _unwrap(await server.stata_task_status("all"))
    rows = {t["task_id"]: t for t in first["data"]["tasks"]}
    assert set(rows) >= {"r1", "d1", "f1"}
    assert rows["r1"]["status"] == "running" and rows["r1"]["session_id"] == "auto-1"
    assert rows["d1"]["status"] == "done"
    assert rows["f1"]["status"] == "failed" and rows["f1"]["error"] == "r(198)"
    assert "summary" in rows["r1"]
    assert first["data"]["counts"]["running"] >= 1

    second = _unwrap(await server.stata_task_status("all"))
    ids = {t["task_id"] for t in second["data"]["tasks"]}
    assert "r1" in ids
    assert not ids & {"d1", "f1"}, "finished tasks are reported once"


@pytest.mark.asyncio
async def test_all_wait_returns_when_any_task_finishes(pool):
    slow = _task("slow")
    quick = _task("quick")
    _unwrap(await server.stata_task_status("all"))  # nothing finished yet

    async def finish():
        await asyncio.sleep(0.3)
        quick.progress.mark_finished()
        quick.done = True

    t = asyncio.create_task(finish())
    start = time.monotonic()
    res = _unwrap(await server.stata_task_status("all", wait=True, timeout=10, poll_interval=0.05))
    await t
    assert time.monotonic() - start < 3
    rows = {r["task_id"]: r for r in res["data"]["tasks"]}
    assert rows["quick"]["status"] == "done"
    assert rows["slow"]["status"] == "running"
    slow.done = True


@pytest.mark.asyncio
async def test_all_wait_with_nothing_active_returns_immediately(pool):
    start = time.monotonic()
    res = _unwrap(await server.stata_task_status("all", wait=True, timeout=10))
    assert time.monotonic() - start < 1
    assert res["success"] is True


@pytest.mark.asyncio
async def test_single_task_status_marks_it_reported(pool):
    _task("solo", done=True)
    await server.stata_task_status("solo")
    res = _unwrap(await server.stata_task_status("all"))
    assert "solo" not in {t["task_id"] for t in res["data"]["tasks"]}
