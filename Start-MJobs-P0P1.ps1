#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Preview,
    [switch]$Status,
    [switch]$NewCampaign,
    [ValidateRange(1,40)][int]$Iterations = 12,
    [ValidatePattern('^[a-zA-Z0-9._-]+$')][string]$Model = 'gpt-6.1-sol',
    [ValidateSet('medium','high','xhigh')][string]$Reasoning = 'high',
    [ValidateRange(1,6)][int]$Agents = 6
)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
function Invoke-Checked([string]$Name, [string[]]$Arguments) {
    $value = & $Name @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with code $LASTEXITCODE" }
    return $value
}
$root = (Invoke-Checked git @('-C',$repo,'rev-parse','--show-toplevel')).Trim()
if ([IO.Path]::GetFullPath($root).TrimEnd('\') -ine [IO.Path]::GetFullPath($repo).TrimEnd('\')) {
    throw 'Run this launcher from the canonical MJobs repository root, not a linked worktree.'
}
$common = (Invoke-Checked git @('-C',$repo,'rev-parse','--path-format=absolute','--git-common-dir')).Trim()
$stateDir = Join-Path $common 'mjobs-p0p1-orchestrator'
$latestPath = Join-Path $stateDir 'latest.json'
if ($Status) {
    if (Test-Path -LiteralPath $latestPath) { Get-Content -LiteralPath $latestPath -Raw } 
    else { Write-Output 'No campaign started by this launcher yet.' }
    return
}
foreach ($tool in @('gh','git','codex')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Required command missing: $tool" }
}
function Read-EligibleBoard {
    $raw = Invoke-Checked gh @('project','item-list','1','--owner','MulattoTech','--limit','1000','--format','json')
    $data = ($raw -join [Environment]::NewLine) | ConvertFrom-Json
    $all = @($data.items)
    if ($all.Count -eq 0 -or ($data.PSObject.Properties.Name -contains 'totalCount' -and $all.Count -lt [int]$data.totalCount)) {
        throw 'GitHub Project returned an empty or truncated item set. Refusing to select work.'
    }
    $rows = foreach ($item in $all) {
        if ($item.content.repository -ne 'MulattoTech/MJobs') { continue }
        if ($item.priority -cnotin @('P0','P1')) { continue }
        if ($item.status -eq 'Done' -or $item.acceptance -eq 'PASS') { continue }
        $priorityLabels = @($item.labels | Where-Object { $_ -like 'priority:*' })
        if ($priorityLabels.Count -ne 1 -or $priorityLabels[0] -cne "priority:$($item.priority)") {
            throw "Priority mismatch for Project item #$($item.content.number)"
        }
        [pscustomobject]@{
            issue = [int]$item.content.number
            priority = [string]$item.priority
            title = [string]$item.title
            acceptance = [string]$item.acceptance
            area = [string]$item.area
            category = [string]$item.category
            status = [string]$item.status
        }
    }
    return @($rows | Sort-Object priority,issue -Unique)
}
$queue = @(Read-EligibleBoard)
$p0 = @($queue | Where-Object priority -eq 'P0').Count
$p1 = @($queue | Where-Object priority -eq 'P1').Count
Write-Host "Live Project #1 eligible, unfinished queue: P0=$p0; P1=$p1; total=$($queue.Count). P2-P6 excluded."
if ($Preview) {
    $queue | Select-Object -First 12 | Format-Table issue,priority,acceptance,title -AutoSize
    Write-Host "PREVIEW ONLY: No worktrees created, no Codex invoked."
    return
}
if (-not $queue.Count) { Write-Host 'No unfinished P0/P1 Project cards; nothing to run.'; return }
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$lockStream = $null
try {
    $lockStream = [IO.File]::Open((Join-Path $stateDir 'owner.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
} catch { throw 'Another MJobs P0/P1 orchestrator may be running. Check its process before retrying.' }
try {
    $latest = $null
    if (-not $NewCampaign -and (Test-Path -LiteralPath $latestPath)) {
        $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json
    }
    if ($latest) {
        $workspace = [string]$latest.workspace
        $campaign = [string]$latest.campaign
        if (-not (Test-Path -LiteralPath $workspace)) { throw "Saved worktree missing: $workspace. Investigate before -NewCampaign." }
        $branch = (Invoke-Checked git @('-C',$workspace,'branch','--show-current')).Trim()
        if ($branch -notlike 'codex/p0p1-only-*') { throw "Saved worktree branch is unexpected: $branch" }
        Write-Host "Resuming existing P0/P1 campaign $campaign."
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $campaign = "p0p1-$stamp"
        $branch = "codex/p0p1-only-$stamp"
        $workspace = Join-Path (Join-Path $repo '.worktrees') "p0p1-orchestrator-$stamp"
        Invoke-Checked git @('-C',$repo,'fetch','origin','main') | Out-Null
        if (Test-Path -LiteralPath $workspace) { throw "Worktree already exists: $workspace" }
        Invoke-Checked git @('-C',$repo,'worktree','add','-b',$branch,$workspace,'origin/main') | Out-Null
    }
    $runDir = Join-Path $stateDir $campaign
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $schemaPath = Join-Path $runDir 'result.schema.json'
    $schema = @{
        type = 'object'
        additionalProperties = $false
        required = @('status','summary','issueIds','pullRequests','nextAction')
        properties = @{
            status = @{type='string';enum=@('CONTINUE','BLOCKED','COMPLETE')}
            summary = @{type='string'}
            issueIds = @{type='array';items=@{type='integer'}}
            pullRequests = @{type='array';items=@{type='string'}}
            nextAction = @{type='string'}
        }
    } | ConvertTo-Json -Depth 7
    Set-Content -LiteralPath $schemaPath -Value $schema -Encoding utf8
    $attempt = if ($latest) { [int]$latest.iteration } else { 0 }
    $stagnant = if ($latest) { [int]$latest.stagnant } else { 0 }
    for ($round = 1; $round -le $Iterations; $round++) {
        $queue = @(Read-EligibleBoard)
        if ($queue.Count -eq 0) { Write-Host 'P0/P1 queue has no unfinished cards.'; break }
        $attempt++
        $before = (Invoke-Checked git @('-C',$workspace,'rev-parse','HEAD')).Trim()
        $queuePath = Join-Path $runDir "queue-$attempt.json"
        $queue | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $queuePath -Encoding utf8
        $instructionPath = Join-Path $runDir "directive-$attempt.md"
        $directive = @"
# MJobs P0/P1-only authorized implementation iteration $attempt
Current worktree: $workspace
GitHub Project queue snapshot: $queuePath

You are the single integration owner of MJobs for this campaign. This is an authorized implementation turn.
Read AGENTS.md, the MJobs delivery skill, concise handoff and the Project snapshot above. Independently verify live Project #1 and issue labels before claiming work. ONLY select open incomplete P0 or P1 Project cards present in the snapshot. P2-P6 are forbidden as selectable issues, PR objectives or new card priorities. Do not silently reclassify any issue; narrowly necessary supporting code for a P0/P1 acceptance contract is allowed.

Build actual complete vertical functionality, not administrative slices: UI/API/database/worker/readback as relevant, with error/recovery tests. Prefer finishing existing P0/P1 capabilities over new child issues and bookkeeping. Prioritize severe security/data loss first, then high-impact end-to-end product workflows. Do not select an issue already owned by another agent. Use one integration owner, no more than three concurrent disjoint writer teams, independent worktrees/paths, and at most $Agents agent threads. If overlapping scopes or ownership is uncertain, work serially.

Git common directory is explicitly writable via the launcher; check live ownership before writes. The AFK-DEV loop is NOT this campaign's owner: do not spawn another AFK runner or replace its historical goal. This launcher supplies fresh-context iteration. Do not insist on planning-only sessions when an existing issue has an actionable contract. If real architecture/design authority is missing, choose a different unblocked P0/P1 card.

For each delivery: implement source, add meaningful tests, run the impacted integration/browser checks, review, commit with truthful AI attribution, publish a reviewable PR when permitted, verify exact-head CI, integrate only under repository rules, and update the original acceptance matrix only for proven full criteria. Do not infer PASS from merge or a mock.

No P2-P6 work. No real job applications, paid inference, credential access, unrelated host changes, force push, unsafe sandbox bypass, or false PASS. Preserve all existing worktrees, changes and private data.

Keep working through multiple P0/P1 cards in this iteration. Do not return a plan-only answer. Finish with a JSON object matching the provided output schema. Include only P0/P1 issue IDs actually worked on. Use CONTINUE if safe work remains; BLOCKED only if the campaign truly cannot proceed; COMPLETE only if validated original requirements and the eligible Project queue are done. Report exact next action.
"@
        Set-Content -LiteralPath $instructionPath -Value $directive -Encoding utf8
        $outputPath = Join-Path $runDir "result-$attempt.json"
        $latestData = @{campaign=$campaign;workspace=$workspace;iteration=$attempt;stagnant=$stagnant;status='RUNNING';updated=(Get-Date).ToUniversalTime().ToString('o')}
        $latestData | ConvertTo-Json | Set-Content -LiteralPath $latestPath -Encoding utf8
        Write-Host "===== MJobs P0/P1 iteration ${attempt}: $($queue.Count) eligible issues ====="
        $shortPrompt = "Read the complete authorized P0/P1 implementation directive at $instructionPath and the Project-only queue JSON. Execute it now. Return only the required JSON result."
        $args = @('exec','-C',$workspace,'--add-dir',$common,'-s','workspace-write',
            '-m',$Model,'-c',"model_reasoning_effort=`"$Reasoning`"",
            '-c','approval_policy="never"',
            '-c',"agents.max_concurrent_threads_per_session=$Agents",
            '-c',"agents.default_subagent_model=`"$Model`"",
            '-c','agents.default_subagent_reasoning_effort="medium"',
            '--enable','multi_agent',
            '--output-schema',$schemaPath,'--output-last-message',$outputPath,
            $shortPrompt)
        & codex @args
        $exit = $LASTEXITCODE
        if ($exit -ne 0 -or -not (Test-Path -LiteralPath $outputPath)) {
            $latestData.status = 'ERROR'
            $latestData.updated = (Get-Date).ToUniversalTime().ToString('o')
            $latestData | ConvertTo-Json | Set-Content -LiteralPath $latestPath -Encoding utf8
            throw "Codex iteration $attempt failed (exit $exit). Workspace and checkpoints preserved: $workspace"
        }
        try { $result = Get-Content -LiteralPath $outputPath -Raw | ConvertFrom-Json } 
        catch { throw "Malformed iteration response in $outputPath; stopping without repeating." }
        $allowed = @($queue | ForEach-Object {$_.issue})
        foreach ($issue in @($result.issueIds)) {
            if ([int]$issue -notin $allowed) {
                throw "P0/P1 SCOPE VIOLATION: Codex reported issue #$issue outside the eligible Project queue. Stopping."
            }
        }
        $after = (Invoke-Checked git @('-C',$workspace,'rev-parse','HEAD')).Trim()
        $stagnant = if ($before -eq $after -and @($result.pullRequests).Count -eq 0) { $stagnant + 1 } else { 0 }
        $latestData.stagnant = $stagnant
        $latestData.status = [string]$result.status
        $latestData.updated = (Get-Date).ToUniversalTime().ToString('o')
        $latestData | ConvertTo-Json | Set-Content -LiteralPath $latestPath -Encoding utf8
        Write-Host "Codex iteration ${attempt}: $($result.status) | $($result.summary)"
        if ($result.status -in @('BLOCKED','COMPLETE')) { break }
        if ($stagnant -ge 2) {
            Write-Warning 'Two unchanged rounds with no published PRs. Stopping to avoid wasting usage.'
            break
        }
    }
    Write-Host "MJobs P0/P1 campaign checkpoint: $latestPath"
    Write-Host "Worktree retained: $workspace"
} finally {
    if ($lockStream) { $lockStream.Dispose() }
}
