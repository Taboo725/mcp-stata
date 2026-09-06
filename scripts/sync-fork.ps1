<#
.SYNOPSIS
    Sync this fork with upstream and rebase the local patch series.
.DESCRIPTION
    main = exact mirror of tmonk/mcp-stata
    mine = main + the commits documented in PATCHES.md
#>
param([string]$UpstreamBranch = "main")

$ErrorActionPreference = "Stop"
$PatchBranch = "mine"

Set-Location (git rev-parse --show-toplevel)

git remote get-url upstream *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "adding 'upstream' remote -> https://github.com/tmonk/mcp-stata.git"
    git remote add upstream https://github.com/tmonk/mcp-stata.git
}

if (git status --porcelain) {
    Write-Error "working tree is dirty; commit or stash first."
}

git fetch upstream --tags
git checkout main
git merge --ff-only "upstream/$UpstreamBranch"
if ($LASTEXITCODE -ne 0) { Write-Error "main is not a clean mirror of upstream any more." }

Write-Host ""
Write-Host "patch series to replay:"
git log --oneline "main..$PatchBranch"
Write-Host ""

git checkout $PatchBranch
git rebase main
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Rebase stopped on a conflict. PATCHES.md explains what each commit was for."
    Write-Host "Resolve the hunk, then: git add <file>; git rebase --continue"
    Write-Host "or abandon with: git rebase --abort"
    exit 1
}

Write-Host ""
Write-Host "rebased. now re-run the regression tests:"
Write-Host "  .venv\Scripts\python.exe -m pytest tests\server\test_stdin_detach_unit.py ``"
Write-Host "      tests\server\test_session_init_recovery_unit.py ``"
Write-Host "      tests\server\test_truncate_text_unit.py tests\execution\startup -q --no-cov"
Write-Host ""
Write-Host "then push with:  git push --force-with-lease origin main mine"
