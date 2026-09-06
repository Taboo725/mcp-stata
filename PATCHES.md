# Fork patches

This fork tracks `tmonk/mcp-stata`. `main` is kept as an exact mirror of
upstream; every local change lives on `mine`, which is `main` plus the commits
listed below, in order, with no merge commits.

Keeping the series linear and each commit small is what makes
`git rebase upstream/main` cheap after a `sync fork`.

| # | Commit subject | Files touched |
|---|---|---|
| 1 | `fix(server): stop worker spawn from deadlocking on the stdio stdin pipe` | `src/mcp_stata/server.py` (+1 new test file) |
| 2 | `fix(sessions): discard sessions that fail to initialize instead of poisoning the pool` | `src/mcp_stata/sessions.py` (+1 new test file) |
| 3 | `fix(client): stop the post-`clear all` startup reload from eating command output` | `src/mcp_stata/stata_client.py`, `tests/execution/startup/test_startup_do_file_unit.py` (+1 new test file) |
| 4 | `fix(server): keep the head as well as the tail when truncating output` | `src/mcp_stata/server.py`, `tests/execution/errors/test_error_visibility_e2e.py` (+1 new test file) |

## 1. Worker spawn deadlocks on the stdio stdin pipe (Windows)

**Symptom.** The first call on any newly created session times out after 45s
with `Stata session <id> timed out during initialization`, every time. The
session created at server start-up is fine.

**Cause.** A spawned worker inherits the server's standard handles. On Windows
the worker's interpreter start-up seeks fd 0 while building `sys.stdin`, and
that seek blocks for as long as the server is parked in a synchronous read of
the same pipe object — i.e. for the whole gap between JSON-RPC messages. The
worker never reaches its target, so it never reports `ready`. Verified with
`py-spy dump --native`: the stuck child sits in
`Py_InitializeFromConfig → … → NtQueryInformationFile`, with no Python frames,
and only unblocks when the client happens to send the next request. The
start-up session is unaffected because it is spawned before the transport
starts reading.

**Fix.** `_detach_stdin_from_children()` in `server.py` points fd 0 at the null
device before serving and keeps the transport on a private duplicate, so
children inherit a handle nobody is blocked on. Windows only.
`MCP_STATA_NO_STDIN_DETACH=1` opts out.

**Measured.** First call on a new session: 45.06s (timeout) → 2.6s.

**Conflict risk.** Low. One new self-contained function plus one call site in
`main()`, next to the existing `_install_stdout_filter()`.

## 2. A session that fails to start poisons the pool

**Symptom.** After the timeout above, *every* later call on that session id
returns `failed to initialize. Stata binary may be missing or inaccessible`,
which is permanent and wrong. The only cure was
`stata_manage_session(action="stop")`.

**Cause.** `get_or_create_session` flipped the session to `"error"` but left it
in `_sessions`, and then neither rebuilt nor re-waited.

**Fix.** Discard an unusable session (error status, or a worker that is no
longer alive) before returning, so the next call builds a fresh worker. Report
what actually happened — init timeout vs. worker exit — and make the wait
configurable via `MCP_STATA_SESSION_INIT_TIMEOUT` (default 45s).

**Conflict risk.** Medium: it rewrites the body of `get_or_create_session`. If
upstream touches that method, re-apply by hand — the shape is small.

## 3. `clear all` floods the output with the startup reload

**Symptom.** `display "A"` / `clear all` / `display "B"` returns ~5KB of
`statest.mata` bootstrap and neither marker. Any do-file starting with
`clear all` came back unusable.

**Cause.** Program-clearing commands make the client re-run its startup files.
The reload used `do`, which echoes every line of the file, and it ran *before*
the command's SMCL log was read — so the bootstrap was appended to the user's
own output, and tail-only truncation (see patch 4) then dropped the real
results.

**Fix.** Two changes, each sufficient alone, together also covering the
reload's error messages (`program drop _all` leaves Mata alone, so re-running
`statest.mata` reports "already exists", and error text ignores `quietly`):

- read the log before triggering the reload, in all three exec paths
  (`run_command`, `run_do_file`, `_exec_with_capture`);
- `_load_startup_do_file(quiet=True)` uses `run`/`capture` instead of
  `do`/`capture noisily`, keeping the log file itself clean.

`MCP_STATA_NO_RELOAD_ON_CLEAR=1` (upstream's existing escape hatch) still
disables the reload entirely; it is no longer needed.

**Measured.** 5194 characters with both markers missing → 77 characters with
both present.

**Conflict risk.** Medium. Three small reorderings in long functions plus a
parameter on `_load_startup_do_file`. `git rebase` usually reapplies them
cleanly because the surrounding context lines are distinctive.

## 4. Truncation kept only the tail

`_truncate_text` returned only the last 5000 characters. Stata puts the echoed
command and any error at the top and the results table at the bottom, so a
tail-only window can drop the lines that say what ran. Now keeps the first
third and the last two thirds of the budget.

**Conflict risk.** Low, one small function.

## Syncing with upstream

```sh
scripts/sync-fork.sh          # bash / Git Bash
scripts\sync-fork.ps1         # PowerShell
```

Either one fetches `upstream`, fast-forwards `main`, rebases `mine` onto it and
reminds you to re-run the tests. On a conflict, fix the hunk, `git add`, then
`git rebase --continue`; this file tells you what each commit was for.

Regression tests for all four patches:

```sh
.venv/Scripts/python.exe -m pytest \
  tests/server/test_stdin_detach_unit.py \
  tests/server/test_session_init_recovery_unit.py \
  tests/server/test_truncate_text_unit.py \
  tests/execution/startup \
  -q --no-cov
```
