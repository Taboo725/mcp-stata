"""Live progress for long-running Stata executions.

A background ``stata_run`` used to be a black box: ``stata_task_status`` could
only say ``running``, so neither the model nor the person watching could tell a
two-hour regression from a hung session. This module keeps a small, cheap
record per task, fed from the two streams the worker already emits -- log
chunks and progress events -- and turns it into a snapshot that answers the
questions that matter: how far along, for how long, doing what, and is the
worker actually computing or sitting idle.

Everything here must be exception-safe. The observers run inside the session
listener; an exception escaping them would tear that listener down.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

STALL_SECONDS_ENV = "MCP_STATA_STALL_SECONDS"
DEFAULT_STALL_SECONDS = 60.0

# Below this, a silent worker is treated as idle rather than computing.
_IDLE_CPU_PERCENT = 5.0
_MAX_SNIPPET = 160

_SMCL_TAG_RE = re.compile(r"\{[^{}]*\}")
_COMMAND_ECHO_RE = re.compile(r"^\.\s+(\S.*)$")
_EVENT_PREFIX = '{"event"'


def stall_seconds() -> float:
    raw = (os.environ.get(STALL_SECONDS_ENV) or "").strip()
    if not raw:
        return DEFAULT_STALL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STALL_SECONDS
    return value if value > 0 else DEFAULT_STALL_SECONDS


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_SNIPPET:
        return text
    return text[: _MAX_SNIPPET - 1] + "…"


@dataclass
class TaskProgress:
    """Mutable progress record for one execution."""

    started_mono: float = field(default_factory=time.monotonic)
    finished_mono: Optional[float] = None
    progress: Optional[float] = None
    total: Optional[float] = None
    message: Optional[str] = None
    last_output_mono: Optional[float] = None
    output_chars: int = 0
    last_line: Optional[str] = None
    current_command: Optional[str] = None
    # (monotonic time, cumulative worker CPU seconds) from the previous sample.
    cpu_sample: Optional[tuple[float, float]] = None

    def observe_chunk(self, text: Any) -> None:
        """Record one streamed log chunk. JSON control events are ignored."""
        try:
            if not isinstance(text, str) or not text:
                return
            if text.lstrip().startswith(_EVENT_PREFIX):
                return
            self.last_output_mono = time.monotonic()
            self.output_chars += len(text)
            seen_line = False
            for raw in reversed(text.splitlines()):
                line = _SMCL_TAG_RE.sub("", raw).strip()
                if not line:
                    continue
                if not seen_line:
                    self.last_line = _clip(line)
                    seen_line = True
                match = _COMMAND_ECHO_RE.match(line)
                if match:
                    self.current_command = _clip(match.group(1))
                    break
        except Exception:
            pass

    def observe_progress(self, progress: Any, total: Any, message: Any) -> None:
        try:
            self.progress = float(progress) if progress is not None else None
            self.total = float(total) if total is not None else None
            self.message = str(message) if message is not None else None
        except Exception:
            pass

    def mark_finished(self) -> None:
        if self.finished_mono is None:
            self.finished_mono = time.monotonic()


def process_cpu_seconds(pid: Optional[int]) -> Optional[float]:
    """Cumulative user+kernel CPU seconds of *pid*, or ``None`` if unknown."""
    if not pid:
        return None
    try:
        if sys.platform == "win32":
            return _win_cpu_seconds(pid)
        stat_path = f"/proc/{pid}/stat"
        if os.path.exists(stat_path):
            with open(stat_path, "r", encoding="ascii", errors="replace") as fh:
                raw = fh.read()
            # Field 2 (comm) may contain spaces; everything after the last ')'
            # is space-separated, with utime/stime at positions 14/15 overall.
            fields = raw[raw.rindex(")") + 2 :].split()
            ticks = float(fields[11]) + float(fields[12])
            return ticks / float(os.sysconf("SC_CLK_TCK"))
    except Exception:
        return None
    return None


def _win_cpu_seconds(pid: int) -> Optional[float]:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return None

        def _ticks(ft: wintypes.FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        return (_ticks(kernel) + _ticks(user)) / 1e7  # 100ns units
    finally:
        kernel32.CloseHandle(handle)


async def sample_cpu_percent(record: TaskProgress, pid: Optional[int], min_window: float = 0.5) -> Optional[float]:
    """CPU use of the worker since the last sample (100 = one core busy).

    Reuses the previous sample when it is old enough, so repeated status polls
    cost nothing; otherwise takes a short fresh window.
    """
    first = process_cpu_seconds(pid)
    if first is None:
        return None
    now = time.monotonic()
    prev = record.cpu_sample
    if prev is None or now - prev[0] < min_window:
        await asyncio.sleep(min_window)
        prev = (now, first)
        first = process_cpu_seconds(pid)
        now = time.monotonic()
        if first is None:
            return None
    record.cpu_sample = (now, first)
    span = now - prev[0]
    if span <= 0:
        return None
    return round(max(0.0, (first - prev[1]) / span * 100.0), 1)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def build_snapshot(
    record: TaskProgress,
    *,
    status: str,
    worker_alive: Optional[bool] = None,
    cpu_percent: Optional[float] = None,
    queued_behind: Optional[str] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Compact, model-facing description of where an execution stands."""
    now = time.monotonic() if now is None else now
    end = record.finished_mono if record.finished_mono is not None else now
    elapsed = max(0.0, end - record.started_mono)
    snap: dict[str, Any] = {"elapsed_s": round(elapsed, 1)}

    has_fraction = record.total is not None and record.total > 1 and record.progress is not None
    if has_fraction:
        snap["lines_done"] = int(record.progress)
        snap["lines_total"] = int(record.total)
        snap["percent"] = round(100.0 * record.progress / record.total, 1)

    quiet_for: Optional[float] = None
    if record.finished_mono is None:
        reference = record.last_output_mono or record.started_mono
        quiet_for = max(0.0, now - reference)
        snap["quiet_for_s"] = round(quiet_for, 1)
    if record.current_command:
        snap["current_command"] = record.current_command
    if record.last_line and record.last_line != record.current_command:
        snap["last_output"] = record.last_line
    snap["output_chars"] = record.output_chars
    if worker_alive is not None:
        snap["worker_alive"] = worker_alive
    if cpu_percent is not None:
        snap["worker_cpu_percent"] = cpu_percent
    if queued_behind:
        snap["queued_behind"] = queued_behind

    snap["state"] = _classify(status, quiet_for, worker_alive, cpu_percent)
    snap["summary"] = _summary(status, snap)
    return snap


def _classify(
    status: str,
    quiet_for: Optional[float],
    worker_alive: Optional[bool],
    cpu_percent: Optional[float],
) -> str:
    if status in {"done", "failed", "queued", "cancelling"}:
        return status
    if worker_alive is False:
        return "worker_dead"
    if quiet_for is None or quiet_for < stall_seconds():
        return "running"
    if cpu_percent is None:
        return "quiet"
    return "busy_silent" if cpu_percent >= _IDLE_CPU_PERCENT else "possibly_stuck"


def _summary(status: str, snap: dict[str, Any]) -> str:
    state = snap["state"]
    parts = [state.replace("_", " ")]
    if "percent" in snap:
        parts.append(f"≈{snap['lines_done']}/{snap['lines_total']} lines ({snap['percent']:.0f}%)")
    parts.append(f"{_fmt_duration(snap['elapsed_s'])} elapsed")
    if "quiet_for_s" in snap and state not in {"queued"}:
        parts.append(f"last output {_fmt_duration(snap['quiet_for_s'])} ago")
    if "worker_cpu_percent" in snap:
        parts.append(f"cpu {snap['worker_cpu_percent']:.0f}%")
    if snap.get("queued_behind"):
        parts.append(f"waiting for task {snap['queued_behind']}")
    if snap.get("current_command") and state not in {"done", "failed", "queued"}:
        parts.append(f"now: {snap['current_command']}")
    return " · ".join(parts)


def stall_advice(snapshot: dict[str, Any], session_id: str) -> Optional[str]:
    """A next action to surface when the snapshot looks unhealthy."""
    state = snapshot.get("state")
    if state == "possibly_stuck":
        return (
            f"No output for {_fmt_duration(snapshot.get('quiet_for_s', 0))} and the worker is idle "
            f"(~{snapshot.get('worker_cpu_percent', 0):.0f}% CPU). Stata may be blocked on a --more-- "
            f"prompt, a dialog, network I/O or a file lock. Inspect with stata_read_log, or interrupt "
            f"with stata_control(action='break', id='{session_id}')."
        )
    if state == "worker_dead":
        return (
            "The Stata worker process is no longer alive; this task cannot finish. "
            "The session is rebuilt on the next call."
        )
    if state == "busy_silent":
        return (
            "Silent but busy: the worker is using CPU, so a long estimation is most likely still "
            "computing. Keep waiting."
        )
    return None
