#requires -Version 7.0
<# Resumes a stopped *root* MJobs Round3 Codex TUI; never creates a second sprint.
   -AutoReview (default): sandboxed native automatic approval review
   -Yolo: explicit no-sandbox/no-approval mode, refused under Administrator elevation.
   -ManualApprovals: restore normal y/p prompts.
#>
[CmdletBinding()]
param(
    [switch]$Yolo,
    [switch]$ManualApprovals,
    [switch]$StatusOnly,
    [switch]$Preview,
    [ValidatePattern('^[a-zA-Z0-9._-]+$')][string]$Model = 'gpt-6.1-sol',
    [ValidateSet('medium','high','xhigh')][string]$Reasoning = 'high',
    [ValidateRange(1,6)][int]$AgentThreads = 4
)
$ErrorActionPreference = 'Stop'
if($Yolo -and $ManualApprovals) {throw 'Choose Yolo OR ManualApprovals.'}
$root = $PSScriptRoot
$latest = Join-Path $root '.git\mjobs-p0p1-round3\latest.json'
if(-not(Test-Path -LiteralPath $latest)) { throw 'No Round 3 checkpoint exists.' }
$state=Get-Content -LiteralPath $latest -Raw | ConvertFrom-Json
$workspace = (Resolve-Path -LiteralPath ([string]$state.worktree)).Path
$allowed = [IO.Path]::GetFullPath((Join-Path $root '.mjobs-recovery\active')).TrimEnd('\')+'\'
if(-not $workspace.StartsWith($allowed,[StringComparison]::OrdinalIgnoreCase)) {
    throw 'Saved sprint workspace is outside the MJobs archive; refusing to resume.'
}
if(-not(Test-Path -LiteralPath (Join-Path $workspace '.git') -PathType Container)) {
    throw 'Saved checkout is not an independent MJobs clone.'
}
$actualBranch=& git -C $workspace branch --show-current
if($LASTEXITCODE -ne 0 -or $actualBranch -notlike 'codex/p0p1-round3-*') {
    throw 'Unexpected branch in the saved clone; refusing to resume.'
}
$busy=@(Get-CimInstance Win32_Process -Filter "Name='codex.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine.Replace('/','\').Contains($workspace)
})
if($busy.Count -gt 0) {
    Write-Host "Existing Codex is still active ($($busy.ProcessId -join ', ')); do not start another integration owner."
    if($StatusOnly){ return }
    if(-not $Preview){ throw 'Exit or checkpoint the existing native Codex session before resuming it.' }
}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
$admin=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if($Yolo -and $admin -and -not $Preview) {
    throw 'YOLO_BLOCKED_ELEVATED: Close elevated VS Code after checkpointing Codex, reopen MJobs normally from your desktop shortcut (not Run as administrator), then retry.'
}
$lookup=@'
import json,pathlib,sys
cwd=pathlib.Path(sys.argv[1])
target=str(cwd).replace('\\','/').rstrip('/').casefold()
root=pathlib.Path.home()/'.codex'/'sessions'
recent=sorted(root.glob('*/*/*/rollout-*.jsonl'),key=lambda p:p.stat().st_mtime,reverse=True)
for path in recent[:260]:
 try:
  with path.open(encoding='utf-8-sig',errors='replace') as f:
   for line in f:
    ev=json.loads(line)
    if ev.get('type')!='session_meta':continue
    p=ev.get('payload') or {}
    source=p.get('source')
    if isinstance(source,dict) and source.get('subagent'):break
    if str(p.get('cwd') or '').replace('\\','/').rstrip('/').casefold()==target:
     print(p.get('id') or '')
     sys.exit(0)
    break
 except (OSError,ValueError):continue
sys.exit(2)
'@
$py = Get-Command python -ErrorAction Stop
$sessionId = $lookup | & $py.Source - $workspace
if($LASTEXITCODE -ne 0 -or $sessionId -notmatch '^[0-9a-f-]{36}$') {
    throw 'Could not uniquely locate a root Codex session for this checkout. Nothing was launched.'
}
Write-Host "Campaign: $($state.campaign)"
Write-Host "Workspace: $workspace"
Write-Host "Root session: $sessionId"
Write-Host "Model: $Model ($Reasoning); max agent threads: $AgentThreads"
if($StatusOnly -or $Preview){
    Write-Host "Preview only. No model, Codex session, or Git operation started."
    return
}
$approval = if($Yolo) {
    @('--dangerously-bypass-approvals-and-sandbox')
} elseif($ManualApprovals) {
    @('-s','workspace-write','-a','on-request','-c','approvals_reviewer="user"')
} else {
    @('--approve-for-me','-c','approvals_reviewer="auto_review"')
}
Write-Host "Approval mode: $(if($Yolo){'YOLO, NO SANDBOX'}elseif($ManualApprovals){'Manual'}else{'Automatic review'})"
$args = @('-C',$workspace) + $approval + @(
    '-m',$Model,'-c',"model_reasoning_effort=`"$Reasoning`"",
    '-c',"agents.max_concurrent_threads_per_session=$AgentThreads",
    '-c','agents.default_subagent_model="gpt-6-luna"',
    '-c','agents.default_subagent_reasoning_effort="high"',
    '--enable','multi_agent',
    'resume',$sessionId
)
Push-Location $workspace
try {& codex @args;$exitCode=$LASTEXITCODE}
finally {Pop-Location}
if($exitCode -ne 0){throw "Codex resumed session exited $exitCode; checkout preserved."}
