<#
Read-only proof of an already-running, explicitly reviewed native Codex resume.
No socket connections, provider commands, history/credential reads, process
mutations or ownership writes. Reuses the established Win32 read-only helpers.
#>
[CmdletBinding()]
param([string]$RequestBase64,[switch]$SelfTest)
$ErrorActionPreference='Stop'
$reviewRequestBase64=$RequestBase64
$reviewSelfTest=$SelfTest
. (Join-Path $PSScriptRoot 'Get-AoRuntimeEvidence.ps1') -LoadFunctionsOnly

function Test-ReviewedCodexResume([string[]]$Arguments,[string]$NativeSessionId){
    # The pinned AO buildCodexRestore places configuration flags BEFORE the
    # positional UUID. Only the observed, reviewed option arities qualify.
    if($Arguments.Count -lt 3 -or $Arguments.Count -gt 256 -or $Arguments[1] -cne 'resume') {return $false}
    $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $index=2
    while($index -lt $Arguments.Count){
        $token=$Arguments[$index]
        if($token -ceq '-c'){
            if($index+1 -ge $Arguments.Count -or $Arguments[$index+1] -cnotmatch '^[^=\s]+=') {return $false}
            $index+=2;continue
        }
        if($token -cin @('--ask-for-approval','--model')){
            if(-not $seen.Add($token) -or $index+1 -ge $Arguments.Count -or
                [string]::IsNullOrWhiteSpace($Arguments[$index+1]) -or $Arguments[$index+1].StartsWith('-')) {return $false}
            $index+=2;continue
        }
        if($token -cin @('--dangerously-bypass-hook-trust','--no-alt-screen')){
            if(-not $seen.Add($token)){return $false}
            $index++;continue
        }
        # No prompt, picker, --last/--all, unreviewed option or second positional
        # token may substitute for an exact explicit native conversation UUID.
        return $index -eq $Arguments.Count-1 -and $token -ceq $NativeSessionId -and
            $token -cmatch '^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
    }
    return $false
}
function ConvertTo-ReviewedProcessRecord($Record){
    if(-not $Record -or -not $Record.CreationDate -or $Record.ProcessId -le 0) {throw 'REVIEWED_NATIVE_PROCESS_UNAVAILABLE'}
    $created=([DateTimeOffset]$Record.CreationDate).ToUniversalTime()
    $executable=[string]$Record.ExecutablePath;$commandLine=[string]$Record.CommandLine
    if(-not $executable -or -not $commandLine){
        $limited=Read-FleetLimitedProcessIdentity ([uint32]$Record.ProcessId)
        if([Math]::Abs($limited.CreatedAt.UtcTicks-$created.UtcTicks) -ge 10) {throw 'REVIEWED_NATIVE_PROCESS_CHANGED'}
        if($executable -and [FleetRuntimeEvidenceV1.Native]::CanonicalPath($executable) -cne [FleetRuntimeEvidenceV1.Native]::CanonicalPath($limited.ExecutablePath)) {throw 'REVIEWED_NATIVE_PROCESS_CHANGED'}
        if($commandLine -and $commandLine -cne $limited.CommandLine) {throw 'REVIEWED_NATIVE_PROCESS_CHANGED'}
        $executable=$limited.ExecutablePath;$commandLine=$limited.CommandLine
    }
    $image=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($executable)
    $arguments=[FleetRuntimeEvidenceV1.Native]::Argv($commandLine)
    if($arguments.Count -lt 1) {throw 'REVIEWED_NATIVE_PROCESS_UNAVAILABLE'}
    if([IO.Path]::IsPathRooted($arguments[0])){
        if([FleetRuntimeEvidenceV1.Native]::CanonicalPath($arguments[0]) -cne $image) {throw 'REVIEWED_NATIVE_EXECUTABLE_MISMATCH'}
    }elseif($arguments[0] -match '[/\\]' -or [IO.Path]::GetFileNameWithoutExtension($arguments[0]) -ine [IO.Path]::GetFileNameWithoutExtension($image)){
        throw 'REVIEWED_NATIVE_EXECUTABLE_MISMATCH'
    }
    if($created -gt [DateTimeOffset]::UtcNow.AddSeconds(1)) {throw 'REVIEWED_NATIVE_PROCESS_CHANGED'}
    return [pscustomobject]@{processId=[long]$Record.ProcessId;parentPid=[long]$Record.ParentProcessId;
        createdAt=$created;executable=$image;arguments=$arguments}
}
function Get-ReviewedProcessInventory {
    $rows=@(Get-CimInstance -ClassName Win32_Process -Property ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine -OperationTimeoutSec 2 -ErrorAction Stop)
    $byPid=@{}
    foreach($row in $rows){
        $key=[long]$row.ProcessId
        if($byPid.ContainsKey($key)){throw 'REVIEWED_NATIVE_PROCESS_CHANGED'}
        $byPid[$key]=$row
    }
    return $byPid
}
function Get-ReviewedRuntimeChain($Request,[hashtable]$Records,[string]$AoExecutable,[string]$CodexExecutable,
    [scriptblock]$Normalize={param($Record) ConvertTo-ReviewedProcessRecord $Record}){
    $expected=$Request.nativeRuntime
    $visited=[Collections.Generic.HashSet[long]]::new()
    $chain=@();$currentPid=[long]$expected.providerPid;$previousCreated=$null
    for($depth=0;$depth -lt 16;$depth++){
        if($currentPid -le 0 -or -not $visited.Add($currentPid) -or -not $Records.ContainsKey($currentPid)) {throw 'REVIEWED_NATIVE_ANCESTRY_UNVERIFIED'}
        $record=& $Normalize $Records[$currentPid]
        if(-not $record -or $record.processId -ne $currentPid -or -not $record.createdAt -or
            ($null -ne $previousCreated -and $record.createdAt -gt $previousCreated)) {throw 'REVIEWED_NATIVE_ANCESTRY_UNVERIFIED'}
        if($depth -eq 0){
            if($record.executable -cne $CodexExecutable) {throw 'REVIEWED_NATIVE_EXECUTABLE_MISMATCH'}
            if($record.createdAt -ne [DateTimeOffset]$expected.providerCreatedAt) {throw 'REVIEWED_NATIVE_PROCESS_CHANGED'}
            if(-not (Test-ReviewedCodexResume $record.arguments $expected.nativeSessionId)) {throw 'REVIEWED_NATIVE_RESUME_UNVERIFIED'}
        }
        $chain+=,[ordered]@{pid=$record.processId;parentPid=$record.parentPid;createdAt=$record.createdAt.UtcTicks;
            executable=$record.executable;argvHash=(Get-FleetTextHash ($record.arguments|ConvertTo-Json -Compress))}
        if($currentPid -eq $expected.hostPid){
            if($depth -eq 0 -or $record.executable -cne $AoExecutable -or $record.createdAt -ne [DateTimeOffset]$expected.hostCreatedAt) {throw 'REVIEWED_NATIVE_HOST_UNVERIFIED'}
            $hostArguments=Get-FleetHostArguments $record.arguments
            if($hostArguments.kind -cne 'tui' -or $hostArguments.sessionId -cne $Request.id -or $hostArguments.auxiliaryShell -eq $true) {throw 'REVIEWED_NATIVE_HOST_UNVERIFIED'}
            return Get-FleetTextHash ($chain|ConvertTo-Json -Compress -Depth 8)
        }
        $previousCreated=$record.createdAt;$currentPid=[long]$record.parentPid
    }
    throw 'REVIEWED_NATIVE_ANCESTRY_UNVERIFIED'
}
function Invoke-ReviewedRuntimeProof($Request){
    $expected=$Request.nativeRuntime
    if($Request.version -ne 1 -or $Request.probeId -cnotmatch '^[a-f0-9]{32}$' -or
        $Request.id -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' -or
        $expected.nativeSessionId -cnotmatch '^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$' -or
        $expected.nativeSessionId -ceq '00000000-0000-0000-0000-000000000000') {throw 'REVIEWED_NATIVE_IDENTITY_INVALID'}
    foreach($key in @('providerPid','hostPid')){
        if(-not ($expected.$key -is [long] -or $expected.$key -is [int]) -or $expected.$key -le 0 -or $expected.$key -gt [uint32]::MaxValue) {throw 'REVIEWED_NATIVE_IDENTITY_INVALID'}
    }
    $providerCreated=[DateTimeOffset]::MinValue;$hostCreated=[DateTimeOffset]::MinValue
    if($expected.providerPid -eq $expected.hostPid -or
        -not [DateTimeOffset]::TryParse([string]$expected.providerCreatedAt,[ref]$providerCreated) -or
        -not [DateTimeOffset]::TryParse([string]$expected.hostCreatedAt,[ref]$hostCreated) -or
        $hostCreated -gt $providerCreated -or $providerCreated -gt [DateTimeOffset]::UtcNow.AddSeconds(1)) {throw 'REVIEWED_NATIVE_IDENTITY_INVALID'}
    $aoExecutable=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($Request.aoExecutable)
    $codexExecutable=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($Request.codexExecutable)
    if([FleetRuntimeEvidenceV1.Native]::CanonicalPath($expected.providerExecutable) -cne $codexExecutable) {throw 'REVIEWED_NATIVE_EXECUTABLE_MISMATCH'}
    $before=Get-ReviewedRuntimeChain $Request (Get-ReviewedProcessInventory) $aoExecutable $codexExecutable
    Start-Sleep -Milliseconds 150
    $after=Get-ReviewedRuntimeChain $Request (Get-ReviewedProcessInventory) $aoExecutable $codexExecutable
    if($before -cne $after) {throw 'REVIEWED_NATIVE_PROCESS_CHANGED'}
    return [ordered]@{version=1;type='reviewed-native-runtime';probeId=$Request.probeId;id=$Request.id;
        verified=$true;observedAt=[DateTimeOffset]::UtcNow.ToString('o');
        nativeSessionId=$expected.nativeSessionId;providerPid=$expected.providerPid;providerCreatedAt=$expected.providerCreatedAt;
        providerExecutable=$codexExecutable;hostPid=$expected.hostPid;hostCreatedAt=$expected.hostCreatedAt;
        proof=@{sameProvider=$true;sameHost=$true;exactResume=$true;ancestryVerified=$true;stableObservations=$true}}
}
function Invoke-ReviewedRuntimeSelfTest {
    $script:reviewChecks=0
    function Assert-ReviewedProof([bool]$Condition,[string]$Message){$script:reviewChecks++;if(-not $Condition){throw "SELFTEST_FAILED: $Message"}}
    $thread='12345678-abcd-4abc-8abc-123456789abc'
    $valid=@('codex.exe','resume','-c','notice.x=true','-c','hooks={}','--dangerously-bypass-hook-trust',
        '--ask-for-approval','on-request','--no-alt-screen','--model','gpt-5','-c','model_instructions_file=C:\work\instructions',$thread)
    Assert-ReviewedProof (Test-ReviewedCodexResume $valid $thread) 'AO options precede the sole positional UUID'
    Assert-ReviewedProof (Test-ReviewedCodexResume @('codex.exe','resume',$thread) $thread) 'An exact minimal resume is identifiable'
    $invalid=@{
        'wrong subcommand'=@('codex.exe','exec',$thread)
        'UUID in config only'=@('codex.exe','resume','-c',('x='+$thread))
        'UUID in model only'=@('codex.exe','resume','--model',$thread)
        'picker option'=@('codex.exe','resume','--last',$thread)
        'extra prompt'=@('codex.exe','resume',$thread,'hello')
        'flag after UUID'=@('codex.exe','resume',$thread,'--no-alt-screen')
        'missing config value'=@('codex.exe','resume','-c',$thread)
        'missing valued option'=@('codex.exe','resume','--model','--no-alt-screen',$thread)
        'duplicate single flag'=@('codex.exe','resume','--no-alt-screen','--no-alt-screen',$thread)
        'unreviewed option'=@('codex.exe','resume','--config','x=true',$thread)
        'second UUID'=@('codex.exe','resume',$thread,$thread)
        'different UUID'=@('codex.exe','resume','ffffffff-abcd-4abc-8abc-123456789abc')
    }
    foreach($case in $invalid.GetEnumerator()){Assert-ReviewedProof (-not (Test-ReviewedCodexResume $case.Value $thread)) $case.Key}
    function New-ReviewedFixture {
        $stamp=[DateTimeOffset]::UtcNow.AddMinutes(-2)
        $request=[pscustomobject]@{id='mjobs-review-10';nativeRuntime=[pscustomobject]@{nativeSessionId=$thread;
            providerPid=[long]303;providerCreatedAt=$stamp.AddSeconds(2).ToString('o');providerExecutable='c:\fixture\codex.exe';
            hostPid=[long]101;hostCreatedAt=$stamp.ToString('o');terminalGeneration=$null}}
        $records=@{}
        $records[[long]101]=[pscustomobject]@{processId=[long]101;parentPid=[long]1;createdAt=$stamp;executable='c:\fixture\ao.exe';arguments=@('ao.exe','pty-host','mjobs-review-10','C:\work','pwsh.exe')}
        $records[[long]202]=[pscustomobject]@{processId=[long]202;parentPid=[long]101;createdAt=$stamp.AddSeconds(1);executable='c:\fixture\pwsh.exe';arguments=@('pwsh.exe','-NoProfile')}
        $records[[long]303]=[pscustomobject]@{processId=[long]303;parentPid=[long]202;createdAt=$stamp.AddSeconds(2);executable='c:\fixture\codex.exe';arguments=$valid.Clone()}
        return @{request=$request;records=$records}
    }
    $normalizer={param($Record) $Record}
    $fixture=New-ReviewedFixture
    $first=Get-ReviewedRuntimeChain $fixture.request $fixture.records 'c:\fixture\ao.exe' 'c:\fixture\codex.exe' $normalizer
    Assert-ReviewedProof ($first -cmatch '^[a-f0-9]{64}$') 'Exact native provider and ancestry yield a digest'
    $second=Get-ReviewedRuntimeChain $fixture.request $fixture.records 'c:\fixture\ao.exe' 'c:\fixture\codex.exe' $normalizer
    Assert-ReviewedProof ($first -ceq $second) 'Identical observations remain stable'
    $fixture.records[[long]202].arguments=@('pwsh.exe','-NoProfile','-NoLogo')
    $changed=Get-ReviewedRuntimeChain $fixture.request $fixture.records 'c:\fixture\ao.exe' 'c:\fixture\codex.exe' $normalizer
    Assert-ReviewedProof ($first -cne $changed) 'Intermediate argv changes invalidate stability'
    $changes=@{
        'provider PID absent'={param($f)$f.records.Remove([long]303)}
        'provider birth changed'={param($f)$f.records[[long]303].createdAt=$f.records[[long]303].createdAt.AddSeconds(1)}
        'provider image changed'={param($f)$f.records[[long]303].executable='c:\other\codex.exe'}
        'provider conversation changed'={param($f)$f.records[[long]303].arguments=@('codex.exe','resume','ffffffff-abcd-4abc-8abc-123456789abc')}
        'parent missing'={param($f)$f.records.Remove([long]202)}
        'parent PID reused'={param($f)$f.records[[long]202].createdAt=$f.records[[long]303].createdAt.AddSeconds(1)}
        'ancestry cycle'={param($f)$f.records[[long]202].parentPid=[long]303}
        'wrong host image'={param($f)$f.records[[long]101].executable='c:\other\ao.exe'}
        'wrong host session'={param($f)$f.records[[long]101].arguments=@('ao.exe','pty-host','mjobs-other','C:\work','pwsh.exe')}
        'wrong host mode'={param($f)$f.records[[long]101].arguments=@('ao.exe','chat-host','mjobs-review-10','C:\data','a','b','c')}
        'host birth changed'={param($f)$f.request.nativeRuntime.hostCreatedAt=([DateTimeOffset]$f.request.nativeRuntime.hostCreatedAt).AddSeconds(-1).ToString('o')}
        'unrelated ancestry'={param($f)$f.records[[long]202].parentPid=[long]404}
    }
    foreach($case in $changes.GetEnumerator()){
        $fixture=New-ReviewedFixture;& $case.Value $fixture|Out-Null
        $rejected=$false
        try{Get-ReviewedRuntimeChain $fixture.request $fixture.records 'c:\fixture\ao.exe' 'c:\fixture\codex.exe' $normalizer|Out-Null}catch{$rejected=$true}
        Assert-ReviewedProof $rejected $case.Key
    }
    $decoded=[FleetRuntimeEvidenceV1.Native]::Argv('"C:\Program Files\Codex\codex.exe" resume -c "x=spaces in value" --no-alt-screen '+$thread)
    Assert-ReviewedProof (Test-ReviewedCodexResume $decoded $thread) 'Win32 tokenization preserves quoted option values'
    return @{ok=$true;checks=$script:reviewChecks;processMutations=0;ownershipWrites=0;socketConnections=0}
}
$reviewProbeId=$null
try{
    if([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT){throw 'REVIEWED_NATIVE_PLATFORM_UNAVAILABLE'}
    Initialize-FleetRuntimeNative
    if($reviewSelfTest){Invoke-ReviewedRuntimeSelfTest|ConvertTo-Json -Compress -Depth 5;return}
    if(-not $reviewRequestBase64 -or $reviewRequestBase64.Length -gt 32768) {throw 'REVIEWED_NATIVE_IDENTITY_INVALID'}
    $reviewRequest=ConvertFrom-FleetStrictJson ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($reviewRequestBase64)))
    if($reviewRequest.probeId -cmatch '^[a-f0-9]{32}$'){$reviewProbeId=$reviewRequest.probeId}
    Invoke-ReviewedRuntimeProof $reviewRequest|ConvertTo-Json -Compress -Depth 8
}catch{
    if($reviewSelfTest){throw}
    $reason='REVIEWED_NATIVE_INSPECTION_UNAVAILABLE'
    if($_.Exception.Message -cmatch '^REVIEWED_NATIVE_[A-Z_]{2,65}$'){$reason=$_.Exception.Message}
    @{version=1;probeId=$reviewProbeId;verified=$false;observedAt=[DateTimeOffset]::UtcNow.ToString('o');reason=$reason}|ConvertTo-Json -Compress -Depth 3
}
