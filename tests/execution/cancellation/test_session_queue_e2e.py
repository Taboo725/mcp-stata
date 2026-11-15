"""End-to-end: work queued behind a busy session is visible and safely cancellable."""

from __future__ import annotations

import time

import anyio
import pytest

from stdio_harness import Client, run_with_server

pytestmark = [pytest.mark.requires_stata, pytest.mark.integration, pytest.mark.xdist_group("stata_heavy")]


async def _wait_state(c: Client, task_id: str, wanted: str, limit: float = 30.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        res = await c.status(task_id)
        if res["data"]["status"] == wanted:
            return res
        await anyio.sleep(0.2)
    raise AssertionError(f"{task_id} never reached {wanted}")


def test_second_background_job_returns_at_once_as_queued(tmp_path):
    async def body(c: Client):
        a = await c.call("stata_run", code='sleep 8000\ndisplay "A done"', background=True)
        a_id = a["data"]["task_id"]
        await _wait_state(c, a_id, "running")

        t0 = time.monotonic()
        b = await c.call("stata_run", code='display "B done"', background=True)
        assert time.monotonic() - t0 < 3, "submitting behind a busy session blocked"
        b_id = b["data"]["task_id"]
        assert b["data"]["status"] == "queued"
        assert b["data"]["queued_behind"] == f"task {a_id}"
        assert any("different session_id" in w for w in b["warnings"])

        st = await c.status(b_id)
        assert st["data"]["status"] == "queued"
        assert st["data"]["progress"]["state"] == "queued"

        a_final = await c.wait_done(a_id)
        b_final = await c.wait_done(b_id)
        assert a_final["data"]["status"] == "done"
        assert b_final["data"]["status"] == "done"
        assert "B done" in str(b_final["data"]["result"])
        assert b_final["data"]["progress"]["queued_s"] >= 4

    run_with_server(tmp_path, body, MCP_STATA_STALL_SECONDS="1")


def test_cancelling_queued_job_leaves_running_job_alone(tmp_path):
    async def body(c: Client):
        a = await c.call("stata_run", code='sleep 6000\ndisplay "A survived"', background=True)
        a_id = a["data"]["task_id"]
        await _wait_state(c, a_id, "running")
        b = await c.call("stata_run", code='display "B must not run"', background=True)
        b_id = b["data"]["task_id"]
        assert b["data"]["status"] == "queued"

        await c.call("stata_control", action="cancel", id=b_id)
        b_final = await c.wait_done(b_id, limit=20)
        assert b_final["data"]["status"] == "failed"

        a_final = await c.wait_done(a_id)
        assert a_final["data"]["status"] == "done", a_final["data"].get("error")
        assert "A survived" in str(a_final["data"]["result"])

        # B was withdrawn, not merely abandoned: the session is idle again.
        t0 = time.monotonic()
        after = await c.call("stata_run", code='display "idle"')
        assert "idle" in str(after) and time.monotonic() - t0 < 10
        log = await c.call("stata_run", code='display "check"')
        assert "B must not run" not in str(log)

    run_with_server(tmp_path, body)


def test_cancelling_running_job_lets_queued_job_run(tmp_path):
    async def body(c: Client):
        a = await c.call("stata_run", code="sleep 120000", background=True)
        a_id = a["data"]["task_id"]
        await _wait_state(c, a_id, "running")
        b = await c.call("stata_run", code='display "B ran"', background=True)
        b_id = b["data"]["task_id"]

        t0 = time.monotonic()
        await c.call("stata_control", action="cancel", id=a_id)
        b_final = await c.wait_done(b_id, limit=30)
        assert time.monotonic() - t0 < 15
        assert b_final["data"]["status"] == "done"
        assert "B ran" in str(b_final["data"]["result"])

    run_with_server(tmp_path, body)


def test_sync_call_behind_busy_session_is_promoted_as_queued(tmp_path):
    async def body(c: Client):
        a = await c.call("stata_run", code="sleep 10000", background=True)
        await _wait_state(c, a["data"]["task_id"], "running")
        t0 = time.monotonic()
        res = await c.call("stata_run", code='display "late"')
        assert time.monotonic() - t0 < 6
        assert res["data"]["auto_backgrounded"] is True
        assert res["data"]["progress"]["state"] == "queued"
        assert any("different session_id" in w for w in res["warnings"])
        final = await c.wait_done(res["data"]["task_id"])
        assert "late" in str(final["data"]["result"])

    run_with_server(tmp_path, body, MCP_STATA_AUTO_BACKGROUND_AFTER="2")


def test_separate_sessions_run_in_parallel(tmp_path):
    async def body(c: Client):
        await c.call("stata_manage_session", action="create", session_id="p2")
        t0 = time.monotonic()
        a = await c.call("stata_run", code="sleep 6000", background=True)
        b = await c.call("stata_run", code="sleep 6000", background=True, session_id="p2")
        assert b["data"]["status"] == "started"
        await c.wait_done(a["data"]["task_id"])
        await c.wait_done(b["data"]["task_id"])
        assert time.monotonic() - t0 < 10, "two sessions did not run concurrently"

    run_with_server(tmp_path, body)
