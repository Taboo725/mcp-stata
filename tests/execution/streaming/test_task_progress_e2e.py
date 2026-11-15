"""End-to-end: live progress, stall diagnosis and failure paths over real stdio.

Each test starts the real ``mcp-stata`` server against a real Stata and drives
it through an MCP client, so the progress record, the CPU probe on the actual
worker PID, auto-promotion and the dead-worker paths are exercised as a host
would see them.
"""

from __future__ import annotations

import time

import anyio
import pytest

from stdio_harness import Client, busy_mata, kill_pid, run_with_server

pytestmark = [pytest.mark.requires_stata, pytest.mark.integration, pytest.mark.xdist_group("stata_heavy")]


def test_progress_moves_and_stalls_are_diagnosed(tmp_path):
    """Lines advance, a silent busy block reads busy_silent, idle sleep reads possibly_stuck."""
    lines = []
    for i in range(1, 9):
        lines.append(f'display "step {i}"')
        lines.append("sleep 700")
    dofile = tmp_path / "progress.do"
    dofile.write_text("\n".join(lines) + "\n" + busy_mata(9000) + "sleep 9000\n" + 'display "the end"\n')

    async def body(c: Client):
        started = await c.call("stata_run", code=str(dofile), is_file=True, background=True)
        task_id = started["data"]["task_id"]
        snaps = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            res = await c.status(task_id)
            snaps.append(res["data"]["progress"])
            if res["data"]["status"] in {"done", "failed"}:
                final = res
                break
            await anyio.sleep(0.8)
        else:
            raise AssertionError("do-file never finished")

        running = [s for s in snaps if s["state"] not in {"done", "failed"}]
        done_counts = sorted({s.get("lines_done") for s in running if s.get("lines_done") is not None})
        assert len(done_counts) >= 3, f"progress never advanced: {done_counts}"
        assert all(s.get("lines_total", 0) >= 16 for s in running if "lines_total" in s)
        assert any("step" in (s.get("current_command") or "") or "sleep" in (s.get("current_command") or "")
                   for s in running), running[:3]
        elapsed = [s["elapsed_s"] for s in snaps]
        assert elapsed == sorted(elapsed)

        busy = [s for s in running if s["state"] == "busy_silent"]
        stuck = [s for s in running if s["state"] == "possibly_stuck"]
        assert busy, f"busy Mata loop never classified busy_silent: {[s['state'] for s in running]}"
        assert max(s["worker_cpu_percent"] for s in busy) >= 50
        assert stuck, f"idle sleep never classified possibly_stuck: {[s['state'] for s in running]}"
        assert all(s["worker_cpu_percent"] < 5 for s in stuck)
        assert all(s["worker_alive"] for s in running)

        assert final["data"]["status"] == "done"
        assert final["data"]["progress"]["state"] == "done"
        assert "the end" in str(final["data"]["result"])

    run_with_server(tmp_path, body, MCP_STATA_STALL_SECONDS="3")


def test_slow_sync_call_is_promoted_and_fast_one_is_not(tmp_path):
    async def body(c: Client):
        fast = await c.call("stata_run", code="display 6*7")
        assert not fast["data"].get("auto_backgrounded")
        assert "42" in str(fast)

        t0 = time.monotonic()
        slow = await c.call("stata_run", code='sleep 8000')
        assert time.monotonic() - t0 < 7, "sync call was not promoted"
        assert slow["data"]["auto_backgrounded"] is True
        assert slow["data"]["progress"]["elapsed_s"] >= 2.5
        final = await c.wait_done(slow["data"]["task_id"])
        assert final["data"]["status"] == "done"

        after = await c.call("stata_run", code='display "still usable"')
        assert "still usable" in str(after)

    run_with_server(tmp_path, body, MCP_STATA_AUTO_BACKGROUND_AFTER="3")


def test_cancelling_promoted_task_frees_the_session(tmp_path):
    async def body(c: Client):
        slow = await c.call("stata_run", code="sleep 60000")
        task_id = slow["data"]["task_id"]
        await c.call("stata_control", action="cancel", id=task_id)
        final = await c.wait_done(task_id, limit=30)
        assert final["data"]["status"] == "failed"
        t0 = time.monotonic()
        after = await c.call("stata_run", code='display "after cancel"')
        assert "after cancel" in str(after)
        assert time.monotonic() - t0 < 20

    run_with_server(tmp_path, body, MCP_STATA_AUTO_BACKGROUND_AFTER="2")


def test_killed_worker_fails_the_task_instead_of_hanging(tmp_path):
    async def body(c: Client):
        started = await c.call("stata_run", code="sleep 60000", background=True, session_id="victim")
        task_id = started["data"]["task_id"]
        sessions = await c.call("stata_manage_session", action="list")
        pid = next(s["pid"] for s in sessions["data"]["sessions"] if s["id"] == "victim")
        kill_pid(pid)

        t0 = time.monotonic()
        final = await c.wait_done(task_id, limit=30)
        assert time.monotonic() - t0 < 15
        assert final["data"]["status"] == "failed"
        assert "exited unexpectedly" in final["data"]["error"]

        again = await c.call("stata_run", code='display "rebuilt"', session_id="victim")
        assert "rebuilt" in str(again)

    run_with_server(tmp_path, body)


def test_stopping_session_fails_its_running_task(tmp_path):
    async def body(c: Client):
        started = await c.call("stata_run", code="sleep 60000", background=True, session_id="doomed")
        task_id = started["data"]["task_id"]
        await anyio.sleep(1)
        await c.call("stata_manage_session", action="stop", session_id="doomed")
        final = await c.wait_done(task_id, limit=30)
        assert final["data"]["status"] == "failed"
        assert "stopped" in final["data"]["error"]

        default = await c.call("stata_run", code='display "default fine"')
        assert "default fine" in str(default)

    run_with_server(tmp_path, body)


def test_do_file_error_midway_reports_failure_with_progress(tmp_path):
    dofile = tmp_path / "boom.do"
    dofile.write_text('display "before"\nsleep 500\nthis_is_not_a_command\ndisplay "after"\n')

    async def body(c: Client):
        started = await c.call("stata_run", code=str(dofile), is_file=True, background=True)
        final = await c.wait_done(started["data"]["task_id"])
        assert final["data"]["status"] == "failed"
        assert final["data"]["progress"]["state"] == "failed"
        assert final["data"]["progress"]["elapsed_s"] > 0

    run_with_server(tmp_path, body)
