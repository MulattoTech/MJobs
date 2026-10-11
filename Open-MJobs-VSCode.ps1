# Launch the one canonical MJobs folder, not multiple historical roots.
[CmdletBinding()]
param(
    [string]$RepoRoot = $PSScriptRoot,
    [switch]$NoLaunch,
    [switch]$ReuseWindow,
    [switch]$InstallGitGraph
)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
    throw "MJobs must be opened from its canonical Git checkout: $RepoRoot"
}
$code = Get-Command 'code.cmd','code.exe','code' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $code) {
    throw 'VS Code was not found on PATH. Open the MJobs folder directly in VS Code.'
}
if ($InstallGitGraph) {
    & $code.Source --install-extension 'mhutchie.git-graph'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Git Graph extension.' }
}
Write-Host "Single-root MJobs workspace: $RepoRoot"
if ($NoLaunch) { return }
$window = if ($ReuseWindow) { '--reuse-window' } else { '--new-window' }
& $code.Source $window $RepoRoot
exit $LASTEXITCODE
