#!/usr/bin/env bash
# Sync this fork with upstream and rebase the local patch series.
#
#   main  = exact mirror of tmonk/mcp-stata
#   mine  = main + the commits documented in PATCHES.md
#
# Usage: scripts/sync-fork.sh [upstream-branch]   (default: main)
set -euo pipefail

UPSTREAM_BRANCH="${1:-main}"
PATCH_BRANCH="mine"

cd "$(git rev-parse --show-toplevel)"

if ! git remote get-url upstream >/dev/null 2>&1; then
    echo "adding 'upstream' remote -> https://github.com/tmonk/mcp-stata.git"
    git remote add upstream https://github.com/tmonk/mcp-stata.git
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "working tree is dirty; commit or stash first." >&2
    exit 1
fi

git fetch upstream --tags
git checkout main
git merge --ff-only "upstream/${UPSTREAM_BRANCH}"

echo
echo "patch series to replay:"
git log --oneline "main..${PATCH_BRANCH}" || true
echo

git checkout "$PATCH_BRANCH"
if ! git rebase main; then
    cat <<'MSG'

Rebase stopped on a conflict. PATCHES.md explains what each commit was for.
Resolve the hunk, then:

    git add <file> && git rebase --continue

or abandon with `git rebase --abort`.
MSG
    exit 1
fi

echo
echo "rebased. now re-run the regression tests:"
echo "  .venv/Scripts/python.exe -m pytest tests/server/test_stdin_detach_unit.py \\"
echo "      tests/server/test_session_init_recovery_unit.py \\"
echo "      tests/server/test_truncate_text_unit.py tests/execution/startup -q --no-cov"
echo
echo "then push with:  git push --force-with-lease origin main mine"
