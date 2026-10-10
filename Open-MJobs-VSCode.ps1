# Portable launcher for all local MJobs Git sources in VS Code.
# Rebuilds an external .code-workspace file; never mutates MJobs branches.
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$WorkspaceFile,
    [ValidateRange(0,250)][int]$RecentWorktrees = 25,
    [switch]$AllWorktrees,
    [switch]$AllowHugeWorkspace,
    [switch]$NoLaunch,
    [switch]$ReuseWindow,
    [switch]$InstallGitGraph
)
$ErrorActionPreference = 'Stop'
$devRoot = Split-Path -Parent $PSScriptRoot
if (-not $RepoRoot) {
    $RepoRoot = Join-Path $devRoot 'MJobs'
}
if (-not $WorkspaceFile) {
    $WorkspaceFile = Join-Path $devRoot 'MJobs-MultiRepo.generated.code-workspace'
}
$helper = Join-Path $PSScriptRoot 'tools\mjobs_vscode_workspace.py'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
    throw "The GitHub-versioned VS Code workspace builder is missing: $helper"
}
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    # The builder only uses Python's standard library.
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $found) {
        throw 'Python is unavailable. Install Python or create the MJobs virtual environment.'
    }
    $python = $found.Source
}
$arguments = @(
    $helper, '--repo-root', $RepoRoot, '--output', $WorkspaceFile,
    '--recent-worktrees', [string]$RecentWorktrees
)
if ($AllWorktrees) { $arguments += '--all-worktrees' }
if ($AllowHugeWorkspace) { $arguments += '--allow-huge-workspace' }
if ($NoLaunch) { $arguments += '--no-launch' }
if ($ReuseWindow) { $arguments += '--reuse-window' }
if ($InstallGitGraph) { $arguments += '--install-git-graph' }
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to build or open the local MJobs VS Code workspace.'
}
