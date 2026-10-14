#requires -Version 7.0
<#
Run in C:\Tools\Dev\MJobs with:
  pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -Preview
  pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1
  pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -Status
The campaign is synchronous: closing this terminal stops the launcher.
#>
[CmdletBinding()]
param(
    [switch]$Preview,
    [switch]$Status,
    [switch]$NewCampaign,
    [ValidateRange(1,8)][int]$Iterations = 4,
    [ValidatePattern('^[A-Za-z0-9._-]+$')][string]$Model = 'gpt-6.1-sol',
    [ValidateSet('medium','high','xhigh')][string]$Reasoning = 'high',
    [ValidateRange(1,6)][int]$AgentThreads = 4
)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
function Invoke-External([string]$Program,[string[]]$Arguments) {
    $output = & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE): $($Arguments -join ' ')" }
    return $output
}
$realRoot = [string]((Invoke-External 'git' @('-C',$repo,'rev-parse','--show-toplevel')) -join '').Trim()
if ([IO.Path]::GetFullPath($realRoot).TrimEnd('\') -ine [IO.Path]::GetFullPath($repo).TrimEnd('\')) {
    throw "Run the launcher from MJobs' original checkout. Found $realRoot"
}
$commonDir = [string]((Invoke-External 'git' @('-C',$repo,'rev-parse','--path-format=absolute','--git-common-dir')) -join '').Trim()
$stateRoot = Join-Path $commonDir 'mjobs-p0p1-round3'
$latest = Join-Path $stateRoot 'latest.json'
if ($Status) {
    if (Test-Path $latest) {
        Get-Content $latest -Raw
        $record = Get-Content $latest -Raw | ConvertFrom-Json
        if (Test-Path $record.runDir) {
            Get-ChildItem $record.runDir -Filter 'result-*.json' | Sort-Object Name | Select-Object -Last 6 Name,Length,LastWriteTime
        }
    } else { 'No Round 3 campaign checkpoint exists yet.' }
    return
}
foreach ($program in @('gh','codex','git')) {
    if (-not (Get-Command $program -ErrorAction SilentlyContinue)) { throw "Required command not found: $program" }
}
function Read-Board {
    $text = Invoke-External 'gh' @('project','item-list','1','--owner','MulattoTech','--limit','1000','--format','json')
    $json = ($text -join [Environment]::NewLine) | ConvertFrom-Json
    $all = @($json.items)
    if (-not $all.Count -or ($null -ne $json.totalCount -and $all.Count -lt [int]$json.totalCount)) {
        throw 'Project #1 returned an empty or incomplete queue. Refusing to dispatch.'
    }
    $eligible = foreach ($item in $all) {
        if ($item.content.repository -ne 'MulattoTech/MJobs' -or $item.content.type -ne 'Issue') { continue }
        if ($item.priority -cnotin @('P0','P1') -or $item.status -eq 'Done' -or $item.acceptance -eq 'PASS') { continue }
        $labels = @($item.labels | Where-Object {$_ -like 'priority:*'})
        if ($labels.Count -ne 1 -or $labels[0] -cne "priority:$($item.priority)") {
            throw "Project priority/label drift on #$($item.content.number)"
        }
        [pscustomobject]@{
            issue = [int]$item.content.number
            priority = [string]$item.priority
            acceptance = [string]$item.acceptance
            status = [string]$item.status
            area = [string]$item.area
            category = [string]$item.category
            title = [string]$item.title
        }
    }
    return @($eligible | Sort-Object priority,issue -Unique)
}
function Read-MainAcceptance {
    $source = Invoke-External 'git' @('-C',$repo,'show','origin/main:planning/CAPABILITY_MATRIX.json')
    $matrix = ($source -join [Environment]::NewLine) | ConvertFrom-Json
    $original = @($matrix.requirements | Where-Object { $_.priority -in @('P0','P1') -and $_.result -eq 'PASS' })
    $byPriority = @{}
    foreach ($priority in @('P0','P1')) {
        $byPriority[$priority] = @($original | Where-Object priority -eq $priority).Count
    }
    return $byPriority
}
$queue = @(Read-Board)
$p0 = @($queue | Where-Object priority -eq 'P0').Count
$p1 = @($queue | Where-Object priority -eq 'P1').Count
$openPRs = @(Invoke-External 'gh' @('pr','list','--repo','MulattoTech/MJobs','--state','open','--limit','100','--json','number,title,headRefName') | ConvertFrom-Json)
Write-Host "Project #1: $p0 incomplete P0 and $p1 incomplete P1; $($openPRs.Count) open PRs. P2-P6 excluded."
if ($Preview) {
    $baseline = Read-MainAcceptance
    Write-Host "Current original requirement PASS: P0=$($baseline.P0)/26; P1=$($baseline.P1)/116."
    Write-Host 'No mutations. Candidate queue preview (not an automatic work assignment):'
    $queue | Select-Object -First 20 issue,priority,acceptance,title | Format-Table -AutoSize
    $openPRs | Select-Object number,headRefName | Format-Table -AutoSize
    return
}
if ($queue.Count -eq 0) { Write-Host 'No incomplete P0/P1 cards: nothing to launch.'; return }
New-Item -ItemType Directory -Force $stateRoot | Out-Null
$lock = $null
try { $lock = [IO.File]::Open((Join-Path $stateRoot 'owner.lock'),'OpenOrCreate','ReadWrite','None') }
catch { throw 'Another Round 3 owner may be active. Never run overlapping orchestrators.' }
try {
    if (-not $NewCampaign -and (Test-Path $latest)) {
        $current = Get-Content $latest -Raw | ConvertFrom-Json
        $worktree = [string]$current.worktree
        $campaign = [string]$current.campaign
        $branch = [string]((Invoke-External git @('-C',$worktree,'branch','--show-current')) -join '').Trim()
        if (-not (Test-Path (Join-Path $worktree '.git'))) { throw 'Saved clone has no independent .git directory.' }
        if ($current.status -in @('BLOCKED','ERROR')) { throw 'Previous Round3 run is blocked; inspect checkpoint instead of repeating unchanged work.' }
        if ($branch -ne [string]$current.branch -or $branch -notlike 'codex/*') {
            throw 'Saved campaign branch changed; refuse automatic mutation.'
        }
        $runDir = [string]$current.runDir
        $iteration = [int]$current.iteration
        Write-Host "Resuming saved campaign $campaign."
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $campaign = "round3-$stamp"
        $branch = "codex/p0p1-round3-$stamp"
        # A fully independent clone puts .git inside the Codex writable root.
        # Do not link metadata to the blocked Round2 worktree.
        $worktree = Join-Path (Split-Path -Parent $repo) "MJobs-Round3-$stamp"
        $runDir = Join-Path $stateRoot $campaign
        if (Test-Path $worktree) { throw "Clone destination already exists: $worktree" }
        Invoke-External git @('clone','--single-branch','--branch','main','https://github.com/MulattoTech/MJobs.git',$worktree) | Out-Null
        Invoke-External git @('-C',$worktree,'switch','-c',$branch) | Out-Null
        $iteration = 0
    }
    New-Item -ItemType Directory -Force $runDir | Out-Null
    $schemaPath = Join-Path $runDir 'completion.schema.json'
    $schema = @{
        type = 'object'
        additionalProperties = $false
        required = @('status','summary','issueIds','prs','completeOriginalRequirements','nextAction')
        properties = @{
            status = @{type='string';enum=@('CONTINUE','BLOCKED','COMPLETE')}
            summary = @{type='string'}
            issueIds = @{type='array';items=@{type='integer'}}
            prs = @{type='array';items=@{type='string'}}
            completeOriginalRequirements = @{type='array';items=@{type='string'}}
            nextAction = @{type='string'}
        }
    } | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($schemaPath,$schema,[Text.UTF8Encoding]::new($false))
    $noProgress = 0
    for($round=1; $round -le $Iterations; $round++) {
        $iteration++
        Invoke-External git @('-C',$repo,'fetch','origin','main') | Out-Null
        $before = Read-MainAcceptance
        $mainBefore = [string]((Invoke-External git @('-C',$repo,'rev-parse','origin/main')) -join '').Trim()
        $queue = @(Read-Board)
        if (-not $queue.Count) { Write-Host 'No incomplete P0/P1 cards remaining.'; break }
        $queueFile = Join-Path $runDir ("queue-{0:000}.json" -f $iteration)
        [IO.File]::WriteAllText($queueFile,($queue | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
        $directiveFile = Join-Path $runDir ("directive-{0:000}.md" -f $iteration)
        $directive = @"
# MJobs original P0/P1 completion campaign, round $iteration

Project work queue: $queueFile
Current coding checkout: $worktree
Baseline origin/main SHA: $mainBefore
Original PASS baseline: P0=$($before.P0), P1=$($before.P1).
Previous campaign checkpoint: $commonDir\mjobs-p0p1-orchestrator\latest.json
Previous worktree: $repo\.worktrees\p0p1-orchestrator-20261013-232152

You are one integration owner with at most $AgentThreads agent threads, where up to 3 agents may write only in non-overlapping owned worktrees. Enforce all AGENTS.md, code safety, ownership and branch protections. This prompt authorizes code implementation and controlled Project/GitHub changes required for selected P0/P1 work.

PRIORITY #1 (RECONCILE, NOT REBUILD): GitHub already merged PRs #651, #652, and #653. MJ-REQ-277 is PASS and issue #278 is closed. Do not reopen, duplicate or reintegrate them. Round 2's linked worktree contains a pending local merge/closeout commit blocked by index.lock; it is off limits. This independent clone starts from fresh GitHub main and uses its OWN writable .git directory. Verify that these accepted results are present. Review the current Project board, ownership, required CI and actual PR queue policy; do not close unrelated Dependabot PRs or bypass a genuine PR cap. Spend implementation effort on genuinely unfinished original P0/P1 requirements, not historical publication bookkeeping.

PRIORITY #2 (ACTUALLY COMPLETE ORIGINAL REQUIREMENTS): Choose ONE or TWO achievable original incomplete P0/P1 MJ-REQ requirements to bring fully to PASS, prioritizing a meaningful P0 and P1. Prioritize an achievable P0 completion plus an achievable P1 completion where evidence supports it. Read the original issue acceptance contract and its canonical requirement matrix note; make a precise checklist of every missing criterion. Before writing new code, determine whether the gap is actual behavior, missing integration, missing proof, or external impossibility. Avoid huge umbrella P0 planning/reporting directives that lack independent user-observable completion.

Implement all reasonably achievable required behavior for the selected original requirement: UI/API/domain/persistence/worker/browser/restart as applicable, including failures and isolation. Run criterion-level tests and authentic end-to-end proofs within allowed synthetic/local environments. Complete user-visible capabilities rather than a series of tiny defensive patches. Do not switch parent requirements after one incremental fix merely to increase PR count; continue working on the selected parent until fully PASS or a documented external dependency makes full PASS impossible.

Use GitHub Project #1 as the ONLY source for selecting new work. Only its incomplete P0/P1 cards listed in the queue are eligible; cross-check one matching issue priority:P0 or priority:P1 label and real issue state. P2-P6 tasks, reclassification, lower-priority independent work, fake P0/P1 child issue inflation and unrelated housekeeping work are FORBIDDEN. Small supporting changes may be necessary for the selected original P0/P1 contract. Do not touch claimed paths owned by other agents or use another running AFK orchestrator.

For every deliverable: focused tests, affected PostgreSQL/Redis/Chromium/Compose checks when applicable, code/provenance review, short-lived PR, required exact-head hosted checks and integration. Only update canonical original matrix status to PASS with full acceptance evidence and regenerate the requirement index/dashboard; never promote from source commits or green CI alone. A child P0 issue may PASS while its parent stays PARTIAL.

No real job application submissions, paid AI/API services, secret access/extraction, host config changes, reset/clean of another worktree, force push or approval/security bypass. Stop on genuine missing permissions or a repeated unchanged failure; otherwise deliver real code.

Return JSON matching the requested schema. issueIds contains only P0/P1 issues that you actually worked on from the supplied queue. completeOriginalRequirements contains only fully verified MJ-REQ/MJ-REF identifiers, never speculative upgrades. When safe meaningful P0/P1 work remains return CONTINUE with nextAction. A plan-only response is insufficient.
"@
        [IO.File]::WriteAllText($directiveFile,$directive,[Text.UTF8Encoding]::new($false))
        $resultFile = Join-Path $runDir ("result-{0:000}.json" -f $iteration)
        $log = Join-Path $runDir ("codex-{0:000}.jsonl" -f $iteration)
        $stampInfo = [ordered]@{campaign=$campaign;branch=$branch;worktree=$worktree;runDir=$runDir;iteration=$iteration;status='RUNNING';updatedUtc=(Get-Date).ToUniversalTime().ToString('o')}
        $stampInfo|ConvertTo-Json|Set-Content -LiteralPath $latest -Encoding utf8
        $short = "Read $directiveFile and $queueFile. Execute the scoped MJobs P0/P1 implementation and closeout campaign now. Return the required JSON report."
        $arguments = @('exec','-C',$worktree,
            '-s','workspace-write','-m',$Model,
            '-c',"model_reasoning_effort=`"$Reasoning`"",
            '-c','approval_policy="never"',
            '-c',"agents.max_concurrent_threads_per_session=$AgentThreads",
            '-c','agents.default_subagent_model="gpt-6-luna"',
            '-c','agents.default_subagent_reasoning_effort="high"',
            '--enable','multi_agent','--json',
            '--output-schema',$schemaPath,'--output-last-message',$resultFile,
            $short)
        Write-Host "=== Round 3 iteration $iteration | P0 PASS $($before.P0), P1 PASS $($before.P1) ==="
        Write-Host "Saved full Codex event log: $log"
        Write-Host "Primary model: $Model | reasoning: $Reasoning | subagent default: gpt-6-luna high | agent limit: $AgentThreads"
        $startedAt = Get-Date
        $cmdCount = 0
        $fileCount = 0
        & codex @arguments 2>&1 | Tee-Object -FilePath $log | ForEach-Object {
            $event = $null
            try { $event = ([string]$_) | ConvertFrom-Json -ErrorAction Stop } catch { return }
            $kind = [string]$event.item.type
            if ($event.type -eq 'item.started' -and $kind -eq 'command_execution') {
                $cmdCount++
                if ($cmdCount -le 5 -or $cmdCount % 8 -eq 0) {
                    $commandText = [string]$event.item.command
                    Write-Host ("CMD #{0}: {1}" -f $cmdCount,$commandText.Substring(0,[Math]::Min(145,$commandText.Length))) -ForegroundColor DarkGray
                }
            } elseif ($event.type -eq 'item.completed' -and $kind -eq 'file_change') {
                foreach($change in @($event.item.changes)) {
                    if ($null -ne $change) {
                        $fileCount++
                        Write-Host ("EDIT #{0}: {1} {2}" -f $fileCount,$change.kind,$change.path) -ForegroundColor Magenta
                    }
                }
            } elseif ($event.type -eq 'item.completed' -and $kind -eq 'agent_message') {
                $message = ([string]$event.item.text -replace '[\r\n]+',' ')
                if ($message.Length -gt 320) { $message = $message.Substring(0,320) + '...' }
                Write-Host "AGENT: $message" -ForegroundColor Cyan
            } elseif ($event.type -eq 'turn.completed') {
                $usage = $event.usage
                $elapsed = [Math]::Max(0.1,((Get-Date) - $startedAt).TotalMinutes)
                Write-Host ("TOKENS input={0:N0}, cached={1:N0}, output={2:N0}, reasoning={3:N0}; throughput {4:N0} total tokens/min (mostly cached)" -f $usage.input_tokens,$usage.cached_input_tokens,$usage.output_tokens,$usage.reasoning_output_tokens,(($usage.input_tokens + $usage.output_tokens)/$elapsed)) -ForegroundColor Green
                Write-Host 'NOTE: Codex exec JSONL does not include account quota percentages. Use interactive Codex /status for account quotas.'
            } elseif ($event.type -eq 'error' -or $event.type -eq 'turn.failed') {
                Write-Warning "Codex error event; inspect $log"
            }
        }
        $code = $LASTEXITCODE
        if ($code -ne 0 -or -not (Test-Path $resultFile)) {
            $stampInfo.status='ERROR'
            $stampInfo|ConvertTo-Json|Set-Content -LiteralPath $latest -Encoding utf8
            throw "Codex failed at iteration $iteration (exit $code). Full log: $log. Worktrees preserved."
        }
        try { $report = Get-Content $resultFile -Raw | ConvertFrom-Json }
        catch { throw "Codex report is not valid JSON: $resultFile" }
        $allowed = @($queue | ForEach-Object { [int]$_.issue })
        foreach($number in @($report.issueIds)) {
            if ([int]$number -notin $allowed) { throw "Codex reported out-of-scope issue #$number; halting immediately." }
        }
        Invoke-External git @('-C',$repo,'fetch','origin','main') | Out-Null
        $after = Read-MainAcceptance
        $mainAfter = [string]((Invoke-External git @('-C',$repo,'rev-parse','origin/main')) -join '').Trim()
        Write-Host ("Original P0 PASS: {0} -> {1}; P1 PASS: {2} -> {3}" -f $before.P0,$after.P0,$before.P1,$after.P1)
        $changed = ($mainBefore -ne $mainAfter) -or ($after.P0 -gt $before.P0) -or ($after.P1 -gt $before.P1)
        if ($changed) { $noProgress=0 } else { $noProgress++ }
        $stampInfo.status=[string]$report.status
        $stampInfo.branch = [string]((Invoke-External git @('-C',$worktree,'branch','--show-current')) -join '').Trim()
        $stampInfo.updatedUtc=(Get-Date).ToUniversalTime().ToString('o')
        $stampInfo|ConvertTo-Json|Set-Content -LiteralPath $latest -Encoding utf8
        Write-Host ("Codex: {0} | {1}" -f $report.status,$report.summary)
        if ($report.status -in @('BLOCKED','COMPLETE')) { break }
        if ($noProgress -ge 2) {
            Write-Warning 'Two rounds without any new main commit or original PASS; pause and review before burning more usage.'
            break
        }
    }
    Write-Host "Campaign checkpoint: $latest"
    Write-Host "Full logs and directives: $runDir"
} finally {
    $lock.Dispose()
}
