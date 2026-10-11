#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Run,
    [switch]$Resume,
    [switch]$Status,
    [ValidateRange(0,100000)][int]$MaxIterations = 0,
    [ValidatePattern('^[A-Za-z0-9_.:/-]+$')][string]$Model = 'gpt-6.1-sol'
)
$ErrorActionPreference = 'Stop'
if ($Status -and $Run) { throw 'Use either -Status or -Run, not both.' }
$root = $PSScriptRoot
$skill = Join-Path $HOME '.codex/skills/afk-dev/scripts/afk_loop.py'
if (-not (Test-Path -LiteralPath $skill)) { throw 'The installed AFK-DEV skill was not found.' }
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $root '.venv/bin/python' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the project Python environment using docs/DEVELOPMENT.md first.' }
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) { throw 'Codex CLI must be on PATH.' }
$goal = Join-Path $root 'prompts/codex/next-main.md'
$verification = ConvertTo-Json -Compress -InputObject @($python, '-m', 'tools.ci.release_gate', '--scope', 'p0p1', '--online')
$arguments = @($skill, 'run', '--project', $root,
    '--model', $Model, '--effort', 'medium', '--max-iterations', [string]$MaxIterations,
    '--max-stagnant', '3', '--ci', 'on', '--browser', 'on')
if ($Resume) {
    # Resume the stored goal and independent verification contract unchanged.
    # A newer prompt file after git pull must not silently replace that contract.
    $arguments += '--resume'
} else {
    $arguments += @('--goal-file', $goal, '--verify', $verification)
}
if ($Status) { $arguments = @($skill, 'status', '--project', $root) }
if (-not $Run -and -not $Status) {
    Write-Host 'Dry run only. Add -Run to start; -Run -Resume preserves the existing goal.'
    [pscustomobject]@{ Interpreter = $python; Arguments = $arguments; StartsCodex = $false } | ConvertTo-Json -Depth 4
    return
}
Push-Location $root
try { & $python @arguments; exit $LASTEXITCODE } finally { Pop-Location }
