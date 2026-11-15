"""Drive the real ``mcp-stata`` server over stdio from a test.

Shared by the end-to-end tests that need a host's view of the server: start
the CLI, open an MCP client session, call tools and decode their envelopes.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import time
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from tool_payload import tool_payload_dict


def cli_path() -> str:
    cli = shutil.which("mcp-stata")
    if cli:
        return cli
    for base in (sysconfig.get_path("scripts"), str(Path(sys.executable).parent)):
        if not base:
            continue
        for name in ("mcp-stata.exe", "mcp-stata"):
            candidate = Path(base) / name
            if candidate.exists():
                return str(candidate)
    pytest.skip("mcp-stata CLI not found")


class Client:
    def __init__(self, session: ClientSession):
        self.session = session

    async def call(self, tool: str, **args):
        result = await self.session.call_tool(tool, args)
        return tool_payload_dict(result)

    async def status(self, task_id: str, **kw):
        return await self.call("stata_task_status", task_id=task_id, **kw)

    async def wait_done(self, task_id: str, limit: float = 90.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            res = await self.status(task_id, wait=True, timeout=10, poll_interval=0.2)
            if res["data"]["status"] in {"done", "failed"}:
                return res
        raise AssertionError(f"task {task_id} did not finish within {limit}s")


def run_with_server(tmp_path, body, **env) -> None:
    """Start the server, run ``await body(Client)``, shut everything down."""

    async def main():
        params = StdioServerParameters(
            command=cli_path(),
            args=[],
            cwd=os.getcwd(),
            env={**os.environ, "MCP_STATA_ALLOWED_ROOTS": str(tmp_path), **env},
        )
        async with AsyncExitStack() as stack:
            r, w = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(r, w))
            await session.initialize()
            await body(Client(session))

    anyio.run(main)


def kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        os.kill(pid, signal.SIGKILL)


# Burns CPU for ~N ms without printing anything; braces in the echo also
# exercise the SMCL renderer's handling of literal "{ }".
def busy_mata(ms: int) -> str:
    return (
        "mata:\n"
        's = clock(c("current_date") + " " + c("current_time"), "DMYhms")\n'
        f'while ((clock(c("current_date") + " " + c("current_time"), "DMYhms") - s) < {ms}) {{ }}\n'
        "end\n"
    )
