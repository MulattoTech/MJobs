<# Read-only ConPTY status evidence. Sends only MsgStatusReq (0x06).
Never kills a host, writes ownership/DB state, or supplies terminal input. #>
[CmdletBinding()]
param([string]$RequestBase64)
$ErrorActionPreference='Stop'
$savedRequest=$RequestBase64
. (Join-Path $PSScriptRoot 'Get-AoRuntimeEvidence.ps1') -LoadFunctionsOnly
function Read-ExitedHostStatus([string]$Address){
 if($Address -cnotmatch '^127\.0\.0\.1:([1-9][0-9]{0,4})$'){throw 'NON_LOOPBACK_HOST'}
 $portNumber=[int]$Matches[1];if($portNumber -gt 65535){throw 'INVALID_PORT'}
 $client=[Net.Sockets.TcpClient]::new()
 try{
  if(-not $client.ConnectAsync('127.0.0.1',$portNumber).Wait(1200)){throw 'STATUS_TIMEOUT'}
  $stream=$client.GetStream();$stream.ReadTimeout=1200;$stream.WriteTimeout=1200
  $stream.Write([byte[]]@(6,0,0,0,0),0,5)
  $received=0;$deadline=[DateTimeOffset]::UtcNow.AddSeconds(2)
  while([DateTimeOffset]::UtcNow -lt $deadline){
   $header=[byte[]]::new(5);$offset=0
   while($offset -lt 5){$n=$stream.Read($header,$offset,5-$offset);if($n -eq 0){throw 'HOST_CLOSED'};$offset+=$n}
   $size=([long]$header[1]*16777216)+([long]$header[2]*65536)+([long]$header[3]*256)+[long]$header[4]
   $received+=$size+5;if($received -gt 16777216 -or ($header[0] -eq 7 -and $size -gt 4096)){throw 'STATUS_TOO_LARGE'}
   # New connections replay scrollback first. Discard it in fixed-size chunks;
   # never retain, parse or publish terminal output in an identity probe.
   if($header[0] -ne 7){
    $scratch=[byte[]]::new(8192);$left=$size
    while($left -gt 0){if([DateTimeOffset]::UtcNow -ge $deadline){throw 'STATUS_TIMEOUT'};$n=$stream.Read($scratch,0,[int][Math]::Min($left,8192));if($n -eq 0){throw 'HOST_CLOSED'};$left-=$n}
    continue
   }
   $body=[byte[]]::new([int]$size);$offset=0
   while($offset -lt $size){$n=$stream.Read($body,$offset,[int]$size-$offset);if($n -eq 0){throw 'HOST_CLOSED'};$offset+=$n}
   return ConvertFrom-FleetStrictJson ([Text.Encoding]::UTF8.GetString($body))
  }
  throw 'STATUS_TIMEOUT'
 }finally{$client.Dispose()}
}
function Get-ExitedHostObservation($Target,$Observation){
 if($Observation.registryReason -or !$Observation.inventory.complete -or !$Observation.instance.verified){throw 'INCOMPLETE_OWNERSHIP'}
 $entries=@($Observation.registry.entries|Where-Object sessionId -ceq $Target.id)
 $hosts=@($Observation.inventory.hosts|Where-Object sessionId -ceq $Target.id)
 if($entries.Count -ne 1 -or $hosts.Count -ne 1){throw 'AMBIGUOUS_HOST'}
 $entry=$entries[0];$runtimeHost=$hosts[0]
 if($runtimeHost.kind -cne 'tui' -or $runtimeHost.auxiliaryShell -or $entry.ptyHostPid -ne $runtimeHost.pid -or
  $entry.launchId -cne $Target.terminalGeneration -or $entry.pipePath -cnotmatch '^127\.0\.0\.1:([1-9][0-9]{0,4})$'){throw 'HOST_IDENTITY_MISMATCH'}
 $portNumber=[int]$Matches[1];if($portNumber -gt 65535){throw 'INVALID_PORT'}
 $process=$Observation.inventory.byPid[[long]$runtimeHost.pid]
 $argv=[FleetRuntimeEvidenceV1.Native]::Argv($process.CommandLine);$index=[Array]::IndexOf($argv,'--launch')
 if($index -lt 0 -or $index+1 -ge $argv.Count -or $argv[$index+1] -cne $entry.launchId){throw 'HOST_LAUNCH_MISMATCH'}
 $children=@($Observation.inventory.byPid.Values|Where-Object {$_.ParentProcessId -eq $runtimeHost.pid -and $_.CreationDate -and ([DateTimeOffset]$_.CreationDate) -ge $runtimeHost.createdAt})
 if($children.Count -gt 0 -or $Observation.inventory.ownedProviders.Contains($Target.id)){throw 'PROVIDER_STILL_PRESENT'}
 $listeners=@(Get-NetTCPConnection -State Listen -LocalPort $portNumber -ErrorAction Stop)
 if($listeners.Count -ne 1 -or $listeners[0].LocalAddress -cne '127.0.0.1' -or $listeners[0].OwningProcess -ne $runtimeHost.pid){throw 'LISTENER_OWNER_MISMATCH'}
 $status=Read-ExitedHostStatus $entry.pipePath
 if($status.alive -isnot [bool] -or $status.alive -ne $false -or $status.protocolVersion -ne 2 -or
  !($status.exitCode -is [long] -or $status.exitCode -is [int]) -or
  !($status.pid -is [long] -or $status.pid -is [int]) -or $status.pid -le 0 -or $Observation.inventory.byPid.ContainsKey([long]$status.pid)){throw 'CHILD_EXIT_UNVERIFIED'}
 return [ordered]@{hostPid=[long]$runtimeHost.pid;hostCreatedAt=$runtimeHost.createdAt.ToUniversalTime().ToString('o');launchId=$entry.launchId;childPid=[long]$status.pid;exitCode=[long]$status.exitCode;protocolVersion=2;address=$entry.pipePath}
}
try{
 Initialize-FleetRuntimeNative
 if(!$savedRequest -or $savedRequest.Length -gt 131072){throw 'INVALID_REQUEST'}
 $request=ConvertFrom-FleetStrictJson ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($savedRequest)))
 if($request.version -ne 1 -or $request.probeId -cnotmatch '^[a-f0-9]{32}$' -or @($request.targets).Count -lt 1 -or @($request.targets).Count -gt 32){throw 'INVALID_REQUEST'}
 foreach($target in $request.targets){if($target.mode -cne 'tui' -or $target.isTerminated -ne $false -or $target.activity -cne 'exited' -or $target.statusReadiness -cne 'ready' -or $target.provisionState -cne 'ready' -or $target.hibernatedAt -or $target.terminalGeneration -cnotmatch '^[a-f0-9-]{36}$'){throw 'INELIGIBLE_TARGET'}}
 $exe=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($request.aoExecutable);$dataDir=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($request.aoDataDir)
 $before=Get-FleetObservation $request $exe $dataDir;$first=@{};$failures=@{}
 foreach($target in $request.targets){try{$first[$target.id]=Get-ExitedHostObservation $target $before}catch{$failures[$target.id]=if($_.Exception.Message -cmatch '^[A-Z_]{3,80}$'){$_.Exception.Message}else{'HOST_INSPECTION_ERROR'}}}
 $after=Get-FleetObservation $request $exe $dataDir;$results=@()
 foreach($target in $request.targets){
  try{
   if(!$first.ContainsKey($target.id) -or $before.registry.hash -cne $after.registry.hash -or $before.instance.signature -cne $after.instance.signature -or $before.inventory.signature -cne $after.inventory.signature){throw 'UNSTABLE_OBSERVATIONS'}
   $second=Get-ExitedHostObservation $target $after
   if(($first[$target.id]|ConvertTo-Json -Compress) -cne ($second|ConvertTo-Json -Compress)){throw 'HOST_CHANGED'}
   $results+=[ordered]@{id=$target.id;exited=$true;hostPid=$second.hostPid;hostCreatedAt=$second.hostCreatedAt;launchId=$second.launchId;exitCode=$second.exitCode;proof=@{stableInventory=$true;stableRegistry=$true;stableInstance=$true;listenerOwned=$true;exactGeneration=$true;noProviderChildren=$true;nativeExitConfirmed=$true}}
  }catch{$why=if($failures.ContainsKey($target.id)){$failures[$target.id]}elseif($_.Exception.Message -cmatch '^[A-Z_]{3,80}$'){$_.Exception.Message}else{'HOST_INSPECTION_ERROR'};$results+=[ordered]@{id=$target.id;exited=$false;reason=$why}}
 }
 [ordered]@{version=1;probeId=$request.probeId;complete=$true;observedAt=[DateTimeOffset]::UtcNow.ToString('o');results=$results}|ConvertTo-Json -Compress -Depth 8
}catch{[ordered]@{version=1;probeId=$request.probeId;complete=$false;reason='EXITED_HOST_PROBE_FAILED'}|ConvertTo-Json -Compress}
