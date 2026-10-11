param()
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
if(-not (Get-Command codex -ErrorAction SilentlyContinue)) {
  throw 'Codex CLI is not installed on PATH.'
}
Write-Host "Launching interactive Codex in $PSScriptRoot"
& codex
exit $LASTEXITCODE
