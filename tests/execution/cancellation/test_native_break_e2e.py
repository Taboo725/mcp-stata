"""End-to-end: break and cancel really stop Stata, and leave the session usable.

Before the StataSO_SetBreak fallback both were no-ops on current Stata: a
"cancelled" task kept running and the next command waited behind it.
"""

from __future__ import annotations

import time

import anyio
import pytest

from stdio_harness import Client, busy_mata, run_with_server

pytestmark = [pytest.mark.requires_stata, pytest.mark.integration, pytest.mark.xdist_group("stata_heavy")]

LONG_JOBS = {
    "sleep": "sleep 120000",
    "forvalues": "quietly forvalues i = 1/2000000000 {\n}",
    "mata": busy_mata(120000),
}


@pytest.mark.parametrize("kind", sorted(LONG_JOBS))
def test_break_interrupts_running_command(tmp_path, kind):
    async def body(c: Client):
        started = await c.call("stata_run", code=LONG_JOBS[kind], background=True)
        task_id = started["data"]["task_id"]
        await anyio.sleep(1.5)
        t0 = time.monotonic()
        await c.call("stata_control", action="break", id="default")
        final = await c.wait_done(task_id, limit=20)
        assert time.monotonic() - t0 < 10, "break did not interrupt Stata"
        assert final["data"]["status"] == "failed"
        # The SMCL echo of the Mata job contains "{ }": the failure must be the
        # break itself, not an output-rendering crash.
        assert "list index out of range" not in str(final["data"]["error"])

        t1 = time.monotonic()
        after = await c.call("stata_run", code='display "alive"')
        assert "alive" in str(after)
        assert time.monotonic() - t1 < 10

    run_with_server(tmp_path, body)


def test_cancel_frees_session_for_next_command(tmp_path):
    async def body(c: Client):
        started = await c.call("stata_run", code="sleep 120000", background=True)
        await anyio.sleep(1)
        await c.call("stata_control", action="cancel", id=started["data"]["task_id"])
        await c.wait_done(started["data"]["task_id"], limit=20)
        t0 = time.monotonic()
        after = await c.call("stata_run", code='display "free"')
        assert "free" in str(after)
        assert time.monotonic() - t0 < 10, "cancelled command was still occupying the session"

    run_with_server(tmp_path, body)


def test_idle_break_does_not_hit_next_command(tmp_path):
    async def body(c: Client):
        await c.call("stata_run", code="display 1")
        await c.call("stata_control", action="break", id="default")
        await anyio.sleep(0.5)
        for i in range(3):
            res = await c.call("stata_run", code=f'display "ok{i}"')
            assert res["success"] is True
            assert f"ok{i}" in str(res)

    run_with_server(tmp_path, body)
