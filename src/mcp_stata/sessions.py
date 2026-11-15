from __future__ import annotations
import os
import uuid
import logging
import asyncio
import atexit
import multiprocessing
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable, Awaitable
from multiprocessing.connection import Connection
from datetime import datetime, timezone

from mcp_stata.models import SessionInfo, CommandResponse

logger = logging.getLogger("mcp_stata.sessions")

# Use 'spawn' for process creation to ensure thread-safety and Stata/Rust compatibility.
# Re-exposed at module level so tests can patch these references.
_ctx = multiprocessing.get_context("spawn")
Process = _ctx.Process
Pipe = _ctx.Pipe


@dataclass
class _SessionSnapshot:
    command_count: int
    variables: set[str]
    macros: dict[str, Any]
    n_obs: int
    n_vars: int
    captured_at: str

class StataSession:
    _STATEFUL_METHODS = {
        "run_command",
        "run_do_file",
        "run_command_structured",
        "load_data",
    }

    def __init__(self, session_id: str, startup_do_file: Optional[str] = None):
        self.id = session_id
        self.status = "starting"
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.pid: Optional[int] = None
        self.profile_code: Optional[str] = None
        self.command_count = 0
        self._max_history_entries = int(os.getenv("MCP_STATA_MAX_SESSION_HISTORY", "200"))
        self._snapshot_timeout_seconds = float(os.getenv("MCP_STATA_HISTORY_SNAPSHOT_TIMEOUT", "1.5"))
        self._history: List[_SessionSnapshot] = []
        self._last_diff_snapshot: Optional[_SessionSnapshot] = None
        self._history_lock = asyncio.Lock()
        self._stopping = False
        
        self._parent_conn, self._child_conn = Pipe()
        self._process = Process(target=self._run_worker, args=(self._child_conn, startup_do_file))
        self._process.daemon = True
        self._process.start()
        
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._log_listeners: Dict[str, List[Callable[[str], Awaitable[None]]]] = {}
        self._progress_listeners: Dict[str, List[Callable[[float, Optional[float], Optional[str]], Awaitable[None]]]] = {}
        
        self._listener_running = True
        self._listener_task = asyncio.create_task(self._listen_to_worker())

    def _run_worker(self, conn: Connection, startup_do_file: Optional[str] = None):
        if startup_do_file:
            os.environ["MCP_STATA_STARTUP_DO_FILE"] = startup_do_file
        from mcp_stata.worker import main
        main(conn, startup_do_file)

    async def _listen_to_worker(self):
        loop = asyncio.get_running_loop()
        connection_closed = False
        try:
            while self._listener_running:
                # Use poll with timeout to allow checking self._listener_running and asyncio cancellation
                try:
                    ready = await loop.run_in_executor(None, self._parent_conn.poll, 0.2)
                    msg = await loop.run_in_executor(None, self._parent_conn.recv) if ready else None
                except (EOFError, OSError):
                    logger.info(f"Session {self.id} worker connection closed.")
                    connection_closed = True
                    break
                if ready:
                    await self._handle_worker_msg(msg)
                else:
                    # The parent keeps its own copy of the child's pipe end, so
                    # a worker that dies (crash, OOM kill, taskkill) does not
                    # always surface as EOF. Check liveness directly.
                    if self._pending_requests and not self._worker_alive():
                        logger.warning(f"Session {self.id} worker process is gone.")
                        connection_closed = True
                        break
                    # Give the event loop a chance to process other tasks and check cancellation
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self._listener_running = False
            raise
        except Exception as e:
            logger.error(f"Error in session {self.id} listener: {e}")
            self.status = "error"
        finally:
            self._listener_running = False
            if self.status != "error":
                self.status = "stopped"
            # A closed pipe or a dead worker means no result can ever arrive for
            # what is still pending. Fail it now: otherwise the caller -- and any
            # background task built on it -- reports "running" forever.
            if connection_closed or self._stopping or not self._worker_alive():
                self._fail_pending(self._unavailable_reason())

    async def _handle_worker_msg(self, msg: Dict[str, Any]):
        event = msg.get("event")
        msg_id = msg.get("id")
        
        if event == "ready":
            self.pid = msg.get("pid")
            self.status = "running"
            logger.info(f"Session {self.id} ready (PID: {self.pid})")
        
        elif event == "log":
            for cb in list(self._log_listeners.get(msg_id, ())):
                await self._invoke_listener(cb, msg.get("text"))
        
        elif event == "progress":
            for cb in list(self._progress_listeners.get(msg_id, ())):
                await self._invoke_listener(cb, msg.get("progress"), msg.get("total"), msg.get("message"))
        
        elif event == "result":
            if msg_id in self._pending_requests:
                if not self._pending_requests[msg_id].done():
                    self._pending_requests[msg_id].set_result(msg.get("result"))
                self._cleanup_listeners(msg_id)
        
        elif event == "error":
            if msg_id in self._pending_requests:
                if not self._pending_requests[msg_id].done():
                    self._pending_requests[msg_id].set_exception(RuntimeError(msg.get("message")))
                self._cleanup_listeners(msg_id)
            else:
                error_msg = msg.get("message", "Unknown worker error")
                logger.error(f"Global worker error in session {self.id}: {error_msg}")
                # Don't update status if already stopped or error
                if self.status not in ("stopped", "error"):
                    self.status = "error"
                
                # Fail all pending requests as the worker is dead or unusable
                for pid, fut in list(self._pending_requests.items()):
                    if not fut.done():
                        fut.set_exception(RuntimeError(f"Worker process failed: {error_msg}"))
                    self._cleanup_listeners(pid)

    async def _invoke_listener(self, cb, *args) -> None:
        # A failing log/progress consumer must not take the session listener
        # down with it; that would strand every pending request.
        try:
            await cb(*args)
        except Exception as e:
            logger.warning(f"Session {self.id} listener callback failed: {e}")

    def _worker_alive(self) -> bool:
        try:
            return bool(self._process and self._process.is_alive())
        except Exception:
            return False

    def _unavailable_reason(self) -> str:
        if self._stopping:
            return f"Stata session {self.id} was stopped before the command finished."
        exitcode = getattr(self._process, "exitcode", None)
        detail = f" (exit code {exitcode})" if exitcode is not None else ""
        return (
            f"Stata worker for session {self.id} exited unexpectedly{detail} before the command "
            f"finished. The session will be rebuilt on the next call."
        )

    def _fail_pending(self, message: str) -> None:
        for msg_id, fut in list(self._pending_requests.items()):
            if not fut.done():
                fut.set_exception(RuntimeError(message))
            self._cleanup_listeners(msg_id)

    def _cleanup_listeners(self, msg_id: str):
        self._log_listeners.pop(msg_id, None)
        self._progress_listeners.pop(msg_id, None)
        self._pending_requests.pop(msg_id, None)

    async def _ensure_listener(self):
        current_loop = asyncio.get_running_loop()
        if self._listener_task is None or self._listener_task.done() or (hasattr(self._listener_task, "get_loop") and self._listener_task.get_loop() != current_loop):
            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
            self._listener_running = True
            self._listener_task = current_loop.create_task(self._listen_to_worker())

    async def _call_raw(
        self,
        method: str,
        args: Dict[str, Any],
        notify_log: Optional[Callable[[str], Awaitable[None]]] = None,
        notify_progress: Optional[Callable[[float, Optional[float], Optional[str]], Awaitable[None]]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        await self._ensure_listener()
        if self.status == "error":
            raise RuntimeError(f"Session {self.id} is in an error state and cannot accept commands. This usually happens if Stata failed to initialize.")

        msg_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[msg_id] = future

        if notify_log:
            self._log_listeners.setdefault(msg_id, []).append(notify_log)
        if notify_progress:
            self._progress_listeners.setdefault(msg_id, []).append(notify_progress)

        try:
            self._parent_conn.send({
                "type": method,
                "id": msg_id,
                "args": args
            })
            if timeout_seconds is None:
                return await future
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds)
            except asyncio.TimeoutError as e:
                self._cleanup_listeners(msg_id)
                raise TimeoutError(
                    f"Timed out waiting for worker response to {method} after {timeout_seconds}s"
                ) from e
        except asyncio.CancelledError:
            logger.info(f"Cancellation requested for command {method}:{msg_id} in session {self.id}")
            try:
                self._parent_conn.send({"type": "break"})
            except Exception as e:
                logger.warning(f"Failed to send break command to worker for session {self.id}: {e}")

            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=3.0)
                logger.info(f"Session {self.id} acknowledged break for {msg_id}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Session {self.id} did not acknowledge break within timeout: {e}")
            raise
        except (AttributeError, BrokenPipeError, ConnectionResetError) as e:
            self._cleanup_listeners(msg_id)
            raise RuntimeError(f"Failed to send command to worker: {e}")

    async def _collect_snapshot(self, command_count: int) -> _SessionSnapshot:
        state_payload = await self._call_raw(
            "get_session_state",
            {},
            timeout_seconds=self._snapshot_timeout_seconds,
        )
        variables_payload = state_payload.get("variables", {}) if isinstance(state_payload, dict) else {}
        variable_entries = variables_payload.get("variables", []) if isinstance(variables_payload, dict) else []
        variables = {str(v.get("name")) for v in variable_entries if isinstance(v, dict) and v.get("name")}

        stored = state_payload.get("stored_results", {}) if isinstance(state_payload, dict) else {}
        macros: dict[str, Any] = {}
        if isinstance(stored, dict):
            for cls in ("r", "e", "s"):
                class_values = stored.get(cls)
                if isinstance(class_values, dict):
                    for key, value in class_values.items():
                        macros[f"{cls}.{key}"] = value

        dataset_state = state_payload.get("dataset_state", {}) if isinstance(state_payload, dict) else {}
        n_obs = int(dataset_state.get("n", 0)) if isinstance(dataset_state, dict) else 0
        n_vars = int(dataset_state.get("k", len(variables))) if isinstance(dataset_state, dict) else len(variables)

        return _SessionSnapshot(
            command_count=command_count,
            variables=variables,
            macros=macros,
            n_obs=n_obs,
            n_vars=n_vars,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    def _prune_history(self) -> None:
        if len(self._history) <= self._max_history_entries:
            return
        # Keep the initial baseline (command 0) and the newest entries.
        baseline = self._history[0]
        keep_tail = max(0, self._max_history_entries - 1)
        self._history = [baseline] + self._history[-keep_tail:]

    async def _record_post_command_snapshot(self) -> None:
        async with self._history_lock:
            snapshot = await self._collect_snapshot(self.command_count)
            self._history.append(snapshot)
            self._prune_history()
            if self._last_diff_snapshot is None:
                self._last_diff_snapshot = snapshot

    async def get_session_diff(self, since_command: Optional[int] = None) -> Dict[str, Any]:
        async with self._history_lock:
            current = await self._collect_snapshot(self.command_count)

            if since_command is None:
                baseline = self._last_diff_snapshot
                if baseline is None:
                    if self._history:
                        baseline = self._history[-1]
                    else:
                        baseline = _SessionSnapshot(
                            command_count=0,
                            variables=set(),
                            macros={},
                            n_obs=0,
                            n_vars=0,
                            captured_at=self.created_at,
                        )
            else:
                baseline = None
                for snapshot in reversed(self._history):
                    if snapshot.command_count <= since_command:
                        baseline = snapshot
                        break
                if baseline is None:
                    earliest = self._history[0].command_count if self._history else self.command_count
                    raise ValueError(
                        f"No session history available for command {since_command}. "
                        f"Earliest retained command is {earliest}."
                    )

            new_vars = current.variables - baseline.variables
            removed_vars = baseline.variables - current.variables
            modified_macros = {
                k: v for k, v in current.macros.items()
                if baseline.macros.get(k) != v
            }
            removed_macros = sorted(set(baseline.macros) - set(current.macros))

            self._last_diff_snapshot = current
            self._history.append(current)
            self._prune_history()

            return {
                "command_count": self.command_count,
                "since_command": baseline.command_count,
                "new_variables": sorted(new_vars),
                "removed_variables": sorted(removed_vars),
                "modified_macros": modified_macros,
                "removed_macros": removed_macros,
                "n_obs": current.n_obs,
                "n_vars": current.n_vars,
                "captured_at": current.captured_at,
            }

    def get_history_stats(self) -> Dict[str, Any]:
        if not self._history:
            return {
                "command_count": self.command_count,
                "history_size": 0,
                "max_history_entries": self._max_history_entries,
                "earliest_command": None,
                "latest_command": None,
            }
        return {
            "command_count": self.command_count,
            "history_size": len(self._history),
            "max_history_entries": self._max_history_entries,
            "earliest_command": self._history[0].command_count,
            "latest_command": self._history[-1].command_count,
        }

    async def call(self, method: str, args: Dict[str, Any], 
                   notify_log: Optional[Callable[[str], Awaitable[None]]] = None,
                   notify_progress: Optional[Callable[[float, Optional[float], Optional[str]], Awaitable[None]]] = None) -> Any:
        result = await self._call_raw(
            method,
            args,
            notify_log=notify_log,
            notify_progress=notify_progress,
        )

        if method in self._STATEFUL_METHODS:
            self.command_count += 1
            try:
                await self._record_post_command_snapshot()
            except Exception:
                logger.warning(
                    "Failed to record session snapshot for %s command #%s in session %s",
                    method,
                    self.command_count,
                    self.id,
                    exc_info=True,
                )
        return result

    async def set_profile(self, code: Optional[str]):
        """Set code that runs before every command in this session."""
        self.profile_code = code
        # We also notify the worker so it can store it
        await self.call("set_profile", {"code": code})

    async def send_break(self):
        """Send an out-of-band break signal to the worker."""
        try:
            self._parent_conn.send({"type": "break"})
            logger.info(f"Break signal sent to session {self.id}")
        except Exception as e:
            logger.warning(f"Failed to send break command to session {self.id}: {e}")
            raise RuntimeError(f"Failed to send break signal: {e}")

    async def stop(self, timeout: float = 5.0):
        self._stopping = True
        self._listener_running = False
        if self.status != "stopped":
            try:
                self._parent_conn.send({"type": "stop"})
            except Exception:
                pass
            
            if self._process and self._process.is_alive():
                self._process.terminate()
                
                # Use executor to join with timeout without blocking the event loop
                loop = asyncio.get_running_loop()
                try:
                    await loop.run_in_executor(None, self._process.join, timeout)
                except Exception:
                    pass

                if self._process.is_alive():
                    logger.warning(f"Session {self.id} worker (PID {self._process.pid}) did not exit after {timeout}s; killing.")
                    try:
                        self._process.kill()
                        await loop.run_in_executor(None, self._process.join)
                    except Exception as e:
                        logger.error(f"Failed to kill session {self.id} worker: {e}")

            self._history.clear()
            self._last_diff_snapshot = None
            self.status = "stopped"
            self._fail_pending(self._unavailable_reason())
            if self._listener_task:
                self._listener_task.cancel()
                try:
                    await self._listener_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

    def get_info(self) -> SessionInfo:
        return SessionInfo(
            id=self.id,
            status=self.status,
            created_at=self.created_at,
            pid=self.pid
        )

# Use class-level list to track all managers for cleanup
_all_managers: List[SessionManager] = []
_atexit_registered = False

def _global_shutdown():
    """Final emergency cleanup for all SessionManagers."""
    for manager in _all_managers:
        manager._shutdown()

class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, StataSession] = {}
        self._default_session_id = "default"
        _all_managers.append(self)
        global _atexit_registered
        if not _atexit_registered:
            atexit.register(_global_shutdown)
            _atexit_registered = True

    def _shutdown(self) -> None:
        """Emergency cleanup for atexit."""
        for session in list(self._sessions.values()):
            try:
                if session._process and session._process.is_alive():
                    # Be very aggressive in atexit, we don't have much time
                    session._process.kill()
                    session._process.join(timeout=0.1)
            except Exception:
                pass
            try:
                session._parent_conn.close()
            except Exception:
                pass
        self._sessions.clear()

    async def start(self):
        # Start default session
        await self.get_or_create_session(self._default_session_id)

    async def get_or_create_session(self, session_id: str, startup_do_file: Optional[str] = None) -> StataSession:
        # A session that failed to come up must never be handed back or waited
        # on again: discard it so this call builds a fresh worker instead of
        # reporting the stale failure forever.
        pooled = self._sessions.get(session_id)
        if pooled is not None and (pooled.status == "error" or not pooled._process.is_alive()):
            logger.info(f"Discarding unusable Stata session {session_id} (status={pooled.status})")
            await self.stop_session(session_id)

        if session_id not in self._sessions:
            logger.info(f"Creating new Stata session: {session_id}")
            session = StataSession(session_id, startup_do_file=startup_do_file)
            self._sessions[session_id] = session
            
        session = self._sessions[session_id]
        
        # Wait for the session to be ready or fail, even if it was already created by another call.
        # Give it more time to start up on CI (especially Stata's first init).
        timeout = self._init_timeout()
        start_time = asyncio.get_running_loop().time()
        while session.status == "starting" and asyncio.get_running_loop().time() - start_time < timeout:
            if not session._process.is_alive():
                # Process died before reaching ready
                session.status = "error"
                break
            await asyncio.sleep(0.1)
            
        if session.status == "error":
            await self.stop_session(session_id)
            raise RuntimeError(
                f"Stata session {session_id} failed to initialize: the worker process exited "
                f"before it was ready. Stata may be missing or inaccessible. "
                f"The session was discarded; calling again starts a fresh one."
            )
            
        if session.status == "starting":
             # Still starting after timeout. Drop it rather than leaving a
             # half-built session in the pool for the next call to trip over.
             await self.stop_session(session_id)
             raise TimeoutError(
                 f"Stata session {session_id} did not become ready within {timeout:g}s. "
                 f"The session was discarded; calling again starts a fresh one. "
                 f"Raise {self._INIT_TIMEOUT_ENV} if Stata legitimately needs longer to start."
             )
             
        return session

    _INIT_TIMEOUT_ENV = "MCP_STATA_SESSION_INIT_TIMEOUT"
    _DEFAULT_INIT_TIMEOUT = 45.0  # Stata's first in-process init, with CI headroom.

    def _init_timeout(self) -> float:
        """Seconds to wait for a worker to report ready, overridable by env."""
        raw = (os.getenv(self._INIT_TIMEOUT_ENV) or "").strip()
        if not raw:
            return self._DEFAULT_INIT_TIMEOUT
        try:
            value = float(raw)
        except ValueError:
            logger.warning(f"Ignoring non-numeric {self._INIT_TIMEOUT_ENV}={raw!r}")
            return self._DEFAULT_INIT_TIMEOUT
        if value <= 0:
            logger.warning(f"Ignoring non-positive {self._INIT_TIMEOUT_ENV}={raw!r}")
            return self._DEFAULT_INIT_TIMEOUT
        return value

    def get_session(self, session_id: str) -> StataSession:
        if session_id not in self._sessions:
            raise ValueError(f"Session {session_id} not found.")
        session = self._sessions[session_id]
        if session.status == "error":
            raise RuntimeError(
                f"Stata session {session_id} failed to initialize. Stata may be missing or "
                f"inaccessible. Run a command against it to rebuild the session."
            )
        return session

    def list_sessions(self) -> List[SessionInfo]:
        return [s.get_info() for s in self._sessions.values()]

    async def stop_session(self, session_id: str):
        if session_id in self._sessions:
            await self._sessions[session_id].stop()
            del self._sessions[session_id]

    async def stop_all(self):
        tasks = [s.stop() for s in self._sessions.values()]
        if tasks:
            await asyncio.gather(*tasks)
        self._sessions.clear()
