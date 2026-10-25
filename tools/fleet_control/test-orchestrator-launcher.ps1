param([string]$WorkerPath=(Join-Path $PSScriptRoot 'Start-Orchestrator-Requested.ps1'))
$ErrorActionPreference='Stop'
. $WorkerPath -LoadFunctionsOnly
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('fleet-launch-test-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($temporary)
$script:FleetRequestedRoot=$temporary
$count=0
function Assert-Fleet($Condition,[string]$Message){if(-not $Condition){throw $Message};$script:count++}
try {
    $request=@{version=1;nonce=[Guid]::NewGuid().ToString();mode='daemon';requestedAt=[DateTimeOffset]::UtcNow.ToString('o')}
    $requestPath=Join-Path $temporary 'orchestrator-launch-request.json'
    [IO.File]::WriteAllText($requestPath,($request|ConvertTo-Json))
    $request=[IO.File]::ReadAllText($requestPath)|ConvertFrom-Json -DateKind String
    Assert-Fleet ($request.requestedAt -is [string]) 'Timestamp must remain a string on PS7'
    Write-FleetLaunchResult @{version=1;nonce=$request.nonce;mode='daemon';status='accepted'}
    Write-FleetLaunchResult @{version=1;nonce=$request.nonce;mode='daemon';status='rejected';safeToRetry=$true}
    $written=[IO.File]::ReadAllText((Join-Path $temporary 'orchestrator-launch-result.json'))|ConvertFrom-Json
    Assert-Fleet ($written.status -eq 'rejected') 'Atomic second write must replace accepted status'
    $script:starts=0
    $adapters=@{
        Now={ [DateTimeOffset]::UtcNow }
        Token={ [pscustomobject]@{pid=1;isElevated=$false;integrityRid=8192;sessionId=1} }
        GetDaemon={ [pscustomobject]@{verified=$true;present=$false;healthy=$false} }
        GetUi={ [pscustomobject]@{verified=$true;running=$false} }
        StartDaemon={ $script:starts++;throw 'FIXTURE_START_THROW' }
        StartUi={ $script:starts++;throw 'FIXTURE_START_THROW' }
        Sleep={param($Milliseconds)}
        Cancelled={param($Request) $true}
    }
    $result=Invoke-FleetOrchestratorRequest $request $adapters
    Assert-Fleet ($result.error -eq 'LAUNCH_CANCELLED' -and $result.safeToRetry -and $script:starts -eq 0) 'Cancellation before token checks must not spawn'
    $script:checks=0
    $adapters.Cancelled={param($Request) $script:checks++;return ($script:checks -ge 2)}
    $result=Invoke-FleetOrchestratorRequest $request $adapters
    Assert-Fleet ($result.error -eq 'LAUNCH_CANCELLED' -and $result.safeToRetry -and $script:starts -eq 0) 'Cancellation during native checks must prevent spawn'
    $adapters.Cancelled={param($Request) $false}
    $request.requestedAt=[DateTimeOffset]::UtcNow.AddMinutes(-2).ToString('o')
    $result=Invoke-FleetOrchestratorRequest $request $adapters
    Assert-Fleet ($result.error -eq 'STALE_LAUNCH_REQUEST' -and $result.safeToRetry -and $script:starts -eq 0) 'Stale request must reject without launching'
    $request.requestedAt=[DateTimeOffset]::UtcNow.ToString('o')
    $result=Invoke-FleetOrchestratorRequest $request $adapters
    Assert-Fleet ($result.error -eq 'FIXTURE_START_THROW' -and -not $result.safeToRetry -and $script:starts -eq 1) 'Ambiguous native start must stay uncertain'
    $latchPath=Join-Path $temporary 'emergency-latch.json'
    [IO.File]::WriteAllText($latchPath,'{"enabled":false}')
    Assert-Fleet (-not (Test-FleetLaunchCancelled $request)) 'No cancellation allows the fixed request'
    [IO.File]::WriteAllText((Join-Path $temporary 'orchestrator-launch-cancel.json'),(@{version=1;nonce=$request.nonce;mode='daemon'}|ConvertTo-Json))
    Assert-Fleet (Test-FleetLaunchCancelled $request) 'Matching nonce cancels the native request'
    [IO.File]::WriteAllText($latchPath,'{"enabled":true}')
    $code=$null;try{Test-FleetLaunchCancelled $request|Out-Null}catch{$code=$_.Exception.Message}
    Assert-Fleet ($code -eq 'EMERGENCY_LOCKOUT') 'Emergency latch independently blocks native launch'
    @{ok=$true;checks=$count;nativeStarts=0}|ConvertTo-Json -Compress
}finally{[IO.Directory]::Delete($temporary,$true)}
