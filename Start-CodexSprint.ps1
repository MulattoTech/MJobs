#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Run,
    [switch]$Resume,
    [ValidateRange(0,100000)][int]$MaxIterations = 0,
    [ValidatePattern('^[A-Za-z0-9_.:/-]+$')][string]$Model = 'gpt-6.1-sol'
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$skill = Join-Path $HOME '.codex/skills/afk-dev/scripts/afk_loop.py'
if (-not (Test-Path -LiteralPath $skill)) { throw 'The installed AFK-DEV skill was not found.' }
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $root '.venv/bin/python' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the project Python environment using docs/DEVELOPMENT.md first.' }
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) { throw 'Codex CLI must be on PATH.' }
$goal = Join-Path $root 'prompts/codex/next-main.md'
$verification = ConvertTo-Json -Compress -InputObject @($python, '-m', 'tools.ci.release_gate', '--scope', 'p0p1', '--online')
$arguments = @($skill, 'run', '--project', $root, '--goal-file', $goal,
    '--model', $Model, '--effort', 'medium', '--max-iterations', [string]$MaxIterations,
    '--max-stagnant', '3', '--ci', 'on', '--browser', 'on', '--verify', $verification)
if ($Resume) { $arguments += '--resume' }
if (-not $Run) { $arguments += '--dry-run'; Write-Host 'Dry run only. Add -Run to start the authorized development loop.' }
Push-Location $root
try { & $python @arguments; exit $LASTEXITCODE } finally { Pop-Location }
