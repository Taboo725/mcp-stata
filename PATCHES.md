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
| 5 | `perf(server): budget stata_get_help output instead of returning whole pages` | `src/mcp_stata/server.py` (+1 new test file) |
| 6 | `perf(server): drop pydantic title noise from advertised tool schemas` | `src/mcp_stata/server.py` (+1 new module, +1 new test file) |
| 7 | `test(server): match the compact companion-text default upstream already ships` | `tests/server/test_fastmcp_compact_tool_text.py` |
| 8 | `fix(server): detect packages by resolving commands, not by reading ado dir` | `src/mcp_stata/server.py` (+1 new test file) |
| 9 | `fix(deps): cap mcp below 2.0, whose SDK dropped mcp.server.fastmcp` | `pyproject.toml`, `uv.lock` (+1 new test file) |
| 10 | `fix(smcl): render braces that are not directives as text` | `src/mcp_stata/smcl/smcl2html.py` (+1 new test file) |
| 11 | `fix(client): interrupt Stata via StataSO_SetBreak when sfi.breakIn is absent` | `src/mcp_stata/stata_client.py` (+3 new test files) |
| 12 | `fix(client): count do-file progress on tag-free text` | `src/mcp_stata/stata_client.py` |
| 13 | `fix(sessions): fail pending requests when the worker dies or is stopped` | `src/mcp_stata/sessions.py` (+1 new test file) |
| 14 | `feat(server): live progress, stall diagnosis and auto-background for long runs` | `src/mcp_stata/server.py` (+1 new module, +2 new test files) |
| 15 | `feat(sessions): show queued work and cancel it without breaking the job ahead` | `src/mcp_stata/sessions.py`, `worker.py`, `server.py`, `task_progress.py`, `models.py` (+2 new test files) |
| 16 | `feat(server): session_id="auto" for parallel jobs, and an all-tasks view` | `src/mcp_stata/server.py` (+3 new test files) |

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

## 5. `stata_get_help` returned whole help pages

**Symptom.** One `stata_get_help` call could cost more context than the entire
tool list. Measured against Stata 19, `format="full"` (the old default):

| topic | raw help | after |
|---|---|---|
| `xtreg` | 43,612 chars (~11k tokens) | 4,181 |
| `ivregress` | 22,328 | 4,181 |
| `regress` | 16,744 | 2,759 |
| `merge` | 11,218 | 3,448 |

**Cause.** `_extract_help_format` returned `help_text` unchanged for `"full"`,
and nothing capped any mode. The narrower modes were also mis-anchored: the
`syntax` scan looked for a line *starting with* `syntax`, but rendered help uses
a `## Syntax` markdown heading, so it always fell through to "first 24 lines";
`options`/`examples` took the first line *containing* the word, which for
`regress` is the `[, options]` syntax line — so `format="options"` returned the
syntax table rather than the options section.

**Fix.** Split the page on `## ` headings and select real sections. Add
`brief` — syntax + description, followed by the names of the sections it
dropped — and make it the default, so the model can ask for one section instead
of paying for the page. Cap every mode, `full` included, at
`MCP_STATA_HELP_MAX_CHARS` (default 4,000; `0` disables) via `_truncate_text`,
which grew a `hint` parameter because help, unlike command output, is not
recoverable from a log. Plain-text help has no headings, so those paths keep the
old line heuristics.

**Measured.** Seven common topics: 111,112 chars → 20,409. **-82%.**

**Conflict risk.** Medium. `_extract_help_format` is rewritten wholesale, so an
upstream change to it conflicts as a block — re-apply by taking this version.
The `_truncate_text` signature change is additive and defaults to the old text.

## 6. Every tool schema shipped pydantic's auto-generated titles

**Symptom.** ~4,400 characters (~1,100 tokens) of `"title": "As Json"` beside
`"as_json"`, on every request, in every conversation.

**Cause.** Pydantic titles every model and field, and `model_json_schema()`
emits all of them.

**Fix.** `fastmcp_schema_slim.py` wraps `Tool.from_function` and strips `title`
from each schema node, in both `parameters` and `output_schema`. A property
*named* `title` (`ArtifactRef.title`) is reached through `properties` and
survives. Nothing functional depends on these dicts: arguments validate against
`FuncMetadata.arg_model` and results against `output_model`. Opt out with
`MCP_STATA_SLIM_TOOL_SCHEMAS=0`.

**Measured.** `inputSchema` across all 20 tools: 10,161 → 7,028 chars (-31%).
`outputSchema` 56,000 → 39,256 (-30%); Claude Code does not send `outputSchema`
to the model, but hosts that do now pay less.

**Conflict risk.** Low. One new module plus one call site next to the existing
`install_compact_fastmcp_tool_text()`.

## 7. Upstream's companion-text test contradicts upstream's default

Not our bug: upstream commit `a2f9c4a` changed the default companion mode to
`compact` but left `test_convert_result_omits_duplicate_unstructured_json_by_default`
asserting the old `omit` behaviour, so a clean checkout fails that test. The
test now covers both the `compact` default and `omit`-on-request. Drop this
commit if upstream fixes it.

## 8. `stata_doctor` reported installed packages as missing

**Symptom.** `stata_doctor` warned that `reghdfe`, `estout` and others were not
installed while `which reghdfe` returned
`C:\Data\Stata\ado\plus\r\reghdfe.ado`. On this machine 9 of the 12 probed
packages were false negatives; only `coefplot`, `boottest` and `csdid` were
reported correctly.

**Cause.** `_package_summary` ran `ado` and regex-searched the listing.
`ado dir` reports only packages holding an entry in an ado directory's
`stata.trk`, so a package whose files are installed but whose tracking entry is
absent — copied in by hand, installed while PLUS pointed elsewhere, or left
behind by a reset `stata.trk` — never appears. The listing here named 28
packages and was complete; `reghdfe` and `estout` simply were not in it.

The same scan produced false *positives* in the other direction: `\breghdfe\b`
matched anywhere in the text, including inside another package's description
("'IVREGHDFE': ... requires reghdfe"), so a dependency mentioned in prose read
as installed.

**Fix.** Ask Stata to resolve each command instead. One round trip runs
`capture which` over every target and prints `mcp_pkg:<name>=<0|1>`, which is
what "is this command available" actually means — it honours the whole adopath
and does not care how the files arrived. `MCP_STATA_EXTRA_PACKAGES` (comma- or
space-separated) adds targets to the built-in list; entries that are not plain
identifiers are dropped, since names are interpolated into Stata code.

**Measured.** Same machine: 10 of 12 present, and the two negatives verified
genuine (`which gcollapse` also fails, so `gtools` really is absent).
`stata_doctor` now warns about `gtools, eventstudyinteract` and nothing else.

**Conflict risk.** Low. `_package_summary` is replaced wholesale and gains two
helpers next to it; the `detect` branch gains one extra session call.

## 9. MCP SDK 2.0 breaks start-up

**Symptom.** A fresh `pip install mcp-stata` (or `uv lock --upgrade`) pulls
`mcp` 2.x and the server dies on launch with
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'` (upstream issue #64).

**Cause.** `pyproject.toml` required `mcp[cli]>=1.0.0` with no upper bound, and
SDK 2.0 removed the `mcp.server.fastmcp` package the server is built on.

**Fix.** Require `mcp[cli]>=1.0.0,<2`. The lock stays on 1.24.0; an upgrade now
resolves within 1.x (1.30.0 at the time of writing). A unit test asserts the
bound so it is not lost in a rebase.

**Conflict risk.** Low, one line in `pyproject.toml` plus the matching lock
entry. If upstream adds its own bound, drop this commit; if it ports to SDK 2,
drop it and re-lock.

## 10. Literal `{}` in output failed the whole command

**Symptom.** Any command whose echo or output contained `{}` or `{ }` -- a
Mata loop body, `display "{}"`, a regex -- failed with
`list index out of range`, and its real output was lost.

**Cause.** `_render_fragment` in `smcl2html.py` took `tag_name.split()[0]`
without checking for an empty tag; `{:x}` crashed the same way one line up.

**Fix.** Emit tags that are not directives verbatim.

**Conflict risk.** Low, a guard in one function.

## 11. `break` and `cancel` never interrupted Stata

**Symptom.** `stata_control(action="break")` had no effect. `cancel` marked the
task failed at once, but Stata kept running, and the next command on that
session queued silently behind it: 60s in testing, indefinitely for a long
estimation. To an agent this looks exactly like a hung server.

**Cause.** Both paths called `sfi.breakIn`, which current runtimes (StataNow
19.5) do not have; the code logged at debug level and returned.

**Fix.** `_break_hook()` falls back to `pystata.config.stlib.StataSO_SetBreak()`,
the call pystata itself makes on Ctrl+C. `sfi.breakIn` is still preferred where
it exists.

**Measured.** `sleep`, `forvalues` and a busy Mata loop: never interrupted →
stopped within 0.5s, next command immediate. A break sent while idle does not
leak into the next command. Upstream's `test_break_session_tool_logic`
(issue #34) now passes.

**Conflict risk.** Low. `_request_break_in`/`_request_break_in_fast` are
rewritten around one new static helper.

## 12. Do-file progress stuck at 0/N

**Cause.** The line counter matched `^\.\s+\S` against raw SMCL, where an
echoed command reads `{com}. regress ...`, so it never matched. It also
throttled reports to one per 0.25s, which could strand the last count before a
long silent command.

**Fix.** Match on tag-free text, carry a split line over to the next chunk,
report on every change.

**Conflict risk.** Low, inside `run_do_file_streaming`'s local callback.

## 13. A dead worker left its request hanging forever

**Symptom.** Kill a worker (crash, OOM, `taskkill`) mid-command and the task
stays `running` forever; `stata_manage_session(action="stop")` on a busy
session does the same.

**Cause.** The parent keeps its own copy of the child's pipe end, so a dead
worker does not reliably produce EOF; when EOF did come, or on `stop()`, the
pending futures were never resolved.

**Fix.** The listener checks worker liveness while requests are pending and,
on a closed pipe, a dead worker or a stop, fails them with a message that says
which. A listener merely replaced on an event-loop change leaves them alone. A
raising log/progress callback no longer kills the listener.

**Measured.** Killed worker: hung → task `failed` ("exited unexpectedly",
with exit code) within ~1s; the next call rebuilds the session.

**Conflict risk.** Medium. `_listen_to_worker`'s loop is restructured.

## 14. Long runs were a black box

**Symptom.** A long `stata_run` showed only "calling MCP tool" with no way to
tell progress from a hang; `stata_task_status` could only say `running`.

**Fix.**

- Every execution keeps a progress record (`task_progress.py`), fed from the
  log and progress streams the worker already emits.
- `stata_task_status` returns `data.progress`: elapsed time, lines done/total
  and percent for do-files, current command, last output, seconds since the last
  output, worker liveness and a `state`:
  `running` | `busy_silent` (no output, CPU busy: a long estimation) |
  `possibly_stuck` (no output, CPU idle: a `--more--`, dialog, lock or network
  wait) | `worker_dead`, plus a one-line `summary` and matching advice. Worker
  CPU is sampled only once output has been quiet for `MCP_STATA_STALL_SECONDS`
  (default 60), via `GetProcessTimes` on Windows and `/proc` on Linux.
- `wait=True` returns at least every `MCP_STATA_TASK_WAIT_MAX` seconds (default
  120; `0` disables), so a long job reads as a sequence of progress reports.
- A synchronous `stata_run` still running after `MCP_STATA_AUTO_BACKGROUND_AFTER`
  seconds (default 60; `0` disables) returns `auto_backgrounded: true` with a
  `task_id` and keeps running as a background task. Cancelling the request
  before then still stops the Stata command.
- A task cancelled before its first step used to stay `running` forever and
  leave its Stata call alive; a done-callback now finalizes it.

**Conflict risk.** Medium. `stata_run`'s sync branch and the body of
`stata_task_status` change; the rest is additive.

## 15. Queued work looked like running (or stuck) work

**Symptom.** A second job sent to a busy session reported `running`, and after
a minute of waiting the stall diagnosis would call it stuck. Worse,
`stata_run(background=True)` on a busy session did not return at all until the
previous job finished, because it waited for a log path that only appears once
a command starts executing. And once patch 11 made break real, cancelling a
*queued* job sent a break that killed the job ahead of it.

**Fix.**

- `StataSession` records in-flight requests in send order -- the worker's FIFO
  order -- and exposes `queue_state(label)` / `busy_with()`; `stata_run` tags its
  call with the task id.
- Cancelling sends `break` only for the executing request. A queued one is
  withdrawn with a new out-of-band `{"type": "cancel", "target": id}` message;
  the worker skips it unstarted and answers "Cancelled while queued".
- A waiting task reports `status: "queued"`, `queued_behind`, progress state
  `queued` (never quiet/stuck). Background submissions to a busy session return
  at once as queued; a promoted sync call says the same. Both warn that a
  separate `session_id` runs in parallel. Queue time is reported as `queued_s`
  and no longer counts toward quiet time.

**Measured.** Submitting behind an 8s job: blocked ~8s → returns in <0.1s.
Cancelling the queued job leaves the running one to finish normally.

**Conflict risk.** Medium. `_call_raw`'s cancellation branch and the worker's
main loop change; the rest is additive.

## 16. Parallel work needed manual session juggling

**Fix.**

- `stata_run(session_id="auto")` picks an idle `auto-N` session, else starts a
  new one up to `MCP_STATA_MAX_AUTO_SESSIONS` (default 3), else queues on the
  least loaded. The choice is made and reserved synchronously, so concurrent
  auto runs never collide. It never uses `default`, so interactive state is
  safe; the result's `session_id` names the session used. If a new session fails
  to start (e.g. no free network-licence seat) the run falls back to an existing
  auto session and no more are created.
- `stata_task_status(task_id="all")` returns one row per active task plus each
  task finished since the last check, reported once; with `wait=True` it returns
  as soon as any task finishes. Single-task status rows now carry `session_id`.

**Measured.** Three 6s jobs: 19.3s on one session → 6.7s with `auto`.

**Conflict risk.** Low. New helpers plus a few lines at the top of `stata_run`
and `stata_task_status`.

## What is *not* worth doing

Measured and rejected, so it does not get re-proposed:

- **Dropping `outputSchema`.** Claude Code sends only `description` and
  `inputSchema` to the model; the 2.8KB envelope schema repeated across 20 tools
  costs nothing there.
- **`MCP_STATA_STRUCTURED_COMPANION_TEXT=omit`.** The payload arrives once, not
  twice. `omit` saves bandwidth, not context, and risks blanking output on hosts
  that read `content[]`.

## Syncing with upstream

```sh
scripts/sync-fork.sh          # bash / Git Bash
scripts\sync-fork.ps1         # PowerShell
```

Either one fetches `upstream`, fast-forwards `main`, rebases `mine` onto it and
reminds you to re-run the tests. On a conflict, fix the hunk, `git add`, then
`git rebase --continue`; this file tells you what each commit was for.

Regression tests for the patch series:

```sh
.venv/Scripts/python.exe -m pytest \
  tests/server/test_stdin_detach_unit.py \
  tests/server/test_session_init_recovery_unit.py \
  tests/server/test_truncate_text_unit.py \
  tests/server/test_help_budget_unit.py \
  tests/server/test_schema_slim_unit.py \
  tests/server/test_fastmcp_compact_tool_text.py \
  tests/server/test_package_detection_unit.py \
  tests/server/test_mcp_sdk_bound_unit.py \
  tests/ui/test_smcl_empty_braces_unit.py \
  tests/client/test_break_hook_unit.py \
  tests/server/test_session_worker_death_unit.py \
  tests/server/test_task_progress_unit.py \
  tests/execution/cancellation/test_native_break_e2e.py \
  tests/execution/streaming/test_task_progress_e2e.py \
  tests/server/test_session_queue_unit.py \
  tests/server/test_auto_sessions_unit.py \
  tests/execution/cancellation/test_session_queue_e2e.py \
  tests/execution/cancellation/test_auto_sessions_e2e.py \
  tests/execution/cancellation/test_mixed_workload_e2e.py \
  tests/execution/startup \
  -q --no-cov
```
