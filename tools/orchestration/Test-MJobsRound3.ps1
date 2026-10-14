#requires -Version 7.0
# Deterministic regression tests. No model usage, authentication writes or remote mutations.
$ErrorActionPreference='Stop'
$modulePath=Join-Path $PSScriptRoot 'MJobsRound3.psm1'
$results=[Collections.Generic.List[object]]::new()
function Assert-True([bool]$Condition,[string]$Message){if(-not $Condition){throw $Message}}
function Assert-Throws([scriptblock]$Action,[string]$Pattern){
    $caught=$null
    try{& $Action|Out-Null}catch{$caught=$_.Exception.Message}
    if(-not $caught -or $caught -notmatch $Pattern){throw "Expected $Pattern; received $caught"}
}
function Test-Case([string]$Name,[scriptblock]$Action){
    Import-Module $modulePath -Force -DisableNameChecking
    try{& $Action; $results.Add(@{name=$Name;passed=$true});Write-Host "PASS $Name"}
    catch{$results.Add(@{name=$Name;passed=$false;error=$_.Exception.Message});Write-Host "FAIL $Name : $($_.Exception.Message)"}
}
function Mock-Native([object[]]$Responses){
    $m=Get-Module MJobsRound3
    & $m {param($rows)
        $script:Responses=$rows; $script:Calls=[Collections.Generic.List[object]]::new(); $script:Index=0
        function script:Invoke-MJobsProcess {
            param($Program,$Arguments,$TimeoutSeconds,$WorkingDirectory)
            $script:Calls.Add(@{program=$Program;arguments=@($Arguments)})
            if($script:Index -ge $script:Responses.Count){throw 'Unexpected mock call'}
            return $script:Responses[$script:Index++]
        }
        function script:Start-Sleep {param($Seconds)}
    } $Responses
}
function Call-Count { & (Get-Module MJobsRound3) { $script:Calls.Count } }
function Response([int]$Code,[string]$Out='', [string]$Err='') {@{ExitCode=$Code;StdOut=$Out;StdErr=$Err}}
function Card([int]$Number=1,[string]$Priority='P0',[string]$Status='Todo',[string]$Acceptance='PARTIAL'){
    @{id="card-$Number";content=@{type='Issue';repository='MulattoTech/MJobs';number=$Number};priority=$Priority;labels=@("priority:$Priority");status=$Status;acceptance=$Acceptance;title="Synthetic $Number";area='synthetic';category='feature'}
}
Test-Case '401 is actionable and makes no Project or clone call' {
    Mock-Native @((Response 1 '{"status":"401"}' 'gh: Requires authentication (HTTP 401)'))
    Assert-Throws { Assert-MJobsGitHubAccess } 'MJOBS_AUTH_REQUIRED'
    Assert-True ((Call-Count) -eq 1) '401 was retried or queried Project'
}
Test-Case 'Authenticated wrong account stops before Project access' {
    Mock-Native @((Response 0 '{"login":"someone-else"}'))
    Assert-Throws { Assert-MJobsGitHubAccess } 'MJOBS_WRONG_ACCOUNT'
    Assert-True ((Call-Count) -eq 1) 'wrong user was not stopped'
}
Test-Case 'Valid identity and exact Project access succeed' {
    Mock-Native @((Response 0 '{"login":"MulattoTech"}'),(Response 0 '{"data":{"user":{"projectV2":{"id":"PVT_TEST","number":1,"title":"MJobs","closed":false,"items":{"totalCount":2}}}}}'))
    $p=Assert-MJobsGitHubAccess
    Assert-True ($p.id -eq 'PVT_TEST') 'Project not resolved'
    Assert-True ((Call-Count) -eq 2) 'unexpected auth calls'
}
Test-Case 'GraphQL error inside HTTP 200 is not success' {
    Mock-Native @((Response 0 '{"login":"MulattoTech"}'),(Response 0 '{"errors":[{"message":"Missing read:project scope"}]}'))
    Assert-Throws { Assert-MJobsGitHubAccess } 'MJOBS_PROJECT_ACCESS'
}
Test-Case 'Missing Project is not an empty completed queue' {
    Mock-Native @((Response 0 '{"login":"MulattoTech"}'),(Response 0 '{"data":{"user":{"projectV2":null}}}'))
    Assert-Throws { Assert-MJobsGitHubAccess } 'MJOBS_PROJECT_UNAVAILABLE'
}
Test-Case 'Temporary 503 read retries then returns result' {
    Mock-Native @((Response 1 '' 'HTTP 503 Service Unavailable'),(Response 0 '{"ok":true}'))
    $r=Invoke-MJobsRead gh @('api','user') -RetryRead
    Assert-True ($r.ExitCode -eq 0 -and (Call-Count) -eq 2) '503 retry incorrect'
}
Test-Case 'Persistent 503 reads stop after three attempts' {
    Mock-Native @((Response 1 '' 'HTTP 503'),(Response 1 '' 'HTTP 503'),(Response 1 '' 'HTTP 503'))
    Assert-Throws { Invoke-MJobsRead gh @('api','user') -RetryRead } 'MJOBS_COMMAND_FAILED'
    Assert-True ((Call-Count) -eq 3) 'retry count is not bounded'
}
Test-Case '403 rate limit is not retried' {
    Mock-Native @((Response 1 '' 'HTTP 403 API rate limit exceeded'))
    Assert-Throws { Invoke-MJobsRead gh @('api','graphql') -RetryRead } 'MJOBS_COMMAND_FAILED'
    Assert-True ((Call-Count) -eq 1) 'rate limit retried'
}
Test-Case 'Unknown owner error is not blindly retried' {
    Mock-Native @((Response 1 '' 'unknown owner type'))
    Assert-Throws { Invoke-MJobsRead gh @('project','item-list') -RetryRead } 'unknown owner type'
    Assert-True ((Call-Count) -eq 1) 'owner error retried'
}
Test-Case 'Mutating calls are not retried by default' {
    Mock-Native @((Response 1 '' 'HTTP 503'))
    Assert-Throws { Invoke-MJobsRead gh @('api','--method','POST','example') } 'MJOBS_COMMAND_FAILED'
    Assert-True ((Call-Count) -eq 1) 'mutation retried'
}
Test-Case 'Queue excludes P2-P6 and active/completed cards' {
    $cards=@((Card 1 P0),(Card 2 P1),(Card 3 P2),(Card 4 P3),(Card 5 P4),(Card 6 P5),(Card 7 P6),(Card 8 P0 Done PASS),(Card 9 P1 Todo PASS),(Card 10 P1 'In Progress'))
    $selected=@(Select-MJobsQueue @{items=$cards;totalCount=$cards.Count})
    Assert-True (($selected.issue -join ',') -eq '1,2') 'out-of-scope card selected'
}
Test-Case 'Truncated queue fails closed' {
    Assert-Throws {Select-MJobsQueue @{items=@((Card));totalCount=2}} 'MJOBS_QUEUE_INCOMPLETE'
}
Test-Case 'Complete empty queue is valid without claiming product acceptance' {
    Assert-True (@(Select-MJobsQueue @{items=@();totalCount=0}).Count -eq 0) 'empty queue rejected'
}
Test-Case 'Priority mismatch fails closed' {
    $c=Card; $c.labels=@('priority:P1')
    Assert-Throws {Select-MJobsQueue @{items=@($c);totalCount=1}} 'MJOBS_PRIORITY_DRIFT'
}
Test-Case 'Duplicate Project issue fails closed' {
    Assert-Throws {Select-MJobsQueue @{items=@((Card),(Card));totalCount=2}} 'MJOBS_QUEUE_INVALID'
}
Test-Case 'Malformed JSON is not treated as zero results' {
    Assert-Throws {Convert-MJobsJson '{bad' 'test'} 'MJOBS_INVALID_JSON'
}
Test-Case 'Sensitive diagnostics are redacted' {
    $out=Protect-MJobsText 'ghp_SYNTHETIC123 api_key=EXAMPLE_SECRET Authorization: Bearer EXAMPLE_BEARER'
    Assert-True ($out -notmatch 'SYNTHETIC123|EXAMPLE_SECRET|EXAMPLE_BEARER') 'redaction failed'
}
Test-Case 'UTF-8 array serialization preserves one queue item' {
    $tmp=Join-Path ([IO.Path]::GetTempPath()) ('mjobs-json-'+[Guid]::NewGuid().ToString('N')+'.json')
    try{
        Write-MJobsJson $tmp @((Card))
        $s=[IO.File]::ReadAllText($tmp)
        Assert-True ($s.TrimStart().StartsWith('[')) 'one row collapsed to an object'
        Write-MJobsJson $tmp @()
        Assert-True ([IO.File]::ReadAllText($tmp).Trim() -eq '[]') 'atomic replace failed'
    }finally{if(Test-Path $tmp){Remove-Item -LiteralPath $tmp}}
}
Test-Case 'Real native process preserves quoted/Unicode output and stderr' {
    $r=Invoke-MJobsProcess pwsh @('-NoProfile','-Command','[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); [Console]::Out.Write("UTF8: cafe" + [char]0xE9); [Console]::Error.Write("synthetic-stderr"); exit 7')
    Assert-True ($r.ExitCode -eq 7 -and $r.StdErr -eq 'synthetic-stderr' -and $r.StdOut.EndsWith([string][char]0xE9)) 'native capture corrupted output'
}
Test-Case 'Real timeout terminates only its owned child' {
    $r=Invoke-MJobsProcess pwsh @('-NoProfile','-Command','Start-Sleep -Seconds 10') -TimeoutSeconds 1
    Assert-True ($r.ExitCode -eq 124) 'timeout was not bounded'
}
Test-Case 'Launcher contains auth gate, native TUI and reviewed sandbox mode' {
    $root=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
    $text=Get-Content (Join-Path $root 'Start-MJobs-P0P1-Round3.ps1') -Raw
    Assert-True ($text.Contains('Assert-MJobsGitHubAccess -RepairAuth:$RepairAuth')) 'auth gate missing'
    Assert-True ($text.Contains('if ($Interactive)') -and $text.Contains('& codex @tuiArguments')) 'TUI not wired'
    Assert-True ($text.Contains('--approve-for-me') -and -not $text.Contains('approval_policy="never"')) 'never policy persists'
    Assert-True ($text.Contains('[switch]$Yolo')) 'YOLO must remain an explicit opt-in flag'
    Assert-True ($text.Contains('if ($Yolo -and -not $Preview)') -and $text.Contains('YOLO_BLOCKED_ELEVATED') -and $text.Contains('IsInRole(')) 'YOLO must refuse elevated Windows sessions'
    Assert-True ($text.Contains('$approvalArguments = if ($Yolo)') -and $text.Contains("@('--dangerously-bypass-approvals-and-sandbox')")) 'YOLO may not be mixed into the default sandbox mode'
    Assert-True ($text.Contains('--approve-for-me') -and $text.Contains('approvals_reviewer="auto_review"')) 'default reviewed approvals missing'
    Assert-True ($text.Contains("'.mjobs-run'")) 'directives still live only in protected metadata'
}
$failed=@($results|Where-Object {-not $_.passed})
Write-Host ("RESULT: {0} passed, {1} failed" -f ($results.Count-$failed.Count),$failed.Count)
if($failed.Count){exit 1}
