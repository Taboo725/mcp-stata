"""A session that fails to start must not poison the pool.

Before this fix ``get_or_create_session`` flipped a timed-out session to
"error" but left it in ``_sessions``; every later call then reported
"failed to initialize. Stata binary may be missing or inaccessible", which is
both permanent and misleading. The session is now discarded so the next call
builds a fresh worker.
"""
from unittest.mock import MagicMock, patch

import pytest

from mcp_stata.sessions import SessionManager


def _never_ready_pipe():
    conn = MagicMock()
    conn.poll.return_value = False
    return conn


def _ready_pipe(pid):
    conn = MagicMock()
    conn.poll.side_effect = [True] + [False] * 100
    conn.recv.return_value = {"event": "ready", "pid": pid}
    return conn


@pytest.mark.asyncio
async def test_init_timeout_discards_the_session():
    with patch('mcp_stata.sessions.Process'), patch(
        'mcp_stata.sessions.Pipe', return_value=(_never_ready_pipe(), MagicMock())
    ):
        manager = SessionManager()
        with patch.object(SessionManager, "_init_timeout", return_value=0.2):
            with pytest.raises(TimeoutError) as excinfo:
                await manager.get_or_create_session("slow")

        assert "slow" not in manager._sessions
        assert "did not become ready" in str(excinfo.value)
        assert "MCP_STATA_SESSION_INIT_TIMEOUT" in str(excinfo.value)


@pytest.mark.asyncio
async def test_retry_after_timeout_builds_a_fresh_worker():
    with patch('mcp_stata.sessions.Process'), patch('mcp_stata.sessions.Pipe') as mock_pipe:
        mock_pipe.side_effect = [
            (_never_ready_pipe(), MagicMock()),
            (_ready_pipe(4321), MagicMock()),
        ]
        manager = SessionManager()

        with patch.object(SessionManager, "_init_timeout", return_value=0.2):
            with pytest.raises(TimeoutError):
                await manager.get_or_create_session("retry")

        session = await manager.get_or_create_session("retry")
        assert session.status == "running"
        assert session.pid == 4321
        await manager.stop_all()


@pytest.mark.asyncio
async def test_dead_worker_is_replaced():
    first_proc, second_proc = MagicMock(), MagicMock()
    with patch('mcp_stata.sessions.Process', side_effect=[first_proc, second_proc]), patch(
        'mcp_stata.sessions.Pipe'
    ) as mock_pipe:
        mock_pipe.side_effect = [(_ready_pipe(1), MagicMock()), (_ready_pipe(2), MagicMock())]
        manager = SessionManager()
        first = await manager.get_or_create_session("crashy")
        assert first.pid == 1

        first_proc.is_alive.return_value = False
        second = await manager.get_or_create_session("crashy")
        assert second is not first
        assert second.pid == 2
        await manager.stop_all()


def test_init_timeout_reads_the_env(monkeypatch):
    manager = SessionManager.__new__(SessionManager)
    assert manager._init_timeout() == SessionManager._DEFAULT_INIT_TIMEOUT

    monkeypatch.setenv("MCP_STATA_SESSION_INIT_TIMEOUT", "120")
    assert manager._init_timeout() == 120.0

    for bad in ("", "   ", "abc", "0", "-5"):
        monkeypatch.setenv("MCP_STATA_SESSION_INIT_TIMEOUT", bad)
        assert manager._init_timeout() == SessionManager._DEFAULT_INIT_TIMEOUT
