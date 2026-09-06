"""`clear all` must not flush the user's output away.

A program-clearing command makes the client re-run its startup .do files.
Executed with `do`, and before the command's log was read, that reload echoed
statest.mata and profile.do into the user's own output - several kilobytes,
which tail-only truncation then turned into "the output is gone".
"""
import os
import tempfile

import pytest

from mcp_stata.discovery import find_stata_path
from mcp_stata.sessions import SessionManager

pytestmark = pytest.mark.xdist_group("stata_heavy")

CLEARING_COMMANDS = ["clear all", "clear programs", "program drop _all"]


@pytest.fixture
def startup_do_file():
    with tempfile.NamedTemporaryFile(suffix=".do", mode="w", delete=False) as tf:
        tf.write('program define mcp_reload_probe\n')
        tf.write('    display "PROBE ALIVE"\n')
        tf.write('end\n')
        path = tf.name

    stata_path, _ = find_stata_path()
    previous = {k: os.environ.get(k) for k in ("STATA_BIN", "MCP_STATA_STARTUP_DO_FILE")}
    os.environ["STATA_BIN"] = stata_path
    os.environ["MCP_STATA_STARTUP_DO_FILE"] = path
    try:
        yield path
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.requires_stata
@pytest.mark.asyncio
@pytest.mark.parametrize("clearing_command", CLEARING_COMMANDS)
async def test_clearing_command_keeps_user_output(startup_do_file, clearing_command):
    manager = SessionManager()
    try:
        session = await manager.get_or_create_session("reload_quiet")
        code = f'display "MARKER_A"\n{clearing_command}\ndisplay "MARKER_B"'
        result = await session.call("run_command", {"code": code, "options": {"echo": True}})

        stdout = result.get("stdout") or ""
        assert "MARKER_A" in stdout, stdout
        assert "MARKER_B" in stdout, stdout
        # No trace of the reloaded startup files.
        assert "statest" not in stdout, stdout
        assert "mcp_reload_probe" not in stdout, stdout
        assert len(stdout) < 1000, f"{len(stdout)} chars of output for three commands"
    finally:
        await manager.stop_all()


@pytest.mark.requires_stata
@pytest.mark.asyncio
async def test_clearing_command_still_restores_startup_programs(startup_do_file):
    manager = SessionManager()
    try:
        session = await manager.get_or_create_session("reload_restores")
        await session.call("run_command", {"code": "clear all", "options": {"echo": False}})
        result = await session.call(
            "run_command", {"code": "mcp_reload_probe", "options": {"echo": False}}
        )
        assert "PROBE ALIVE" in (result.get("stdout") or ""), result
    finally:
        await manager.stop_all()
