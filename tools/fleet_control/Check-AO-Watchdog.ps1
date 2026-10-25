$ErrorActionPreference='Stop'
$processes=@(
  Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" -ErrorAction Stop |
    Where-Object {
      $_.CommandLine -and
      $_.CommandLine -match '(?i)(?:^|\s)-File\s+"?[^"]*Emergency-Stop-Codex\.ps1(?:["\s]|$)'
    } | Select-Object -ExpandProperty ProcessId
)
@{active=($processes.Count -gt 0);watchdogPids=$processes;checkedAt=(Get-Date).ToUniversalTime().ToString('o')} | ConvertTo-Json -Depth 3 -Compress
