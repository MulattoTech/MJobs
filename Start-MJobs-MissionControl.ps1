param(
  [ValidateRange(1024,65535)][int]$Port = 18726,
  [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$base = "http://127.0.0.1:$Port"
$logs = Join-Path $repo '.git\mjobs-coordination'
# On a Git worktree, .git may be a file. Use the primary checkout's git common-dir.
try {
  $shared = (git -C $repo rev-parse --git-common-dir).Trim()
  if (-not [IO.Path]::IsPathRooted($shared)) { $shared = Join-Path $repo $shared }
  $logs = Join-Path ([IO.Path]::GetFullPath($shared)) 'mjobs-coordination'
} catch {
  $logs = Join-Path $env:LOCALAPPDATA 'MJobs\logs'
}
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$bootstrapLog = Join-Path $logs 'mission-control-bootstrap.log'
function LogLine($message) {
  Add-Content -LiteralPath $bootstrapLog -Value ("$(Get-Date -Format o) $message") -Encoding UTF8
}
function Probe {
  try {
    $reply = Invoke-RestMethod -Uri "$base/api/launcher/health" -TimeoutSec 1 -ErrorAction Stop
    return ($reply.service -eq 'mjobs-mission-control' -and $reply.version -ge 4 -and
      $reply.prompt_preview -eq $true -and $reply.approval_required -eq $true -and
      $reply.history_available -eq $true)
  } catch { return $false }
}
try {
  if (-not (Probe)) {
    # Refuse to take over a port already owned by a different process.
    $occupied = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($occupied) { throw "Port $Port already occupied; refusing to replace the existing service." }
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
      # Non-primary development worktrees often share the primary checkout's virtualenv.
      $firstWorktree = (git -C $repo worktree list --porcelain | Where-Object { $_ -like 'worktree *' } | Select-Object -First 1)
      if ($firstWorktree) {
        $candidatePython = Join-Path $firstWorktree.Substring(9) '.venv\Scripts\python.exe'
        if (Test-Path $candidatePython) { $python = $candidatePython }
      }
    }
    if (-not (Test-Path $python)) { throw "MJobs Python environment missing; run local setup first." }
    $output = Join-Path $logs 'mission-control-server-stdout.log'
    $errorFile = Join-Path $logs 'mission-control-server-stderr.log'
    $args = "-m tools.agent_dashboard.server --root `"$repo`" --port $Port --no-open --mission-control"
    $process = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput $output -RedirectStandardError $errorFile
    LogLine "Started localhost Mission Control PID $($process.Id) on $Port."
    $ready = $false
    for ($i = 0; $i -lt 55; $i++) {
      Start-Sleep -Milliseconds 240
      if (Probe) { $ready = $true; break }
      if ($process.HasExited) { break }
    }
    if (-not $ready) { throw "Mission Control did not start. Review server error log: $errorFile" }
  }
  if (-not $NoBrowser) { Start-Process "$base/" }
  LogLine "Opened Mission Control $base/"
  exit 0
} catch {
  LogLine ("ERROR: " + $_.Exception.Message)
  exit 1
}
