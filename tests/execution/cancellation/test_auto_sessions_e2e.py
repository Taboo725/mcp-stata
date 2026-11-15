"""End-to-end: session_id="auto" fans independent jobs out across Stata workers."""

from __future__ import annotations

import time

import pytest

from stdio_harness import Client, run_with_server

pytestmark = [pytest.mark.requires_stata, pytest.mark.integration, pytest.mark.xdist_group("stata_heavy")]


async def _collect_all(c: Client, expected: set[str], limit: float = 120.0) -> dict[str, dict]:
    """Drive the all-tasks view until every expected task is reported finished."""
    finished: dict[str, dict] = {}
    deadline = time.monotonic() + limit
    while expected - set(finished) and time.monotonic() < deadline:
        res = await c.call("stata_task_status", task_id="all", wait=True, timeout=30, poll_interval=0.2)
        for row in res["data"]["tasks"]:
            if row["status"] in {"done", "failed"}:
                assert row["task_id"] not in finished, "a finished task was reported twice"
                finished[row["task_id"]] = row
    assert expected <= set(finished), f"never finished: {expected - set(finished)}"
    return finished


def test_auto_jobs_run_in_parallel_and_queue_at_the_cap(tmp_path):
    async def body(c: Client):
        # Sequential sync calls all reuse auto-1 (it is idle again each time);
        # bring all three workers up first so the timing below measures
        # execution, not Stata start-up.
        warm = await c.call("stata_run", code="display 0", session_id="auto")
        assert warm["session_id"] == "auto-1"
        again = await c.call("stata_run", code="display 0", session_id="auto")
        assert again["session_id"] == "auto-1", "an idle auto session must be reused"
        for name in ("auto-2", "auto-3"):
            await c.call("stata_manage_session", action="create", session_id=name)

        t0 = time.monotonic()
        jobs = []
        for i in range(3):
            res = await c.call("stata_run", code=f'sleep 6000\ndisplay "job {i}"', background=True, session_id="auto")
            assert res["data"]["status"] == "started"
            jobs.append(res)
        used = {r["session_id"] for r in jobs}
        assert used == {"auto-1", "auto-2", "auto-3"}

        extra = await c.call("stata_run", code='display "job extra"', background=True, session_id="auto")
        assert extra["session_id"] in used
        assert extra["data"]["status"] == "queued"

        ids = {r["data"]["task_id"] for r in jobs} | {extra["data"]["task_id"]}
        finished = await _collect_all(c, ids)
        elapsed = time.monotonic() - t0
        assert all(row["status"] == "done" for row in finished.values())
        assert elapsed < 14, f"three 6s jobs took {elapsed:.1f}s: not parallel"

        for r in jobs + [extra]:
            out = await c.status(r["data"]["task_id"])
            assert "job" in str(out["data"]["result"])

    run_with_server(tmp_path, body, MCP_STATA_MAX_AUTO_SESSIONS="3")


def test_auto_sessions_do_not_touch_default_state(tmp_path):
    async def body(c: Client):
        await c.call("stata_run", code="sysuse auto, clear")
        res = await c.call(
            "stata_run",
            code='sysuse nlsw88, clear\nquietly regress wage hours\ndisplay "N=" e(N)',
            session_id="auto",
        )
        assert res["session_id"].startswith("auto-")
        assert "N=2242" in str(res).replace(" ", "")

        check = await c.call("stata_run", code='display "obs=" _N')
        assert "obs=74" in str(check).replace(" ", ""), "auto job changed the default session's data"

    run_with_server(tmp_path, body)


def test_all_view_reports_failures_once_with_error(tmp_path):
    async def body(c: Client):
        ok = await c.call("stata_run", code='display "fine"', background=True, session_id="auto")
        bad = await c.call("stata_run", code="this_is_not_a_command", background=True, session_id="auto")
        finished = await _collect_all(c, {ok["data"]["task_id"], bad["data"]["task_id"]})
        assert finished[ok["data"]["task_id"]]["status"] == "done"
        assert finished[bad["data"]["task_id"]]["status"] == "failed"
        assert finished[bad["data"]["task_id"]].get("error")

        again = await c.call("stata_task_status", task_id="all")
        assert again["data"]["tasks"] == []

    run_with_server(tmp_path, body)
