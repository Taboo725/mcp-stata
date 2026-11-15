"""End-to-end stress: everything at once, then check the server is still sane.

Three auto jobs run in parallel (a long one, a do-file with progress, a
failing one), a fourth queues behind them and is cancelled, the long job's
worker is killed outright, and the all-tasks view must account for every
task exactly once. Afterwards the default session must be untouched and new
auto work must still schedule cleanly.
"""

from __future__ import annotations

import time

import anyio
import pytest

from stdio_harness import Client, kill_pid, run_with_server

pytestmark = [pytest.mark.requires_stata, pytest.mark.integration, pytest.mark.xdist_group("stata_heavy")]


def test_mixed_workload(tmp_path):
    dofile = tmp_path / "steps.do"
    dofile.write_text("".join(f'display "step {i}"\nsleep 400\n' for i in range(12)))

    async def body(c: Client):
        await c.call("stata_run", code="sysuse auto, clear")

        long = await c.call("stata_run", code="sleep 120000", background=True, session_id="auto")
        steps = await c.call("stata_run", code=str(dofile), is_file=True, background=True, session_id="auto")
        bad = await c.call("stata_run", code="sleep 1500\nnot_a_command", background=True, session_id="auto")
        sessions_used = {long["session_id"], steps["session_id"], bad["session_id"]}
        assert len(sessions_used) == 3, sessions_used

        queued = await c.call("stata_run", code='display "never"', background=True, session_id="auto")
        assert queued["data"]["status"] == "queued"
        await c.call("stata_control", action="cancel", id=queued["data"]["task_id"])

        # Mid-flight: progress is visible on the do-file.
        await anyio.sleep(2.5)
        mid = await c.status(steps["data"]["task_id"])
        assert mid["data"]["progress"].get("lines_done", 0) >= 1

        listing = await c.call("stata_manage_session", action="list")
        pid = next(s["pid"] for s in listing["data"]["sessions"] if s["id"] == long["session_id"])
        kill_pid(pid)

        ids = {r["data"]["task_id"] for r in (long, steps, bad, queued)}
        finished: dict[str, dict] = {}
        deadline = time.monotonic() + 90
        while ids - set(finished) and time.monotonic() < deadline:
            res = await c.call("stata_task_status", task_id="all", wait=True, timeout=20, poll_interval=0.2)
            for row in res["data"]["tasks"]:
                if row["status"] in {"done", "failed"}:
                    assert row["task_id"] not in finished, "reported twice"
                    finished[row["task_id"]] = row
        assert ids <= set(finished), f"unaccounted: {ids - set(finished)}"

        assert finished[long["data"]["task_id"]]["status"] == "failed"
        assert "exited unexpectedly" in finished[long["data"]["task_id"]]["error"]
        assert finished[steps["data"]["task_id"]]["status"] == "done"
        assert finished[bad["data"]["task_id"]]["status"] == "failed"
        assert finished[queued["data"]["task_id"]]["status"] == "failed"

        steps_final = await c.status(steps["data"]["task_id"])
        assert steps_final["data"]["progress"]["lines_done"] == steps_final["data"]["progress"]["lines_total"]
        assert "step 11" in str(steps_final["data"]["result"])

        # The default session never noticed any of it.
        check = await c.call("stata_run", code='display "obs=" _N')
        assert "obs=74" in str(check).replace(" ", "")

        # New auto work still schedules: the killed session is rebuilt or
        # replaced, and no claim leaked from any of the above.
        t0 = time.monotonic()
        follow = [
            await c.call("stata_run", code=f'display "again {i}"', background=True, session_id="auto")
            for i in range(3)
        ]
        for r in follow:
            done = await c.wait_done(r["data"]["task_id"], limit=60)
            assert done["data"]["status"] == "done", done["data"].get("error")
        assert time.monotonic() - t0 < 60

    run_with_server(tmp_path, body, MCP_STATA_MAX_AUTO_SESSIONS="3")
