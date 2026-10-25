param(
 [string]$Root = 'C:\Tools\AO-Fleet-Control',
 [switch]$DryRun,
 [int]$CheckIntervalSeconds = 8
)
$ErrorActionPreference = 'Continue'
$LatchPath = Join-Path $Root 'emergency-latch.json'
$StatusPath = Join-Path $Root 'emergency-status.json'
$LogPath = Join-Path $Root 'emergency-audit.jsonl'
$targets = @('codex','codex-code-mode-host','node_repl','ao','agent-orchestrator')
function Save-Json([string]$FilePath,[object]$Value) {
  $tmp = $FilePath + '.tmp'
  $json = ConvertTo-Json -InputObject $Value -Depth 12 -Compress
  [System.IO.File]::WriteAllText($tmp,$json,[System.Text.UTF8Encoding]::new($false))
  [System.IO.File]::Move($tmp,$FilePath,$true)
}
function Event([string]$Name,[object]$Data) {
  $record = @{ timestamp=(Get-Date).ToUniversalTime().ToString('o'); event=$Name; data=$Data }
  [System.IO.File]::AppendAllText($LogPath,((ConvertTo-Json -InputObject $record -Compress -Depth 6) + [Environment]::NewLine))
}
function Get-TargetProcesses([string]$Name) {
  $processes = @(Get-Process -Name $Name -ErrorAction SilentlyContinue)
  if ($Name -ne 'codex') { return $processes }
  $managed = @()
  $quotaStatus = $null
  try { $quotaStatus=Get-Content -Raw (Join-Path $Root 'quota-guard-status.json') -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch {}
  foreach ($process in $processes) {
    # A shared app-server can own active inference after its frontend exits.
    # Exempt only the exact, freshly registered, guard-owned stdio monitor.
    $details=Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)" -ErrorAction SilentlyContinue
    $isQuotaMonitor=$false
    if ($details -and $quotaStatus.monitorIdentity.monitorPid -eq $process.Id) {
      $guard=Get-CimInstance Win32_Process -Filter "ProcessId=$($details.ParentProcessId)" -ErrorAction SilentlyContinue
      $isQuotaMonitor=Test-FleetQuotaMonitorIdentity -Details $details -Guard $guard -Status $quotaStatus -Root $Root
    }
    if ($isQuotaMonitor) { continue }
    $managed += $process
  }
  return $managed
}
function Count-Targets {
  $result = @{}
  foreach ($name in $targets) {
    $result[$name] = @(Get-TargetProcesses $name).Count
  }
  return $result
}
function Is-Latched {
  if (!(Test-Path $LatchPath)) { return $false }
  try {
    $v=Get-Content -LiteralPath $LatchPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($v.enabled -isnot [bool]) { return $true }
    return $v.enabled -eq $true
  } catch {return $true}
}
if (!(Test-Path $Root)) { throw "Dashboard directory is missing" }
$identityHelper=Join-Path $PSScriptRoot 'Quota-Monitor-Identity.ps1'
if (Test-Path -LiteralPath $identityHelper) { . $identityHelper }
elseif ($DryRun -or (Is-Latched)) { throw 'Monitor identity helper unavailable; emergency verification cannot proceed' }
if ($DryRun) {
  $count=Count-Targets
  $result=@{ dryRun=$true; counts=$count; wouldTerminate=($targets -join ', '); dashboardExcluded=$true; timestamp=(Get-Date).ToUniversalTime().ToString('o') }
  Write-Output (ConvertTo-Json -InputObject $result -Depth 6)
  exit 0
}
if (!(Is-Latched)) {
  Save-Json $StatusPath @{active=$false;phase='disarmed';checkedAt=(Get-Date).ToUniversalTime().ToString('o');watchdogPid=$PID;dashboardPreserved=$true;reason='Latch released before watchdog started'}
  Write-Output 'Emergency latch is not set; no processes terminated.'
  exit 0
}
$rootHash=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))).Substring(0,16)
$watchdogMutex=[Threading.Mutex]::new($false,('Local\MJobsEmergencyWatchdog_'+$rootHash))
$ownsWatchdog=$false
try { $ownsWatchdog=$watchdogMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsWatchdog=$true }
if (!$ownsWatchdog) { $watchdogMutex.Dispose(); Write-Output 'Emergency watchdog already running.'; exit 0 }
try {
$started=(Get-Date).ToUniversalTime().ToString('o')
Event 'watchdog_started' @{ pid=$PID; targets=$targets }
$failures=New-Object 'System.Collections.Generic.List[string]'
$deniedUntil=@{}
$lastAuditSummary=''
$round=0
do {
  $round++
  $countsBefore=Count-Targets
  $totalBefore=($targets | ForEach-Object {$countsBefore[$_] } | Measure-Object -Sum).Sum
  if ($totalBefore -gt 0) {
    # Stop the orchestration host first so it cannot relaunch the Codex sessions.
    foreach ($name in @('agent-orchestrator','ao','codex','codex-code-mode-host','node_repl')) {
      $processes=@(Get-TargetProcesses $name)
      foreach ($process in $processes) {
        if ($deniedUntil.ContainsKey($process.Id) -and [DateTime]::UtcNow -lt $deniedUntil[$process.Id]) { continue }
        # Kill only the verified target PID and descendants. Broad /IM would
        # repeatedly disrupt hosted Codex app-server and exec-server processes.
        $output = & "$env:SystemRoot\System32\taskkill.exe" /F /T /PID $process.Id 2>&1
        $code=$LASTEXITCODE
        if ($code -ne 0 -and (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
          $detail=(($output | Out-String).Trim())
          if ($detail -match 'Access is denied') { $deniedUntil[$process.Id]=[DateTime]::UtcNow.AddMinutes(5) }
          $message="$name/$($process.Id) taskkill exit $($code): " + $detail.Substring(0,[Math]::Min(180,$detail.Length))
          if (!$failures.Contains($message)) { $failures.Add($message) }
        }
      }
    }
  }
  Start-Sleep -Milliseconds 500
  $countsAfter=Count-Targets
  $remaining=($targets | ForEach-Object {$countsAfter[$_]} | Measure-Object -Sum).Sum
  $status=@{
    active=$true
    phase=$(if($remaining -eq 0){'holding_zero'}else{'partial_stop'})
    startedAt=$started
    checkedAt=(Get-Date).ToUniversalTime().ToString('o')
    watchdogPid=$PID
    sweep=$round
    before=$countsBefore
    remaining=$countsAfter
    remainingTotal=$remaining
    accessDeniedPids=@($deniedUntil.Keys | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    excludedQuotaMonitorCount=([Math]::Max(0,@(Get-Process -Name 'codex' -ErrorAction SilentlyContinue).Count-$countsAfter['codex']))
    dashboardPreserved=$true
    errorCount=$failures.Count
    recentErrors=@($failures | Select-Object -Last 5)
    notes='Emergency targets Codex frontends and shared backends; only the verified isolated read-only quota monitor is exempt. Windows Codex Sandbox Service is preserved.'
  }
  try {Save-Json $StatusPath $status} catch {}
  $summary=($targets | ForEach-Object { "$_=$($countsAfter[$_])" }) -join ','
  if ($round -le 3 -or $summary -ne $lastAuditSummary -or $round % 8 -eq 0) {
    Event 'sweep' @{ round=$round; before=$totalBefore; remaining=$remaining; errors=$failures.Count }
  }
  $lastAuditSummary=$summary
  if (!(Is-Latched)) {break}
  Start-Sleep -Seconds ([Math]::Max(5,[Math]::Min(30,$CheckIntervalSeconds)))
} while($true)
$stopped=@{active=$false;phase='disarmed';checkedAt=(Get-Date).ToUniversalTime().ToString('o');dashboardPreserved=$true}
try {Save-Json $StatusPath $stopped} catch {}
Event 'watchdog_disarmed' @{pid=$PID; rounds=$round}
} finally {
 if ($ownsWatchdog) { $watchdogMutex.ReleaseMutex() }
 $watchdogMutex.Dispose()
}
