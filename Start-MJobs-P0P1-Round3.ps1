#requires -Version 7.0
# ROUND3_AUTH_REPAIR_V2 - tested auth preflight, standard approvals and native TUI option.
<#
Run in C:\Tools\Dev\MJobs with:
  pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -Preview
  pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1
  pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -Status
Use -RepairAuth -Preview to reauthorize GitHub, then -Interactive for native Codex UI.
Default mode runs bounded automatic iterations with saved logs. All modes select P0/P1 only.
#>
[CmdletBinding()]
param(
    [switch]$Preview,
    [switch]$RepairAuth,
    [switch]$Interactive,
    [switch]$Yolo,
    [switch]$ManualApprovals,
    [switch]$PrepareOnly,
    [switch]$Status,
    [switch]$NewCampaign,
    [ValidateRange(1,8)][int]$Iterations = 4,
    [ValidatePattern('^[A-Za-z0-9._-]+$')][string]$Model = 'gpt-6.1-sol',
    [ValidateSet('medium','high','xhigh')][string]$Reasoning = 'high',
    [ValidateRange(1,6)][int]$AgentThreads = 4
)
$ErrorActionPreference = 'Stop'
if ($Yolo -and $ManualApprovals) { throw 'Choose either -Yolo or -ManualApprovals.' }
# YOLO deliberately disables both sandbox and approvals. Never do this in elevated VS Code.
if ($Yolo -and -not $Preview) {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'YOLO_BLOCKED_ELEVATED: Close the elevated VS Code AFTER saving the active sprint checkpoint, then reopen MJobs using the desktop shortcut normally. Do not use Run as administrator. Use -Yolo only from a standard Windows terminal.'
    }
}
# Encoding is process-local; do not change host or user-wide settings.
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$repo = $PSScriptRoot
Import-Module (Join-Path $repo 'tools/orchestration/MJobsRound3.psm1') -Force -DisableNameChecking
if ($Status -and ($Preview -or $RepairAuth -or $Interactive -or $PrepareOnly -or $Yolo -or $ManualApprovals)) { throw 'Use -Status on its own.' }
if ($Interactive -and $PrepareOnly) { throw 'Choose -Interactive or -PrepareOnly.' }
function Invoke-External([string]$Program,[string[]]$Arguments) {
    $response = Invoke-MJobsRead -Program $Program -Arguments $Arguments -TimeoutSeconds 180
    return ([string]$response.StdOut).TrimEnd([char[]]"`r`n")
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
# Never reference deleted/moved MJobs files in generated coding prompts.
# Verify the exact authority files and referenced prompt paths before launching.
function Assert-CampaignAssets([string]$Checkout) {
    $validator = Join-Path $repo 'tools/orchestration/prompt_inventory.py'
    if (-not (Test-Path -LiteralPath $validator)) {
        throw 'MJOBS_PROMPT_PATH_MISSING: The prompt inventory validator is absent. Restore the required tooling from the reviewed branch.'
    }
    $python = Get-Command python -ErrorAction Stop
    $result = & $python.Source $validator --root $Checkout
    if ($LASTEXITCODE -ne 0) {
        throw "MJOBS_PROMPT_PATH_MISSING: Required documents or referenced paths do not exist in $Checkout. Review the printed inventory and update the prompt; never guess an old worktree path."
    }
    return $result
}
foreach ($program in @('gh','codex','git')) {
    if (-not (Get-Command $program -ErrorAction SilentlyContinue)) { throw "Required command not found: $program" }
}
function Read-Board { return @(Get-MJobsQueue) }
function Read-MainAcceptance { return Get-MJobsMainAcceptance }
# Fail before any model call if a referenced MJobs file was moved or deleted.
$null = Assert-CampaignAssets $repo
# Stop before cloning, locking, creating campaign state or invoking Codex when auth is invalid.
$projectAccess = Assert-MJobsGitHubAccess -RepairAuth:$RepairAuth
Write-Host "GitHub authenticated: MulattoTech | Project #1: $($projectAccess.title)"
$queue = @(Read-Board)
$p0 = @($queue | Where-Object priority -eq 'P0').Count
$p1 = @($queue | Where-Object priority -eq 'P1').Count
$openPRs = @(Invoke-External 'gh' @('pr','list','--repo','MulattoTech/MJobs','--state','open','--limit','100','--json','number,title,headRefName') | ConvertFrom-Json)
Write-Host "Project #1: $p0 incomplete P0 and $p1 incomplete P1; $($openPRs.Count) open PRs. P2-P6 excluded."
if ($Preview) {
    $baseline = Read-MainAcceptance
    Write-Host "Current original requirement PASS: P0=$($baseline.P0)/$($baseline.P0Total); P1=$($baseline.P1)/$($baseline.P1Total)."
    Write-Host 'Preflight passed. No clone or Codex model run was started.'
    Write-Host 'Default interactive mode uses automatic approval review; -ManualApprovals restores y/p prompts.'
    Write-Host 'Use -Yolo only in a non-administrator terminal: disables sandbox and approvals for this invocation.'
    $queue | Select-Object -First 20 issue,priority,acceptance,title | Format-Table -AutoSize
    $openPRs | Select-Object number,headRefName | Format-Table -AutoSize
    return
}
if ($queue.Count -eq 0) { Write-Host 'No incomplete P0/P1 cards: nothing to launch.'; return }
New-Item -ItemType Directory -Force $stateRoot | Out-Null
$lock = $null
try { $lock = [IO.File]::Open((Join-Path $commonDir 'mjobs-p0p1-launcher.owner.lock'),'OpenOrCreate','ReadWrite','None') }
catch { throw 'Another Round 3 owner may be active. Never run overlapping orchestrators.' }
try {
    $legacyLocks = @()
    foreach ($relative in @('mjobs-p0p1-orchestrator/owner.lock','mjobs-p0p1-round2/owner.lock','mjobs-p0p1-round3/owner.lock','afk-dev/run.owner.lock')) {
        $oldLock = Join-Path $commonDir $relative
        if (Test-Path -LiteralPath $oldLock) {
            try { $legacyLocks += [IO.File]::Open($oldLock,'Open','ReadWrite','None') }
            catch { throw "Another runner still holds $relative. No processes were stopped." }
        }
    }
    if (-not $NewCampaign -and (Test-Path $latest)) {
        $current = Get-Content $latest -Raw | ConvertFrom-Json
        $worktree = [string]$current.worktree
        $canonicalActive = Join-Path $repo '.mjobs-recovery\active'
        if (-not $worktree.StartsWith($canonicalActive + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {
            throw 'Existing Round 3 campaign lives outside MJobs. Finish the running interactive session, then use -NewCampaign to start a fresh campaign inside C:\Tools\Dev\MJobs\.mjobs-recovery\active.'
        }
        $campaign = [string]$current.campaign
        $branch = [string]((Invoke-External git @('-C',$worktree,'branch','--show-current')) -join '').Trim()
        if (-not (Test-Path (Join-Path $worktree '.git') -PathType Container)) { throw 'Saved clone has no independent .git directory.' }
        if ($current.status -in @('BLOCKED','ERROR')) { throw 'Previous Round3 run is blocked; inspect checkpoint instead of repeating unchanged work.' }
        if ($branch -ne [string]$current.branch -or $branch -notlike 'codex/*') {
            throw 'Saved campaign branch changed; refuse automatic mutation.'
        }
        $runDir = [string]$current.runDir
        $iteration = [int]$current.iteration
        Write-Host "Resuming saved campaign $campaign."
    } else {
        # Discover ANY live Round 3 Codex from the actual canonical active
        # directory. A stale hard-coded historical path must never be used.
        $currentActive = Join-Path $repo '.mjobs-recovery\active'
        $otherRound3 = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq 'codex.exe' -and $_.CommandLine -and
            $_.CommandLine -like "*$currentActive\MJobs-Round3-*"
        })
        if ($otherRound3.Count -gt 0) {
            throw 'Another P0/P1 Round 3 Codex integration owner is still running inside MJobs. Preserve that session; do not start a conflicting campaign.'
        }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $campaign = "round3-$stamp"
        $branch = "codex/p0p1-round3-$stamp"
        # A fully independent clone puts .git inside the Codex writable root.
        # Do not link metadata to the blocked Round2 worktree.
        $workspaceParent = Join-Path $repo '.mjobs-recovery\active'
        New-Item -Path $workspaceParent -ItemType Directory -Force | Out-Null
        $worktree = Join-Path $workspaceParent "MJobs-Round3-$stamp"
        $runDir = Join-Path (Join-Path $worktree '.mjobs-run') $campaign
        if (Test-Path $worktree) { throw "Clone destination already exists: $worktree" }
        Invoke-External git @('clone','--single-branch','--branch','main','https://github.com/MulattoTech/MJobs.git',$worktree) | Out-Null
        Invoke-External git @('-C',$worktree,'switch','-c',$branch) | Out-Null
        $iteration = 0
    }
    # Fresh clone must contain the same existing authority/prompt files.
    $null = Assert-CampaignAssets $worktree
    # Context files live in the working directory, not inside protected .git metadata.
    New-Item -ItemType Directory -Force $runDir | Out-Null
    $excludeFile = Join-Path $worktree '.git/info/exclude'
    if (-not (Select-String -LiteralPath $excludeFile -Pattern '^/\.mjobs-run/$' -Quiet)) {
        [IO.File]::AppendAllText($excludeFile,"`n/.mjobs-run/`n",[Text.UTF8Encoding]::new($false))
    }
    $initialState = [ordered]@{campaign=$campaign;branch=$branch;worktree=$worktree;runDir=$runDir;iteration=$iteration;status='READY';updatedUtc=(Get-Date).ToUniversalTime().ToString('o')}
    Write-MJobsJson $latest $initialState
    if ($PrepareOnly) { Write-Host "Prepared standalone clone: $worktree. No Codex agent started."; return }

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
        $before = Read-MainAcceptance
        $mainBefore = [string]$before.Head
        $queue = @(Read-Board)
        if (-not $queue.Count) { Write-Host 'No incomplete P0/P1 cards remaining.'; break }
        $queueFile = Join-Path $runDir ("queue-{0:000}.json" -f $iteration)
        Write-MJobsJson $queueFile @($queue)
        $directiveFile = Join-Path $runDir ("directive-{0:000}.md" -f $iteration)
        $directive = @"
# MJobs original P0/P1 completion campaign, round $iteration

Project work queue: $queueFile
Current coding checkout: $worktree
Baseline origin/main SHA: $mainBefore
Original PASS baseline: P0=$($before.P0), P1=$($before.P1).
Prior blocked worktrees and credentials are off limits; use this current clone and the live Project board.

You are one integration owner with at most $AgentThreads agent threads, where up to 3 agents may write only in non-overlapping owned worktrees. Enforce all AGENTS.md, code safety, ownership and branch protections. This prompt authorizes code implementation and controlled Project/GitHub changes required for selected P0/P1 work.
PARALLEL FEATURE DELIVERY: When the selected P0 and P1 contracts have genuinely disjoint writable paths and ownership, promptly delegate at least two independent implementation-and-test workstreams to Luna High subagents instead of giving them audit-only work. Each implementation subagent must own a meaningful user-visible feature, start with an executable acceptance check, produce production changes and tests, and return exact evidence for coordinator review. The Sol High root agent is the sole integration and GitHub delivery owner; prioritize coordinating feature acceptance, tests, review, and integration rather than coding every independent feature directly. Never force multiple writers onto shared UI files, migrations, CI, planning matrices or other contested paths: serialize those edits and use read-only reviewers as needed. Never count planned agents as actually spawned. Report root/subagent roles, actual writing vs review contributions, verified accepted requirements, and merged PRs.

PRIORITY #1 (RECONCILE, NOT REBUILD): GitHub already merged PRs #651, #652, and #653. MJ-REQ-277 is PASS and issue #278 is closed. Do not reopen, duplicate or reintegrate them. Round 2's linked worktree contains a pending local merge/closeout commit blocked by index.lock; it is off limits. This independent clone starts from fresh GitHub main and has its OWN .git directory. Codex may still protect Git metadata; request approval through the configured reviewer for legitimate Git operations, and stop if approval is denied. Do not try API or filesystem workarounds to evade a denial. Verify that these accepted results are present. Review the current Project board, ownership, required CI and actual PR queue policy; do not close unrelated Dependabot PRs or bypass a genuine PR cap. Spend implementation effort on genuinely unfinished original P0/P1 requirements, not historical publication bookkeeping.

PRIORITY #2 (ACTUALLY COMPLETE ORIGINAL REQUIREMENTS): Choose ONE or TWO achievable original incomplete P0/P1 MJ-REQ requirements to bring fully to PASS, prioritizing a meaningful P0 and P1. Prioritize an achievable P0 completion plus an achievable P1 completion where evidence supports it. Read the original issue acceptance contract and its canonical requirement matrix note; make a precise checklist of every missing criterion. Before writing new code, determine whether the gap is actual behavior, missing integration, missing proof, or external impossibility. Avoid huge umbrella P0 planning/reporting directives that lack independent user-observable completion.

Implement all reasonably achievable required behavior for the selected original requirement: UI/API/domain/persistence/worker/browser/restart as applicable, including failures and isolation. Run criterion-level tests and authentic end-to-end proofs within allowed synthetic/local environments. Complete user-visible capabilities rather than a series of tiny defensive patches. Do not switch parent requirements after one incremental fix merely to increase PR count; continue working on the selected parent until fully PASS or a documented external dependency makes full PASS impossible.

Use GitHub Project #1 as the ONLY source for selecting new work. Only its incomplete P0/P1 cards listed in the queue are eligible; cross-check one matching issue priority:P0 or priority:P1 label and real issue state. P2-P6 tasks, reclassification, lower-priority independent work, fake P0/P1 child issue inflation and unrelated housekeeping work are FORBIDDEN. Small supporting changes may be necessary for the selected original P0/P1 contract. Do not touch claimed paths owned by other agents or use another running AFK orchestrator.

For every deliverable: focused tests, affected PostgreSQL/Redis/Chromium/Compose checks when applicable, code/provenance review, short-lived PR, required exact-head hosted checks and integration. Only update canonical original matrix status to PASS with full acceptance evidence and regenerate the requirement index/dashboard; never promote from source commits or green CI alone. A child P0 issue may PASS while its parent stays PARTIAL.

No real job application submissions, paid AI/API services, secret access/extraction, host config changes, reset/clean of another worktree, force push or approval/security bypass. Stop on genuine missing permissions or a repeated unchanged failure; otherwise deliver real code.

Return JSON matching the requested schema. issueIds contains only P0/P1 issues that you actually worked on from the supplied queue. completeOriginalRequirements contains only fully verified original MJ-REQ identifiers. Report additive MJ-REF work separately; never use it to inflate original counts. When safe meaningful P0/P1 work remains return CONTINUE with nextAction. A plan-only response is insufficient.
"@
        [IO.File]::WriteAllText($directiveFile,$directive,[Text.UTF8Encoding]::new($false))
        $resultFile = Join-Path $runDir ("result-{0:000}.json" -f $iteration)
        $log = Join-Path $runDir ("codex-{0:000}.jsonl" -f $iteration)
        $stampInfo = [ordered]@{campaign=$campaign;branch=$branch;worktree=$worktree;runDir=$runDir;iteration=$iteration;status='RUNNING';updatedUtc=(Get-Date).ToUniversalTime().ToString('o')}
        Write-MJobsJson $latest $stampInfo
        foreach ($artifact in @($schemaPath,$queueFile,$directiveFile)) {
            if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
                throw "MJOBS_PROMPT_PATH_MISSING: Generated context file does not exist: $artifact"
            }
        }
        $short = "Read $directiveFile and $queueFile. Execute the scoped MJobs P0/P1 implementation and closeout campaign now. Return the required JSON report."
        # Native approval flags, chosen before the process starts.
        # --approve-for-me supplies workspace-write sandbox, and conflicts with -s.
        $approvalArguments = if ($Yolo) {
            @('--dangerously-bypass-approvals-and-sandbox')
        } elseif ($ManualApprovals) {
            @('-s','workspace-write','-a','on-request','-c','approvals_reviewer="user"')
        } else {
            @('--approve-for-me','-c','approvals_reviewer="auto_review"')
        }
        $standardArguments = @(
            '-m',$Model,
            '-c',"model_reasoning_effort=`"$Reasoning`"",
            '-c',"agents.max_concurrent_threads_per_session=$AgentThreads",
            '-c','agents.default_subagent_model="gpt-6-luna"',
            '-c','agents.default_subagent_reasoning_effort="high"',
            '--enable','multi_agent'
        )
        $arguments = @('exec','-C',$worktree) + $approvalArguments + $standardArguments + @(
            '--json','--output-schema',$schemaPath,
            '--output-last-message',$resultFile,$short
        )
        if ($Interactive) {
            $tuiPrompt = $short + ' Interactive mode: execute normally and give a readable handoff rather than a JSON-only final response.'
            $tuiArguments = @('-C',$worktree) + $approvalArguments + $standardArguments + @($tuiPrompt)
            Write-Host ("Effective Codex approvals: " + $(if($Yolo){'YOLO (NO SANDBOX)'}elseif($ManualApprovals){'manual on-request'}else{'automatic reviewed'}))
            Write-Host 'Opening native Codex TUI. /status and agent views are provided by Codex itself.'
            Write-Host 'Exiting this TUI returns to PowerShell; it does not silently launch another session.'
            Push-Location $worktree
            try { & codex @tuiArguments; $tuiExit=$LASTEXITCODE } finally { Pop-Location }
            $stampInfo.status = if ($tuiExit -eq 0) { 'INTERACTIVE_EXIT' } else { 'ERROR' }
            $stampInfo.branch = Invoke-External git @('-C',$worktree,'branch','--show-current')
            $stampInfo.updatedUtc=(Get-Date).ToUniversalTime().ToString('o')
            Write-MJobsJson $latest $stampInfo
            if ($tuiExit -ne 0) { throw "Interactive Codex exited $tuiExit; checkout preserved." }
            break
        }
        Write-Host "=== Round 3 iteration $iteration | P0 PASS $($before.P0), P1 PASS $($before.P1) ==="
        Write-Host "Saved full Codex event log: $log"
        Write-Host "Primary model: $Model | reasoning: $Reasoning | subagent default: gpt-6-luna high | agent limit: $AgentThreads"
        $startedAt = Get-Date
        $cmdCount = 0
        $fileCount = 0
        # Explicit EOF prevents codex exec waiting for inherited redirected stdin.
        '' | & codex @arguments 2>&1 | Tee-Object -FilePath $log | ForEach-Object {
            $event = $null
            try { $event = ([string]$_) | ConvertFrom-Json -ErrorAction Stop } catch {
                $plain = Protect-MJobsText ([string]$_)
                if ($plain) { Write-Host $plain }
                return
            }
            $kind = [string]$event.item.type
            if ($event.type -eq 'item.started' -and $kind -eq 'command_execution') {
                $cmdCount++
                if ($cmdCount -le 5 -or $cmdCount % 8 -eq 0) {
                    $commandText = Protect-MJobsText ([string]$event.item.command)
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
                $message = Protect-MJobsText ([string]$event.item.text -replace '[\r\n]+',' ')
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
            Write-MJobsJson $latest $stampInfo
            throw "Codex failed at iteration $iteration (exit $code). Full log: $log. Worktrees preserved."
        }
        try { $report = Get-Content $resultFile -Raw | ConvertFrom-Json }
        catch { throw "Codex report is not valid JSON: $resultFile" }
        $allowed = @($queue | ForEach-Object { [int]$_.issue })
        foreach($number in @($report.issueIds)) {
            if ([int]$number -notin $allowed) { throw "Codex reported out-of-scope issue #$number; halting immediately." }
        }
        $after = Read-MainAcceptance
        $mainAfter = [string]$after.Head
        Write-Host ("Original P0 PASS: {0} -> {1}; P1 PASS: {2} -> {3}" -f $before.P0,$after.P0,$before.P1,$after.P1)
        foreach ($claimed in @($report.completeOriginalRequirements)) {
            if ($claimed -notin $after.PassIds) { throw "MJOBS_UNVERIFIED_COMPLETION: $claimed is not PASS on current published main." }
        }
        if ($report.status -eq 'COMPLETE' -and ($after.P0 -lt $after.P0Total -or $after.P1 -lt $after.P1Total)) {
            throw 'MJOBS_FALSE_COMPLETE: Original P0/P1 requirements remain incomplete; preserving checkpoint for review.'
        }
        $changed = ($after.P0 -gt $before.P0) -or ($after.P1 -gt $before.P1)
        if ($changed) { $noProgress=0 } else { $noProgress++ }
        $stampInfo.status=[string]$report.status
        $stampInfo.branch = [string]((Invoke-External git @('-C',$worktree,'branch','--show-current')) -join '').Trim()
        $stampInfo.updatedUtc=(Get-Date).ToUniversalTime().ToString('o')
        Write-MJobsJson $latest $stampInfo
        Write-Host ("Codex: {0} | {1}" -f $report.status,$report.summary)
        if ($report.status -in @('BLOCKED','COMPLETE')) { break }
        if ($noProgress -ge 2) {
            Write-Warning 'Two rounds without an original P0/P1 PASS increase; pause and review before further usage.'
            break
        }
    }
    Write-Host "Campaign checkpoint: $latest"
    Write-Host "Full logs and directives: $runDir"
} catch {
    $failure = $_
    if ($stampInfo) {
        $stampInfo.status='ERROR'
        $stampInfo.updatedUtc=(Get-Date).ToUniversalTime().ToString('o')
        $detail=Protect-MJobsText $failure.Exception.Message
        $stampInfo.error=$detail.Substring(0,[Math]::Min(500,$detail.Length))
        try { Write-MJobsJson $latest $stampInfo } catch { Write-Warning 'Could not update the local failure checkpoint.' }
    }
    throw $failure
} finally {
    foreach ($held in $legacyLocks) { $held.Dispose() }
    $lock.Dispose()
}
