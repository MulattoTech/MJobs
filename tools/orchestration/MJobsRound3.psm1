#requires -Version 7.0
# Round 3 helpers: no credential extraction, no global configuration changes.
$ErrorActionPreference = 'Stop'
function Protect-MJobsText([string]$Text) {
    $s = $Text -replace '(?i)(gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)', '[REDACTED]'
    $s = $s -replace '(?i)(authorization\s*[:=]\s*(?:bearer|token)?\s*)\S+', '$1[REDACTED]'
    return ($s -replace '(?i)((?:password|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[=:]\s*)[^\s,;]+', '$1[REDACTED]')
}
function Invoke-MJobsProcess {
    param([string]$Program,[string[]]$Arguments,[int]$TimeoutSeconds=90,[string]$WorkingDirectory='')
    $command = Get-Command $Program -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName=$command.Source; $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true; $info.RedirectStandardInput=$true
    $info.StandardOutputEncoding=[Text.UTF8Encoding]::new($false); $info.StandardErrorEncoding=[Text.UTF8Encoding]::new($false)
    if ($WorkingDirectory) { $info.WorkingDirectory=$WorkingDirectory }
    foreach ($arg in $Arguments) { $info.ArgumentList.Add([string]$arg) }
    $process=[Diagnostics.Process]::new(); $process.StartInfo=$info
    try {
        if (-not $process.Start()) { throw "Could not start $Program" }
        $process.StandardInput.Close()
        $stdout=$process.StandardOutput.ReadToEndAsync(); $stderr=$process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds*1000)) {
            $process.Kill($true); $process.WaitForExit() # Only our own child process.
            return @{ExitCode=124;StdOut='';StdErr="$Program timed out after $TimeoutSeconds seconds."}
        }
        return @{ExitCode=$process.ExitCode;StdOut=$stdout.GetAwaiter().GetResult();StdErr=$stderr.GetAwaiter().GetResult()}
    } finally { $process.Dispose() }
}
function Invoke-MJobsRead {
    param([string]$Program,[string[]]$Arguments,[int]$TimeoutSeconds=90,[switch]$AllowFailure,[switch]$RetryRead)
    for ($attempt=1;$attempt -le 3;$attempt++) {
        $result=Invoke-MJobsProcess -Program $Program -Arguments $Arguments -TimeoutSeconds $TimeoutSeconds
        $text=[string]$result.StdErr+"`n"+[string]$result.StdOut
        $permanent=$text -match '(?i)HTTP (401|403|404|429)|bad credentials|requires authentication|missing.*scope|rate limit|unknown owner type'
        $transient=($result.ExitCode -eq 124) -or ($text -match '(?i)HTTP (500|502|503|504)|connection reset|connection refused|TLS handshake timeout|temporary failure|error connecting|could not resolve host|no such host')
        if ($result.ExitCode -eq 0 -or -not $RetryRead -or $permanent -or -not $transient -or $attempt -eq 3) { break }
        Write-Warning "Transient $Program read failure; retry $attempt of 2."; Start-Sleep -Seconds $attempt
    }
    if ($result.ExitCode -ne 0 -and -not $AllowFailure) {
        $detail=Protect-MJobsText ([string]$result.StdErr+' '+[string]$result.StdOut)
        if ($detail.Length -gt 900) { $detail=$detail.Substring(0,900) }
        throw "MJOBS_COMMAND_FAILED: $Program exited $($result.ExitCode). $detail"
    }
    return $result
}
function Convert-MJobsJson([string]$Text,[string]$Label) {
    try { return ConvertFrom-Json -InputObject $Text -AsHashtable -ErrorAction Stop }
    catch { throw "MJOBS_INVALID_JSON: $Label returned malformed JSON; not an empty result." }
}
function Invoke-MJobsBrowserLogin {
    param([switch]$Refresh)
    foreach ($name in @('GH_TOKEN','GITHUB_TOKEN')) {
        if ([Environment]::GetEnvironmentVariable($name)) { throw "MJOBS_AUTH_OVERRIDE: $name is set. Review that override in your terminal before browser login; its value was not printed." }
    }
    Write-Host 'Complete GitHub browser/device authorization as MulattoTech. Do not paste credentials into chat.'
    if ($Refresh) { & gh auth refresh --hostname github.com --scopes 'project,read:org,workflow' }
    else { & gh auth login --hostname github.com --git-protocol https --web --scopes 'project,workflow' }
    if ($LASTEXITCODE -ne 0) { throw 'MJOBS_AUTH_PENDING: Browser authorization did not complete; no campaign started.' }
}
function Assert-MJobsGitHubAccess {
    param([switch]$RepairAuth)
    if ($env:GH_HOST -and $env:GH_HOST -ne 'github.com') { throw 'MJOBS_WRONG_HOST: GH_HOST is not github.com. No environment settings changed.' }
    $identity=Invoke-MJobsRead gh @('api','--hostname','github.com','user') -AllowFailure -RetryRead
    if ($identity.ExitCode -ne 0) {
        $detail=[string]$identity.StdErr+[string]$identity.StdOut
        if ($detail -match '(?i)401|bad credentials|requires authentication|not logged|auth login') {
            if (-not $RepairAuth) { throw 'MJOBS_AUTH_REQUIRED: GitHub CLI login is missing or invalid (HTTP 401). Run: pwsh -NoProfile -File .\Start-MJobs-P0P1-Round3.ps1 -RepairAuth -Preview. Authorize GitHub in your browser. No clone or Codex run started.' }
            Invoke-MJobsBrowserLogin
            $identity=Invoke-MJobsRead gh @('api','--hostname','github.com','user') -RetryRead
        } else { throw ('MJOBS_GITHUB_UNAVAILABLE: '+(Protect-MJobsText $detail)) }
    }
    $who=Convert-MJobsJson $identity.StdOut 'authenticated identity'
    if ($who.login -ne 'MulattoTech') { throw "MJOBS_WRONG_ACCOUNT: CLI is signed in as $($who.login), not MulattoTech. Review gh auth switch." }
    $query='query($owner:String!,$number:Int!){user(login:$owner){login projectV2(number:$number){id number title closed items{totalCount}}}}'
    $arguments=@('api','--hostname','github.com','graphql','-f',"query=$query",'-F','owner=MulattoTech','-F','number=1')
    $read=Invoke-MJobsRead gh $arguments -AllowFailure -RetryRead
    $data=if($read.StdOut){Convert-MJobsJson $read.StdOut 'Project access'}else{@{}}
    if ($read.ExitCode -ne 0 -or $data.errors) {
        $detail=[string]$read.StdErr+[string]$read.StdOut
        if ($RepairAuth -and $detail -match '(?i)scope|FORBIDDEN|resource not accessible') {
            Invoke-MJobsBrowserLogin -Refresh
            $read=Invoke-MJobsRead gh $arguments -RetryRead; $data=Convert-MJobsJson $read.StdOut 'Project access'
        } else { throw ('MJOBS_PROJECT_ACCESS: Login valid but Project #1 inaccessible. Check project/read:org access or the reported service error; do not recreate the board. '+(Protect-MJobsText $detail)) }
    }
    if ($data.errors -or -not $data.data.user.projectV2) { throw 'MJOBS_PROJECT_UNAVAILABLE: Project #1 not returned; no completion is inferred.' }
    $project=$data.data.user.projectV2
    if ($project.closed) { throw 'MJOBS_PROJECT_CLOSED: Project #1 is closed; no campaign started.' }
    return $project
}
function Select-MJobsQueue {
    param([System.Collections.IDictionary]$Document)
    if (-not $Document.Contains('items') -or -not $Document.Contains('totalCount')) { throw 'MJOBS_QUEUE_INCOMPLETE: Missing items or totalCount.' }
    $all=@($Document.items)
    if ($all.Count -ne [int]$Document.totalCount) { throw 'MJOBS_QUEUE_INCOMPLETE: Partial Project response; refusing dispatch.' }
    $seen=@{}
    $eligible=foreach ($item in $all) {
        if ($item.content.type -ne 'Issue' -or $item.content.repository -ne 'MulattoTech/MJobs') { continue }
        $number=[int]$item.content.number
        if ($number -lt 1 -or $seen.ContainsKey($number)) { throw 'MJOBS_QUEUE_INVALID: Invalid or duplicate issue card.' }
        $seen[$number]=$true
        if ($item.priority -cnotin @('P0','P1')) { continue }
        $labels=@($item.labels|Where-Object {$_ -like 'priority:*'})
        if ($labels.Count -ne 1 -or $labels[0] -cne "priority:$($item.priority)") { throw "MJOBS_PRIORITY_DRIFT: Project and issue label disagree on #$number." }
        if ($item.status -eq 'Done' -or $item.acceptance -eq 'PASS') { continue }
        if ($item.status -notin @('Todo','In Progress') -or $item.acceptance -notin @('PARTIAL','NOT_RUN','FAIL','BLOCKED','UNTRACKED')) { throw "MJOBS_QUEUE_INVALID: Incomplete status metadata for #$number." }
        if ($item.status -eq 'In Progress') { continue } # Never take an active card automatically.
        [pscustomobject]@{issue=$number;itemId=$item.id;priority=$item.priority;acceptance=$item.acceptance;status=$item.status;area=$item.area;category=$item.category;title=$item.title}
    }
    return @($eligible|Sort-Object priority,issue)
}
function Get-MJobsQueue {
    $project=Assert-MJobsGitHubAccess
    $limit=[Math]::Max(100,[int]$project.items.totalCount+50)
    $read=Invoke-MJobsRead gh @('project','item-list','1','--owner','MulattoTech','--limit',[string]$limit,'--format','json') -RetryRead
    return @(Select-MJobsQueue (Convert-MJobsJson $read.StdOut 'Project items'))
}
function Get-MJobsMainAcceptance {
    $ref=Invoke-MJobsRead gh @('api','--hostname','github.com','repos/MulattoTech/MJobs/commits/main') -RetryRead
    $head=(Convert-MJobsJson $ref.StdOut 'GitHub main').sha
    if ($head -notmatch '^[0-9a-f]{40,64}$') { throw 'MJOBS_MAIN_INVALID: Missing main SHA.' }
    $read=Invoke-MJobsRead gh @('api','--hostname','github.com',"repos/MulattoTech/MJobs/contents/planning/CAPABILITY_MATRIX.json?ref=$head") -RetryRead
    $blob=Convert-MJobsJson $read.StdOut 'acceptance matrix'
    if ($blob.encoding -ne 'base64') { throw 'MJOBS_MATRIX_INVALID: Unsupported contents encoding.' }
    $matrix=Convert-MJobsJson ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($blob.content))) 'decoded acceptance matrix'
    if (-not $matrix.requirements) { throw 'MJOBS_MATRIX_INVALID: Missing requirements.' }
    $answer=@{Head=$head;PassIds=@();P0=0;P1=0;P0Total=0;P1Total=0}
    foreach($p in @('P0','P1')){
        $rows=@($matrix.requirements|Where-Object {$_.priority -eq $p}); $passed=@($rows|Where-Object {$_.result -eq 'PASS'})
        $answer[$p]=$passed.Count; $answer[$p+'Total']=$rows.Count
        $answer.PassIds+=@($passed|ForEach-Object {$_.requirement_id})
    }
    return $answer
}
function Write-MJobsJson([string]$Path,[object]$Value){
    $temp=$Path+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    try{
        [IO.File]::WriteAllText($temp,(ConvertTo-Json -InputObject $Value -Depth 20),[Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temp,$Path,$true)
    }finally{if([IO.File]::Exists($temp)){[IO.File]::Delete($temp)}}
}
Export-ModuleMember -Function Protect-MJobsText,Invoke-MJobsProcess,Invoke-MJobsRead,Convert-MJobsJson,Assert-MJobsGitHubAccess,Select-MJobsQueue,Get-MJobsQueue,Get-MJobsMainAcceptance,Write-MJobsJson
