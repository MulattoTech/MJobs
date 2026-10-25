<#
Read-only AO v0.14.6 native-runtime-absence preflight. Never starts/stops AO,
reads provider credentials/conversations, connects to host control sockets, or
modifies a registry. A null memory count is not zero. Complete TUI registry
absence follows conpty.Runtime.ProbeFencedRuntime's exact_absent contract; this
is not a claim that every unrelated/orphan/shared provider process has exited.
Chat controller state is independently verified by the caller before and after.
#>
[CmdletBinding()]
param([string]$RequestBase64,[switch]$SelfTest,[switch]$LoadFunctionsOnly)
$ErrorActionPreference='Stop'

function Initialize-FleetRuntimeNative {
    Add-Type -AssemblyName System.Text.Json
    if('FleetRuntimeEvidenceV1.Native' -as [type]){return}
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
namespace FleetRuntimeEvidenceV1 {
 public static class Native {
  [DllImport("shell32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
  static extern IntPtr CommandLineToArgvW(string command,out int count);
  [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
  static extern IntPtr CreateFileW(string path,uint access,uint sharing,IntPtr security,uint disposition,uint flags,IntPtr template);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
  static extern uint GetFinalPathNameByHandleW(IntPtr handle,StringBuilder buffer,uint size,uint flags);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  public static string[] Argv(string command) {
   if(String.IsNullOrWhiteSpace(command))throw new ArgumentException("COMMAND_LINE_UNREADABLE");
   int count; IntPtr memory=CommandLineToArgvW(command,out count);
   if(memory==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
   try {var result=new string[count];for(int i=0;i<count;i++)result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory,i*IntPtr.Size));return result;}
   finally {LocalFree(memory);}
  }
  public static string CanonicalPath(string path) {
   var h=CreateFileW(path,0,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero);
   if(h==new IntPtr(-1))throw new Win32Exception(Marshal.GetLastWin32Error());
   try {var b=new StringBuilder(32768);uint n=GetFinalPathNameByHandleW(h,b,(uint)b.Capacity,0);
    if(n==0||n>=b.Capacity)throw new Win32Exception(Marshal.GetLastWin32Error());
    string value=b.ToString();if(value.StartsWith(@"\\?\UNC\",StringComparison.OrdinalIgnoreCase))value=@"\\"+value.Substring(8);
    else if(value.StartsWith(@"\\?\",StringComparison.Ordinal))value=value.Substring(4);
    return value.TrimEnd('\\').ToLowerInvariant();
   } finally {CloseHandle(h);}
  }
 }
}
'@
}
function Get-FleetTextHash([string]$Text){
    $hash=[Security.Cryptography.SHA256]::Create()
    try{return [Convert]::ToHexString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))).ToLowerInvariant()}
    finally{$hash.Dispose()}
}
function Assert-FleetJsonKeys($Element){
    if($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Object){
        $names=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach($property in $Element.EnumerateObject()){
            if(-not $names.Add($property.Name)){throw 'DUPLICATE_JSON_KEY'}
            Assert-FleetJsonKeys $property.Value
        }
    }elseif($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Array){
        foreach($value in $Element.EnumerateArray()){Assert-FleetJsonKeys $value}
    }
}
function ConvertFrom-FleetStrictJson([string]$Text){
    $document=[System.Text.Json.JsonDocument]::Parse($Text,[System.Text.Json.JsonDocumentOptions]::new())
    try{Assert-FleetJsonKeys $document.RootElement}finally{$document.Dispose()}
    if((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')){
        return ConvertFrom-Json -InputObject $Text -Depth 32 -DateKind String
    }
    return ConvertFrom-Json -InputObject $Text -Depth 32
}
function Read-FleetSmallFile([string]$Path,[int]$MaximumBytes=1048576){
    $stream=$null
    try{
        $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
        if($stream.Length -gt $MaximumBytes){throw 'OWNERSHIP_FILE_TOO_LARGE'}
        $reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$true)
        try{$text=$reader.ReadToEnd()}finally{$reader.Dispose();$stream=$null}
        return [pscustomobject]@{exists=$true;text=$text;hash=(Get-FleetTextHash $text)}
    }catch [IO.FileNotFoundException]{return [pscustomobject]@{exists=$false;text=$null;hash='absent'}}
    catch [IO.DirectoryNotFoundException]{return [pscustomobject]@{exists=$false;text=$null;hash='absent'}}
    finally{if($stream){$stream.Dispose()}}
}
function Read-FleetRegistry([string]$Path){
    $file=Read-FleetSmallFile $Path
    if(-not $file.exists){return [pscustomobject]@{hash='absent';entries=@()}}
    $parsed=ConvertFrom-FleetStrictJson $file.text
    # v0.14.6 registry is a JSON array, not an object with a best-effort fallback.
    if($null -eq $parsed -and $file.text.Trim() -eq '[]'){$parsed=@()}
    if(-not ($file.text.TrimStart().StartsWith('['))){throw 'REGISTRY_FORMAT_INVALID'}
    $entries=@($parsed);$ids=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach($entry in $entries){
        if($null -eq $entry -or -not ($entry.sessionId -is [string]) -or
            $entry.sessionId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' -or
            -not $ids.Add($entry.sessionId) -or -not ($entry.ptyHostPid -is [long] -or $entry.ptyHostPid -is [int]) -or
            $entry.ptyHostPid -lt 0 -or -not ($entry.pipePath -is [string]) -or -not $entry.pipePath){throw 'REGISTRY_ENTRY_INVALID_OR_DUPLICATE'}
        if($entry.ptyHostPid -eq 0 -and $entry.pipePath -ne 'ao-conpty://startup-unresolved'){throw 'REGISTRY_ENTRY_INVALID_OR_DUPLICATE'}
    }
    return [pscustomobject]@{hash=$file.hash;entries=$entries}
}
function Get-FleetChatOwnershipPaths([string]$DataDir,[string]$SessionId){
    $directory=Join-Path (Join-Path $DataDir 'chat-hosts') $SessionId
    return @{lockPath=(Join-Path $directory 'host.lock');descriptorPath=(Join-Path $directory 'host.json')}
}
function Read-FleetChatOwnership([string]$DataDir,[string]$SessionId){
    $paths=Get-FleetChatOwnershipPaths $DataDir $SessionId
    $lock=Read-FleetSmallFile $paths.lockPath 64
    $owner=$null
    if($lock.exists){
        $text=$lock.text.Trim()
        if($text -notmatch '^[1-9][0-9]{0,9}$'){throw 'CHAT_LOCK_INVALID'}
        $owner=[long]$text
        if($owner -gt [uint32]::MaxValue){throw 'CHAT_LOCK_INVALID'}
    }
    # host.json is a capability file: observe file metadata only, never its token.
    $descriptor=$paths.descriptorPath;$descriptorSignature='absent';$descriptorExists=$false
    try{
        $attributes=[IO.File]::GetAttributes($descriptor)
        if(($attributes -band [IO.FileAttributes]::Directory) -ne 0){throw 'CHAT_DESCRIPTOR_NOT_FILE'}
        $info=[IO.FileInfo]::new($descriptor);$info.Refresh()
        $descriptorExists=$true
        $descriptorSignature=([string]$info.Length)+':'+([string]$info.LastWriteTimeUtc.Ticks)
    }catch [IO.FileNotFoundException]{}catch [IO.DirectoryNotFoundException]{}
    return [pscustomobject]@{lockExists=$lock.exists;lockOwnerPid=$owner;descriptorExists=$descriptorExists;
        signature=$lock.hash+':'+$descriptorSignature}
}
function Read-FleetInstanceOwnership($Request,[string]$DataDir){
    # Reading the wrong directory must never turn missing ownership files into
    # absence. Bind the native DataDir's diagnostic lock PID to running.json,
    # the verified live AO executable, and its creation time. No DB contents read.
    $db=Join-Path $DataDir 'ao.db'
    try{
        if(([IO.File]::GetAttributes($db) -band [IO.FileAttributes]::Directory) -ne 0 -or
            [IO.Path]::GetDirectoryName([FleetRuntimeEvidenceV1.Native]::CanonicalPath($db)) -cne $DataDir){throw 'AO_DATA_DIRECTORY_UNVERIFIED'}
    }catch{throw 'AO_DATA_DIRECTORY_UNVERIFIED'}
    $run=Read-FleetSmallFile $Request.runFilePath 65536
    $lockPath=Join-Path $DataDir 'daemon.lock';$lock=Read-FleetSmallFile $lockPath 64
    if(-not $run.exists -or -not $lock.exists){throw 'AO_INSTANCE_OWNERSHIP_UNAVAILABLE'}
    $info=ConvertFrom-FleetStrictJson $run.text;$pidText=$lock.text.Trim()
    if($pidText -notmatch '^[1-9][0-9]{0,9}$' -or
        -not ($info.pid -is [long] -or $info.pid -is [int]) -or $info.pid -le 0 -or
        [long]$pidText -ne $info.pid -or $info.port -ne $Request.expectedPort){throw 'AO_INSTANCE_OWNERSHIP_MISMATCH'}
    $started=[DateTimeOffset]::MinValue
    if(-not [DateTimeOffset]::TryParse([string]$info.startedAt,[ref]$started)){throw 'AO_INSTANCE_TIME_INVALID'}
    $lockInfo=[IO.FileInfo]::new($lockPath);$lockInfo.Refresh()
    if(-not $lockInfo.Exists){throw 'AO_INSTANCE_OWNERSHIP_UNAVAILABLE'}
    return [pscustomobject]@{pid=[long]$info.pid;startedAt=$started;
        lockWrittenAt=[DateTimeOffset]$lockInfo.LastWriteTimeUtc;
        signature=$run.hash+':'+$lock.hash+':'+$lockInfo.LastWriteTimeUtc.Ticks;verified=$false}
}
function Get-FleetHostArguments([string[]]$Arguments){
    if($Arguments.Count -lt 2){return [pscustomobject]@{kind='other';sessionId=$null;dataDir=$null}}
    if($Arguments[1] -ceq 'chat-host'){
        if($Arguments.Count -lt 7 -or $Arguments[2] -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'){throw 'AO_HOST_ARGUMENTS_UNREADABLE'}
        return [pscustomobject]@{kind='chat';sessionId=$Arguments[2];dataDir=$Arguments[3]}
    }
    if($Arguments[1] -ceq 'pty-host'){
        $index=2
        while($index -lt $Arguments.Count -and $Arguments[$index] -cin @('--start=attach','--surface=lazy')){$index++}
        if($Arguments.Count -lt $index+3 -or $Arguments[$index] -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'){throw 'AO_HOST_ARGUMENTS_UNREADABLE'}
        return [pscustomobject]@{kind='tui';sessionId=$Arguments[$index];dataDir=$null}
    }
    return [pscustomobject]@{kind='other';sessionId=$null;dataDir=$null}
}
function Get-FleetProcessInventory([string]$ExpectedExecutable,[object[]]$Targets){
    $all=@(Get-CimInstance -ClassName Win32_Process -Property Name,ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate -OperationTimeoutSec 2 -ErrorAction Stop)
    if($all.Count -eq 0){throw 'PROCESS_INVENTORY_EMPTY'}
    $byPid=@{};$candidates=@();$hosts=@();$ownedProviders=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach($record in $all){
        if($null -eq $record.ProcessId -or -not $record.Name -or $byPid.ContainsKey([long]$record.ProcessId)){throw 'PROCESS_INVENTORY_INCOMPLETE'}
        $byPid[[long]$record.ProcessId]=$record
        $isAo=$record.Name -ieq 'ao.exe' -or $record.Name -ieq ([IO.Path]::GetFileName($ExpectedExecutable))
        $isProvider=$record.Name -iin @('codex.exe','agy.exe','gemini.exe','node.exe','nodew.exe')
        if(-not $isAo -and -not $isProvider){continue}
        if(-not $record.ExecutablePath -or -not $record.CommandLine -or -not $record.CreationDate){throw 'PROCESS_IDENTITY_UNREADABLE'}
        $executable=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($record.ExecutablePath)
        $arguments=[FleetRuntimeEvidenceV1.Native]::Argv($record.CommandLine)
        if($arguments.Count -lt 1){throw 'PROCESS_EXECUTABLE_ARGV_MISMATCH'}
        if([IO.Path]::IsPathRooted($arguments[0])){
            if([FleetRuntimeEvidenceV1.Native]::CanonicalPath($arguments[0]) -cne $executable){throw 'PROCESS_EXECUTABLE_ARGV_MISMATCH'}
        }elseif($arguments[0] -imatch '[/\\]' -or
            [IO.Path]::GetFileNameWithoutExtension($arguments[0]) -ine [IO.Path]::GetFileNameWithoutExtension($record.Name)){
            throw 'PROCESS_EXECUTABLE_ARGV_MISMATCH'
        }
        $created=([DateTimeOffset]$record.CreationDate).ToUniversalTime()
        if($created -gt [DateTimeOffset]::UtcNow.AddSeconds(1)){throw 'PROCESS_CREATION_TIME_INVALID'}
        $creationKey=$created.UtcTicks.ToString([Globalization.CultureInfo]::InvariantCulture)
        $candidate=[pscustomobject]@{pid=[long]$record.ProcessId;parentPid=[long]$record.ParentProcessId;
            executable=$executable;creationKey=$creationKey;createdAt=$created;argvHash=(Get-FleetTextHash ($arguments|ConvertTo-Json -Compress))}
        $candidates+=,$candidate
        if($isAo){
            if($executable -cne $ExpectedExecutable){throw 'UNVERIFIED_AO_EXECUTABLE'}
            $parsed=Get-FleetHostArguments $arguments
            if($parsed.kind -ne 'other'){
                if($parsed.kind -eq 'chat'){$parsed.dataDir=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($parsed.dataDir)}
                $hosts+=,[pscustomobject]@{pid=$candidate.pid;creationKey=$creationKey;createdAt=$created;
                    kind=$parsed.kind;sessionId=$parsed.sessionId;dataDir=$parsed.dataDir;executable=$executable}
            }
        }
        # Additional conservative rejection for explicit session-owned provider
        # or supervisor argv. Absence of these strings is NOT a provider-death proof.
        if($isProvider -or ($isAo -and ($arguments -join ' ') -match '(?i)supervisor')){
            foreach($target in $Targets){
                $pattern='(?<![A-Za-z0-9_.-])'+[regex]::Escape($target.id)+'(?![A-Za-z0-9_.-])'
                if(($arguments|Where-Object {$_ -match $pattern}|Select-Object -First 1)){
                    [void]$ownedProviders.Add($target.id)
                }
            }
        }
    }
    $signature=Get-FleetTextHash (($candidates|Sort-Object pid|Select-Object pid,parentPid,executable,creationKey,argvHash|ConvertTo-Json -Compress -Depth 5))
    return [pscustomobject]@{byPid=$byPid;hosts=$hosts;ownedProviders=$ownedProviders;signature=$signature;complete=$true}
}
function Get-FleetObservation($Request,[string]$Executable,[string]$DataDir){
    $instance=Read-FleetInstanceOwnership $Request $DataDir
    $registry=Read-FleetRegistry $Request.registryPath
    $owners=@{}
    foreach($target in $Request.targets){if($target.mode -eq 'chat'){$owners[$target.id]=Read-FleetChatOwnership $DataDir $target.id}}
    $inventory=Get-FleetProcessInventory $Executable @($Request.targets)
    $daemon=$inventory.byPid[$instance.pid]
    if(-not $daemon -or -not $daemon.ExecutablePath -or -not $daemon.CreationDate -or
        [FleetRuntimeEvidenceV1.Native]::CanonicalPath($daemon.ExecutablePath) -cne $Executable){throw 'AO_INSTANCE_PROCESS_UNVERIFIED'}
    $daemonCreated=([DateTimeOffset]$daemon.CreationDate).ToUniversalTime()
    if($daemonCreated -gt $instance.startedAt.AddSeconds(1) -or
        $instance.lockWrittenAt -lt $daemonCreated.AddSeconds(-1) -or
        $instance.startedAt -gt [DateTimeOffset]::UtcNow.AddSeconds(1)){throw 'AO_INSTANCE_CREATION_IDENTITY_MISMATCH'}
    $instance.verified=$true
    $registryReason=$null
    foreach($entry in $registry.entries){
        if($entry.ptyHostPid -eq 0 -or $entry.pipePath -eq 'ao-conpty://startup-unresolved'){$registryReason='REGISTRY_UNRESOLVED_OWNERSHIP';break}
        if(-not $inventory.byPid.ContainsKey([long]$entry.ptyHostPid)){continue}
        $host=@($inventory.hosts|Where-Object {$_.pid -eq $entry.ptyHostPid -and $_.kind -eq 'tui' -and $_.sessionId -ceq $entry.sessionId})
        if($host.Count -ne 1){$registryReason='REGISTRY_PID_IDENTITY_MISMATCH';break}
        $registered=[DateTimeOffset]::MinValue
        if(-not [DateTimeOffset]::TryParse([string]$entry.registeredAt,[ref]$registered) -or
            $host[0].createdAt -gt $registered.AddSeconds(2) -or -not $entry.launchId){
            $registryReason='REGISTRY_CREATION_IDENTITY_UNVERIFIED';break
        }
    }
    return [pscustomobject]@{registry=$registry;inventory=$inventory;chatOwners=$owners;registryReason=$registryReason;instance=$instance}
}
function Test-FleetTargetAbsence($Target,$Before,$After,[string]$DataDir){
    $proof=[ordered]@{completeInventory=$Before.inventory.complete -eq $true -and $After.inventory.complete -eq $true;
        stableInventory=$Before.inventory.signature -ceq $After.inventory.signature;
        stableRegistry=$Before.registry.hash -ceq $After.registry.hash;
        noMatchingHost=$false;ownershipClear=$false;noSessionOwnedProvider=$false;
        registryAbsent=$false;chatOwnershipClear=$false;
        instanceVerified=$Before.instance.verified -eq $true -and $After.instance.verified -eq $true;
        stableInstance=$Before.instance.signature -ceq $After.instance.signature}
    $reason=$null
    if(-not $proof.instanceVerified){$reason='AO_INSTANCE_OWNERSHIP_UNAVAILABLE'}
    elseif(-not $proof.stableInstance){$reason='AO_INSTANCE_CHANGED'}
    elseif(-not $proof.completeInventory){$reason='PROCESS_INVENTORY_INCOMPLETE'}
    elseif(-not $proof.stableInventory){$reason='PROCESS_INVENTORY_CHANGED'}
    elseif(-not $proof.stableRegistry){$reason='REGISTRY_CHANGED'}
    elseif($Before.registryReason -or $After.registryReason){$reason=if($After.registryReason){$After.registryReason}else{$Before.registryReason}}
    if(-not $reason){
        # Any exact session host blocks, including another data directory or
        # previous UI mode. Do not cross-adopt or ignore conflicting ownership.
        $matching=@(@($Before.inventory.hosts)+@($After.inventory.hosts)|Where-Object {$_.sessionId -ceq $Target.id})
        if($matching.Count){$reason='MATCHING_NATIVE_HOST_PRESENT'}else{$proof.noMatchingHost=$true}
    }
    if(-not $reason){
        if($Before.inventory.ownedProviders.Contains($Target.id) -or $After.inventory.ownedProviders.Contains($Target.id)){$reason='SESSION_OWNED_PROVIDER_PRESENT'}
        else{$proof.noSessionOwnedProvider=$true}
    }
    if(-not $reason){
        $registryEntries=@(@($Before.registry.entries)+@($After.registry.entries)|Where-Object {$_.sessionId -ceq $Target.id})
        $proof.registryAbsent=$registryEntries.Count -eq 0
        if(-not $proof.registryAbsent){$reason='SESSION_REGISTRY_OWNERSHIP_PRESENT'}
    }
    if(-not $reason -and $Target.mode -eq 'chat'){
        $a=$Before.chatOwners[$Target.id];$b=$After.chatOwners[$Target.id]
        if($null -eq $a -or $null -eq $b){$reason='CHAT_OWNERSHIP_UNREADABLE'}
        elseif($a.signature -cne $b.signature){$reason='CHAT_OWNERSHIP_CHANGED'}
        elseif(($a.descriptorExists -and -not $a.lockExists) -or ($b.descriptorExists -and -not $b.lockExists)){$reason='CHAT_DESCRIPTOR_WITHOUT_OWNER_LOCK'}
        elseif(($a.lockExists -and $Before.inventory.byPid.ContainsKey([long]$a.lockOwnerPid)) -or
            ($b.lockExists -and $After.inventory.byPid.ContainsKey([long]$b.lockOwnerPid))){$reason='CHAT_LOCK_OWNER_LIVE_OR_REUSED'}
        else{$proof.chatOwnershipClear=$true}
    }
    if(-not $reason){$proof.ownershipClear=$true}
    return [pscustomobject]@{id=$Target.id;absent=(-not $reason);reason=$reason;proof=$proof}
}
function Invoke-FleetRuntimeEvidence($Request){
    if($Request.version -ne 1 -or $Request.probeId -notmatch '^[a-f0-9]{32}$' -or
        -not $Request.aoExecutable -or -not $Request.aoDataDir -or -not $Request.registryPath -or
        -not $Request.runFilePath -or $Request.expectedPort -lt 1 -or $Request.expectedPort -gt 65535){throw 'REQUEST_INVALID'}
    $targets=@($Request.targets)
    if($targets.Count -lt 1 -or $targets.Count -gt 200){throw 'REQUEST_INVALID'}
    $ids=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach($target in $targets){
        if($target.id -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' -or -not $ids.Add($target.id) -or
            $target.mode -cnotin @('chat','tui')){throw 'REQUEST_INVALID'}
    }
    foreach($value in @($Request.aoExecutable,$Request.aoDataDir,$Request.registryPath,$Request.runFilePath)){
        if(-not [IO.Path]::IsPathFullyQualified($value)){throw 'REQUEST_PATH_INVALID'}
    }
    $executable=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($Request.aoExecutable)
    $dataDir=[FleetRuntimeEvidenceV1.Native]::CanonicalPath($Request.aoDataDir)
    $registryDirectory=[FleetRuntimeEvidenceV1.Native]::CanonicalPath([IO.Path]::GetDirectoryName($Request.runFilePath))
    if([IO.Path]::GetFileName($Request.registryPath) -ine 'windows-pty-hosts.json' -or
        [FleetRuntimeEvidenceV1.Native]::CanonicalPath([IO.Path]::GetDirectoryName($Request.registryPath)) -cne $registryDirectory){throw 'REGISTRY_INSTANCE_PATH_UNVERIFIED'}
    $before=Get-FleetObservation $Request $executable $dataDir
    $after=Get-FleetObservation $Request $executable $dataDir
    $results=@(foreach($target in $targets){Test-FleetTargetAbsence $target $before $after $dataDir})
    return [pscustomobject]@{version=1;probeId=$Request.probeId;complete=$true;observedAt=[DateTimeOffset]::UtcNow.ToString('o');results=$results}
}
function Invoke-FleetRuntimeEvidenceSelfTest {
    $script:checks=0
    function Assert-FleetEvidence($Condition,[string]$Message){if(-not $Condition){throw $Message};$script:checks++}
    function New-TestObservation {
        [pscustomobject]@{registry=[pscustomobject]@{hash='empty';entries=@()};registryReason=$null;
            instance=[pscustomobject]@{verified=$true;signature='instance-one'};
            inventory=[pscustomobject]@{complete=$true;signature='stable';byPid=@{};hosts=@();ownedProviders=[Collections.Generic.HashSet[string]]::new()};
            chatOwners=@{'mjobs-primary'=[pscustomobject]@{signature='absent';lockExists=$false;descriptorExists=$false;lockOwnerPid=$null}}}
    }
    $tui=[pscustomobject]@{id='mjobs-primary';mode='tui'};$chat=[pscustomobject]@{id='mjobs-primary';mode='chat'}
    $a=New-TestObservation;$b=New-TestObservation
    Assert-FleetEvidence ((Test-FleetTargetAbsence $tui $a $b 'x').absent) 'Exact stable TUI registry absence qualifies'
    Assert-FleetEvidence ((Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Stopped caller chat with absent ownership qualifies'
    $b=New-TestObservation;$b.instance.verified=$false
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Wrong or unverified data root is never absence'
    $b=New-TestObservation;$b.instance.signature='replacement-daemon'
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $tui $a $b 'x').absent) 'Replaced daemon instance invalidates evidence'
    foreach($reason in @('REGISTRY_PID_IDENTITY_MISMATCH','REGISTRY_UNRESOLVED_OWNERSHIP','REGISTRY_CREATION_IDENTITY_UNVERIFIED')){
        $b=New-TestObservation;$b.registryReason=$reason
        Assert-FleetEvidence (-not (Test-FleetTargetAbsence $tui $a $b 'x').absent) $reason
    }
    $b=New-TestObservation;$b.registry.hash='changed'
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $tui $a $b 'x').absent) 'Changing registry rejects'
    $b=New-TestObservation;$b.inventory.signature='changed'
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Changing process creation identity rejects'
    $b=New-TestObservation;$b.inventory.complete=$false
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $tui $a $b 'x').absent) 'Partial inventory rejects'
    $b=New-TestObservation;$b.inventory.hosts=@([pscustomobject]@{sessionId='mjobs-primary';kind='chat';dataDir='different'})
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $tui $a $b 'x').absent) 'Other-mode or other-instance host rejects'
    $b=New-TestObservation;[void]$b.inventory.ownedProviders.Add('mjobs-primary')
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Explicit provider ownership rejects'
    $b=New-TestObservation;$b.registry.entries=@([pscustomobject]@{sessionId='mjobs-primary'})
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $tui $a $b 'x').absent) 'Existing registry entry is never absence'
    $b=New-TestObservation;$b.chatOwners['mjobs-primary'].signature='changed'
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Changing chat lock rejects'
    $b=New-TestObservation;$b.chatOwners['mjobs-primary'].descriptorExists=$true
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Descriptor without owner lock stays unknown'
    $a=New-TestObservation;$b=New-TestObservation
    foreach($observation in @($a,$b)){$observation.chatOwners['mjobs-primary'].lockExists=$true;$observation.chatOwners['mjobs-primary'].lockOwnerPid=66}
    Assert-FleetEvidence ((Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Positively absent old owner PID qualifies'
    # Production CIM inventory keys are Int64; PowerShell Hashtable does not
    # equate an Int32 key with Int64, even when their numeric values match.
    $b.inventory.byPid[[long]66]=[pscustomobject]@{Name='RuntimeBroker.exe'}
    Assert-FleetEvidence (-not (Test-FleetTargetAbsence $chat $a $b 'x').absent) 'Reused lock PID is not a dead owner'
    $argv=[FleetRuntimeEvidenceV1.Native]::Argv('"C:\Program Files\AO\ao.exe" chat-host mjobs-primary "C:\User Data\.ao" "C:\Work Space" -- codex.exe app-server')
    $parsed=Get-FleetHostArguments $argv
    Assert-FleetEvidence ($parsed.kind -ceq 'chat' -and $parsed.sessionId -ceq 'mjobs-primary' -and $parsed.dataDir -ceq 'C:\User Data\.ao') 'Native quoted argument parsing'
    $argv=[FleetRuntimeEvidenceV1.Native]::Argv('ao.exe pty-host --start=attach --surface=lazy mjobs-primary "C:\Work Space" codex.exe')
    Assert-FleetEvidence ((Get-FleetHostArguments $argv).sessionId -ceq 'mjobs-primary') 'Positional TUI options are parsed'
    $paths=Get-FleetChatOwnershipPaths 'C:\Users\Dylan\.ao\data' 'mjobs-primary'
    Assert-FleetEvidence ($paths.lockPath -ceq 'C:\Users\Dylan\.ao\data\chat-hosts\mjobs-primary\host.lock') 'Chat lock uses real native data layout'
    Assert-FleetEvidence ($paths.descriptorPath -ceq 'C:\Users\Dylan\.ao\data\chat-hosts\mjobs-primary\host.json') 'Chat descriptor uses data root, not registry root'
    $rejected=$false;try{ConvertFrom-FleetStrictJson '{"sessionId":"one","sessionId":"two"}'|Out-Null}catch{$rejected=$true}
    Assert-FleetEvidence $rejected 'Duplicate JSON properties reject'
    return @{ok=$true;checks=$script:checks;processMutations=0;ownershipWrites=0}
}
if($LoadFunctionsOnly){return}
$probeId=$null
try{
    if([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT){throw 'WINDOWS_REQUIRED'}
    Initialize-FleetRuntimeNative
    if($SelfTest){Invoke-FleetRuntimeEvidenceSelfTest|ConvertTo-Json -Compress -Depth 8;return}
    if(-not $RequestBase64 -or $RequestBase64.Length -gt 524288){throw 'REQUEST_INVALID'}
    $request=ConvertFrom-FleetStrictJson ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($RequestBase64)))
    if($request.probeId -match '^[a-f0-9]{32}$'){$probeId=$request.probeId}
    Invoke-FleetRuntimeEvidence $request|ConvertTo-Json -Compress -Depth 12
}catch{
    # Do not disclose command lines, filesystem content, paths, or native error
    # strings. Known bounded reason codes are enough for a fail-closed dashboard.
    $reason='RUNTIME_EVIDENCE_UNAVAILABLE'
    if($_.Exception.Message -cmatch '^[A-Z][A-Z0-9_]{2,80}$'){$reason=$_.Exception.Message}
    @{version=1;probeId=$probeId;complete=$false;observedAt=[DateTimeOffset]::UtcNow.ToString('o');reason=$reason;results=@()}|ConvertTo-Json -Compress -Depth 5
}
