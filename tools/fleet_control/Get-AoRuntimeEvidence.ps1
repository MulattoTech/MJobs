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
  [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool QueryFullProcessImageNameW(IntPtr handle,uint flags,StringBuilder buffer,ref uint size);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr handle,out long creation,out long exit,out long kernel,out long user);
  [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr handle,int information,IntPtr buffer,uint size,out uint returned);
  public sealed class ProcessIdentity {public string ExecutablePath;public string CommandLine;public DateTimeOffset CreatedAt;}
  // Read-only optional fallback for metadata that CIM withholds across integrity
  // levels. No VM read, debug privilege, elevation, or access-right modification.
  public static ProcessIdentity LimitedProcessIdentity(uint pid) {
   var handle=OpenProcess(0x1000,false,pid);
   if(handle==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
   try {
    uint size=32768;var image=new StringBuilder((int)size);
    if(!QueryFullProcessImageNameW(handle,0,image,ref size))throw new Win32Exception(Marshal.GetLastWin32Error());
    long created,exited,kernel,user;
    if(!GetProcessTimes(handle,out created,out exited,out kernel,out user))throw new Win32Exception(Marshal.GetLastWin32Error());
    uint required;int status=NtQueryInformationProcess(handle,60,IntPtr.Zero,0,out required);
    if(status!=unchecked((int)0xC0000004)||required<16||required>131072)throw new InvalidOperationException("PROCESS_IDENTITY_UNREADABLE");
    var memory=Marshal.AllocHGlobal((int)required);
    try {
     uint returned;status=NtQueryInformationProcess(handle,60,memory,required,out returned);
     if(status<0||returned<16||returned>required)throw new InvalidOperationException("PROCESS_IDENTITY_UNREADABLE");
     ushort length=unchecked((ushort)Marshal.ReadInt16(memory));
     var buffer=Marshal.ReadIntPtr(memory,IntPtr.Size==8?8:4);long offset=buffer.ToInt64()-memory.ToInt64();
     if(length==0||length%2!=0||offset<(IntPtr.Size==8?16:8)||offset+length>returned)throw new InvalidOperationException("PROCESS_IDENTITY_UNREADABLE");
     string command=Marshal.PtrToStringUni(buffer,length/2);
     if(String.IsNullOrWhiteSpace(command)||command.IndexOf('\0')>=0)throw new InvalidOperationException("PROCESS_IDENTITY_UNREADABLE");
     return new ProcessIdentity {ExecutablePath=image.ToString(),CommandLine=command,CreatedAt=new DateTimeOffset(DateTime.FromFileTimeUtc(created))};
    } finally {Marshal.FreeHGlobal(memory);}
   } finally {CloseHandle(handle);}
  }
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
        # shellterm.Service uses this exact option order for its auxiliary
        # terminals. This marker alone does not exempt registry ownership:
        # Get-FleetAuxiliaryShellIdentity also binds the live daemon and PID.
        $shellIndex=2
        if($Arguments[$shellIndex] -ceq '--start=attach'){$shellIndex++}
        $auxiliaryShell=$Arguments.Count -ge ($shellIndex+4) -and
            $Arguments[$shellIndex] -ceq '--surface=lazy' -and
            $Arguments[$shellIndex+1] -cmatch '^shellterm-[0-9a-f]{16}$' -and
            -not [string]::IsNullOrWhiteSpace($Arguments[$shellIndex+2]) -and
            -not [string]::IsNullOrWhiteSpace($Arguments[$shellIndex+3])
        return [pscustomobject]@{kind='tui';sessionId=$Arguments[$index];dataDir=$null;auxiliaryShell=$auxiliaryShell}
    }
    return [pscustomobject]@{kind='other';sessionId=$null;dataDir=$null}
}
function Read-FleetLimitedProcessIdentity([uint32]$ProcessId){
    return [FleetRuntimeEvidenceV1.Native]::LimitedProcessIdentity($ProcessId)
}
function Get-FleetServiceAnchors([hashtable]$ByPid){
    # Only SCM-attested, currently running LocalSystem services in the genuine
    # Windows svchost binary qualify. A process name alone never qualifies.
    $expected=[FleetRuntimeEvidenceV1.Native]::CanonicalPath((Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'svchost.exe'))
    $services=@(Get-CimInstance -ClassName Win32_Service -Property Name,State,ProcessId,StartName,PathName -OperationTimeoutSec 2 -ErrorAction Stop)
    $anchors=@{}
    foreach($service in $services){
        if($service.State -cne 'Running' -or $service.StartName -cne 'LocalSystem' -or $service.ProcessId -le 0 -or
            -not $service.Name -or -not $service.PathName){continue}
        $serviceProcess=$ByPid[[long]$service.ProcessId]
        if(-not $serviceProcess -or $serviceProcess.Name -ine 'svchost.exe' -or -not $serviceProcess.CreationDate){continue}
        $argv=[FleetRuntimeEvidenceV1.Native]::Argv([Environment]::ExpandEnvironmentVariables([string]$service.PathName))
        if($argv.Count -lt 1 -or -not [IO.Path]::IsPathFullyQualified($argv[0]) -or
            [FleetRuntimeEvidenceV1.Native]::CanonicalPath($argv[0]) -cne $expected){continue}
        if($serviceProcess.ExecutablePath -and [FleetRuntimeEvidenceV1.Native]::CanonicalPath($serviceProcess.ExecutablePath) -cne $expected){continue}
        $created=([DateTimeOffset]$serviceProcess.CreationDate).ToUniversalTime()
        if($created -gt [DateTimeOffset]::UtcNow.AddSeconds(1)){throw 'PROCESS_CREATION_TIME_INVALID'}
        $key=[long]$service.ProcessId
        if(-not $anchors.ContainsKey($key)){
            $anchors[$key]=[pscustomobject]@{pid=$key;name='svchost.exe';executable=$expected;creationKey=$created.UtcTicks.ToString([Globalization.CultureInfo]::InvariantCulture);services=@()}
        }
        $anchors[$key].services+=,[pscustomobject]@{name=$service.Name;state=$service.State;account=$service.StartName;argvHash=(Get-FleetTextHash ($argv|ConvertTo-Json -Compress))}
    }
    foreach($anchor in $anchors.Values){$anchor.services=@($anchor.services|Sort-Object name)}
    return $anchors
}
function Get-FleetServiceAncestry($Record,[hashtable]$ByPid,[hashtable]$Anchors,[string]$AoFileName){
    $chain=@();$seen=[Collections.Generic.HashSet[long]]::new();$current=$Record
    for($depth=0;$depth -lt 64;$depth++){
        if(-not $current -or -not $current.Name -or $null -eq $current.ProcessId -or $null -eq $current.ParentProcessId -or
            -not $current.CreationDate -or -not $seen.Add([long]$current.ProcessId)){return $null}
        if($current.Name -ieq 'ao.exe' -or $current.Name -ieq $AoFileName){return $null}
        $created=([DateTimeOffset]$current.CreationDate).ToUniversalTime()
        if($created -gt [DateTimeOffset]::UtcNow.AddSeconds(1)){return $null}
        $creationKey=$created.UtcTicks.ToString([Globalization.CultureInfo]::InvariantCulture)
        $chain+=,[pscustomobject]@{pid=[long]$current.ProcessId;parentPid=[long]$current.ParentProcessId;name=$current.Name;creationKey=$creationKey}
        $anchor=$Anchors[[long]$current.ProcessId]
        if($anchor){
            if($current.Name -ine 'svchost.exe' -or $anchor.name -cne 'svchost.exe' -or
                $anchor.creationKey -cne $creationKey -or -not $anchor.executable -or @($anchor.services).Count -lt 1){return $null}
            return [pscustomobject]@{signature=(Get-FleetTextHash ([ordered]@{chain=$chain;anchor=$anchor}|ConvertTo-Json -Compress -Depth 8));anchorPid=$anchor.pid}
        }
        $parent=$ByPid[[long]$current.ParentProcessId]
        if(-not $parent -or -not $parent.CreationDate -or ([DateTimeOffset]$parent.CreationDate).ToUniversalTime() -gt $created){return $null}
        $current=$parent
    }
    return $null
}
function Get-FleetProcessInventory([string]$ExpectedExecutable,[object[]]$Targets){
    $all=@(Get-CimInstance -ClassName Win32_Process -Property Name,ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate -OperationTimeoutSec 2 -ErrorAction Stop)
    if($all.Count -eq 0){throw 'PROCESS_INVENTORY_EMPTY'}
    $byPid=@{};$candidates=@();$hosts=@();$ownedProviders=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal);$serviceAnchors=$null
    foreach($record in $all){
        if($null -eq $record.ProcessId -or -not $record.Name -or $byPid.ContainsKey([long]$record.ProcessId)){throw 'PROCESS_INVENTORY_INCOMPLETE'}
        $byPid[[long]$record.ProcessId]=$record
    }
    foreach($record in $all){
        $isAo=$record.Name -ieq 'ao.exe' -or $record.Name -ieq ([IO.Path]::GetFileName($ExpectedExecutable))
        $isProvider=$record.Name -iin @('codex.exe','agy.exe','gemini.exe','node.exe','nodew.exe')
        if(-not $isAo -and -not $isProvider){continue}
        if(-not $record.CreationDate){throw 'PROCESS_IDENTITY_UNREADABLE'}
        if(-not $record.ExecutablePath -or -not $record.CommandLine){
            $nativeIdentity=$null
            try{$nativeIdentity=Read-FleetLimitedProcessIdentity ([uint32]$record.ProcessId)}catch{}
            if($nativeIdentity){
                # WMI timestamps have microsecond precision. The native handle
                # must still name that same creation, never a recycled PID.
                $cimTicks=([DateTimeOffset]$record.CreationDate).ToUniversalTime().UtcTicks
                if([Math]::Abs($nativeIdentity.CreatedAt.UtcTicks-$cimTicks) -ge 10){throw 'PROCESS_CREATION_IDENTITY_CHANGED'}
                if(($record.ExecutablePath -and [FleetRuntimeEvidenceV1.Native]::CanonicalPath($record.ExecutablePath) -cne [FleetRuntimeEvidenceV1.Native]::CanonicalPath($nativeIdentity.ExecutablePath)) -or
                    ($record.CommandLine -and $record.CommandLine -cne $nativeIdentity.CommandLine)){throw 'PROCESS_CREATION_IDENTITY_CHANGED'}
                $record=[pscustomobject]@{Name=$record.Name;ProcessId=$record.ProcessId;ParentProcessId=$record.ParentProcessId;
                    CreationDate=$record.CreationDate;ExecutablePath=$nativeIdentity.ExecutablePath;CommandLine=$nativeIdentity.CommandLine}
                $byPid[[long]$record.ProcessId]=$record
            }else{
                if($isAo){throw 'PROCESS_IDENTITY_UNREADABLE'}
                if($null -eq $serviceAnchors){$serviceAnchors=Get-FleetServiceAnchors $byPid}
                $serviceScope=Get-FleetServiceAncestry $record $byPid $serviceAnchors ([IO.Path]::GetFileName($ExpectedExecutable))
                if(-not $serviceScope){throw 'PROCESS_IDENTITY_UNREADABLE'}
                # This provider belongs to an independently attested Windows
                # service tree, not a native AO host. This does not assert the
                # provider is dead or inspect/authorize that service's work.
                # The full ancestry+SCM identity is included in both snapshots.
                $candidates+=,[pscustomobject]@{pid=[long]$record.ProcessId;parentPid=[long]$record.ParentProcessId;
                    executable='scm-service-provider:'+ $record.Name;
                    creationKey=([DateTimeOffset]$record.CreationDate).ToUniversalTime().UtcTicks.ToString([Globalization.CultureInfo]::InvariantCulture);
                    argvHash=$serviceScope.signature}
                continue
            }
        }
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
                    kind=$parsed.kind;sessionId=$parsed.sessionId;dataDir=$parsed.dataDir;executable=$executable;auxiliaryShell=($parsed.auxiliaryShell -eq $true)}
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
function Get-FleetAuxiliaryShellIdentity($Entry,$RuntimeHost,$Inventory,$Instance,[string]$Executable){
    # AO e424538 shellterm.Service creates a separate non-agent runtime with
    # an eight-byte random hex ID and no AO_RUNTIME_LAUNCH_ID. Do not ignore it:
    # validate its exact native registry shape, argv, image, creation and direct
    # daemon parent. Unknown/malformed entries remain a global evidence blocker.
    $keys=@($Entry.PSObject.Properties.Name)
    $expectedKeys=@('sessionId','ptyHostPid','pipePath','registeredAt')
    if($keys.Count -ne $expectedKeys.Count -or @($keys|Where-Object {$_ -cnotin $expectedKeys}).Count -ne 0 -or
        $Entry.sessionId -cnotmatch '^shellterm-[0-9a-f]{16}$' -or
        -not ($Entry.registeredAt -is [string]) -or $Entry.registeredAt -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$' -or
        $RuntimeHost.kind -cne 'tui' -or $RuntimeHost.sessionId -cne $Entry.sessionId -or
        $RuntimeHost.pid -ne $Entry.ptyHostPid -or $RuntimeHost.auxiliaryShell -ne $true -or
        $RuntimeHost.executable -cne $Executable){return $false}
    $address=[regex]::Match([string]$Entry.pipePath,'^127\.0\.0\.1:([1-9][0-9]{0,4})$')
    if(-not $address.Success -or [int]$address.Groups[1].Value -gt 65535){return $false}
    $record=$Inventory.byPid[[long]$Entry.ptyHostPid]
    $daemonRecord=$Inventory.byPid[[long]$Instance.pid]
    if(-not $record -or -not $daemonRecord -or -not $record.CreationDate -or -not $daemonRecord.CreationDate -or
        $record.ParentProcessId -ne $Instance.pid -or $record.ProcessId -ne $Entry.ptyHostPid){return $false}
    $created=([DateTimeOffset]$record.CreationDate).ToUniversalTime()
    $daemonCreated=([DateTimeOffset]$daemonRecord.CreationDate).ToUniversalTime()
    $registered=[DateTimeOffset]::MinValue
    if(-not [DateTimeOffset]::TryParse([string]$Entry.registeredAt,[ref]$registered) -or
        $created -ne $RuntimeHost.createdAt -or $created -lt $daemonCreated -or
        $registered -lt $daemonCreated.AddSeconds(-1) -or $registered -gt [DateTimeOffset]::UtcNow.AddSeconds(1) -or
        $created -gt $registered.AddSeconds(2)){return $false}
    return $true
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
        $runtimeHostMatches=@($inventory.hosts|Where-Object {$_.pid -eq $entry.ptyHostPid -and $_.kind -eq 'tui' -and $_.sessionId -ceq $entry.sessionId})
        if($runtimeHostMatches.Count -ne 1){$registryReason='REGISTRY_PID_IDENTITY_MISMATCH';break}
        $registered=[DateTimeOffset]::MinValue
        if(-not [DateTimeOffset]::TryParse([string]$entry.registeredAt,[ref]$registered) -or
            $runtimeHostMatches[0].createdAt -gt $registered.AddSeconds(2)){
            $registryReason='REGISTRY_CREATION_IDENTITY_UNVERIFIED';break
        }
        if(-not $entry.launchId -and -not (Get-FleetAuxiliaryShellIdentity $entry $runtimeHostMatches[0] $inventory $instance $Executable)){
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
    # Exercise the production observation's live-registry branch. Empty-registry
    # fixtures never reached it and missed an assignment to PowerShell's $Host
    # constant after the first TUI had resumed. Only dependency reads are mocked;
    # Get-FleetObservation and its creation/ownership validation run unchanged.
    & {
        $fixtureNow=[DateTimeOffset]::UtcNow
        $fixtureExecutable=[FleetRuntimeEvidenceV1.Native]::CanonicalPath((Join-Path $PSHOME 'pwsh.exe'))
        $fixtureInstance=[pscustomobject]@{pid=[long]90101;startedAt=$fixtureNow.AddMinutes(-3);
            lockWrittenAt=$fixtureNow.AddMinutes(-3);signature='fixture-instance';verified=$false}
        $fixtureEntry=[pscustomobject]@{sessionId='mjobs-running-primary';ptyHostPid=[long]90102;
            pipePath='fixture-pipe';registeredAt=$fixtureNow.AddMinutes(-1).ToString('o');launchId='fixture-launch'}
        $fixtureHost=[pscustomobject]@{pid=[long]90102;kind='tui';sessionId='mjobs-running-primary';
            createdAt=$fixtureNow.AddMinutes(-1)}
        $fixtureRegistry=[pscustomobject]@{hash='fixture-registry';entries=@($fixtureEntry)}
        $fixtureInventory=[pscustomobject]@{complete=$true;signature='fixture-processes';
            byPid=@{([long]90101)=[pscustomobject]@{ExecutablePath=$fixtureExecutable;CreationDate=$fixtureNow.AddMinutes(-4)};
                    ([long]90102)=[pscustomobject]@{Name='ao.exe'}};
            hosts=@($fixtureHost);ownedProviders=[Collections.Generic.HashSet[string]]::new()}
        function Read-FleetInstanceOwnership { return $fixtureInstance }
        function Read-FleetRegistry { return $fixtureRegistry }
        function Get-FleetProcessInventory { return $fixtureInventory }
        $fixtureWorker=[pscustomobject]@{id='mjobs-stopped-worker';mode='tui'}
        $fixtureRequest=[pscustomobject]@{registryPath='fixture-registry';targets=@($fixtureWorker)}
        $observation=Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data'
        Assert-FleetEvidence ($observation.instance.verified -and -not $observation.registryReason) 'Production observation accepts a verified live TUI registry entry'
        Assert-FleetEvidence ((Test-FleetTargetAbsence $fixtureWorker $observation $observation 'fixture-data').absent) 'A valid running primary does not prevent absence proof for a stopped worker'
        $fixtureHost.sessionId='different-session'
        Assert-FleetEvidence ((Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data').registryReason -eq 'REGISTRY_PID_IDENTITY_MISMATCH') 'Production registry loop rejects a reused PID with another session identity'
        $fixtureHost.sessionId='mjobs-running-primary';$fixtureHost.createdAt=$fixtureNow
        Assert-FleetEvidence ((Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data').registryReason -eq 'REGISTRY_CREATION_IDENTITY_UNVERIFIED') 'Production registry loop rejects a host created after the registered launch'
        $fixtureHost.createdAt=$fixtureNow.AddMinutes(-1);$fixtureEntry.launchId=$null
        Assert-FleetEvidence ((Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data').registryReason -eq 'REGISTRY_CREATION_IDENTITY_UNVERIFIED') 'Production registry loop rejects a missing launch identity'
        $fixtureEntry.launchId='fixture-launch';$fixtureEntry.ptyHostPid=[long]0;$fixtureEntry.pipePath='ao-conpty://startup-unresolved'
        Assert-FleetEvidence ((Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data').registryReason -eq 'REGISTRY_UNRESOLVED_OWNERSHIP') 'Production registry loop preserves unresolved-start protection'
    }
    & {
        $fixtureNow=[DateTimeOffset]::UtcNow
        $fixtureExecutable=[FleetRuntimeEvidenceV1.Native]::CanonicalPath((Join-Path $PSHOME 'pwsh.exe'))
        $fixtureAuxiliarySessionId='shellterm-f20175ce27993693'
        $shellArgs=@('ao.exe','pty-host','--surface=lazy',$fixtureAuxiliarySessionId,'C:\work','gh.exe','status')
        Assert-FleetEvidence ((Get-FleetHostArguments $shellArgs).auxiliaryShell -eq $true) 'Auxiliary shell argv uses the exact native lazy-surface contract'
        Assert-FleetEvidence ((Get-FleetHostArguments @('ao.exe','pty-host','--start=attach','--surface=lazy',$fixtureAuxiliarySessionId,'C:\work','gh.exe')).auxiliaryShell -eq $true) 'Deferred auxiliary shell argv follows the same native contract'
        $noncanonicalArgs=[ordered]@{
            'No lazy surface'=@('ao.exe','pty-host',$fixtureAuxiliarySessionId,'C:\work','gh.exe')
            'Options in another order'=@('ao.exe','pty-host','--surface=lazy','--start=attach',$fixtureAuxiliarySessionId,'C:\work','gh.exe')
            'Duplicate surface option'=@('ao.exe','pty-host','--surface=lazy','--surface=lazy',$fixtureAuxiliarySessionId,'C:\work','gh.exe')
            'Uppercase random ID'=@('ao.exe','pty-host','--surface=lazy','shellterm-F20175CE27993693','C:\work','gh.exe')
            'Wrong random ID length'=@('ao.exe','pty-host','--surface=lazy','shellterm-short','C:\work','gh.exe')
        }
        foreach($invalidArgs in $noncanonicalArgs.Values){
            Assert-FleetEvidence (-not (Get-FleetHostArguments $invalidArgs).auxiliaryShell) 'Noncanonical auxiliary shell argv never qualifies'
        }
        function New-AuxiliaryShellFixture {
            # Native registry RFC3339 is second precision; host creation retains
            # a subsecond fraction. Both identify the same currently live PID.
            $registered=[DateTimeOffset]::Parse($fixtureNow.AddMinutes(-1).ToString('yyyy-MM-ddTHH:mm:ssZ'))
            $created=$registered.AddMilliseconds(331)
            $entry=[pscustomobject]@{sessionId=$fixtureAuxiliarySessionId;ptyHostPid=[long]90202;
                pipePath='127.0.0.1:54321';registeredAt=$registered.ToString('yyyy-MM-ddTHH:mm:ssZ')}
            $runtime=[pscustomobject]@{pid=[long]90202;kind='tui';sessionId=$fixtureAuxiliarySessionId;createdAt=$created;
                executable=$fixtureExecutable;auxiliaryShell=(Get-FleetHostArguments $shellArgs).auxiliaryShell}
            return [pscustomobject]@{
                instance=[pscustomobject]@{pid=[long]90201;startedAt=$fixtureNow.AddMinutes(-3);
                    lockWrittenAt=$fixtureNow.AddMinutes(-3);signature='auxiliary-instance';verified=$false};
                registry=[pscustomobject]@{hash='auxiliary-registry';entries=@($entry)};
                inventory=[pscustomobject]@{complete=$true;signature='auxiliary-processes';
                    byPid=@{([long]90201)=[pscustomobject]@{ExecutablePath=$fixtureExecutable;CreationDate=$fixtureNow.AddMinutes(-4)};
                        ([long]90202)=[pscustomobject]@{Name='ao.exe';ProcessId=[long]90202;ParentProcessId=[long]90201;CreationDate=$created}};
                    hosts=@($runtime);ownedProviders=[Collections.Generic.HashSet[string]]::new()}}
        }
        function Read-FleetInstanceOwnership { return $fixtureAux.instance }
        function Read-FleetRegistry { return $fixtureAux.registry }
        function Get-FleetProcessInventory { return $fixtureAux.inventory }
        $fixtureWorker=[pscustomobject]@{id='mjobs-stopped-worker';mode='tui'}
        $fixtureRequest=[pscustomobject]@{registryPath='fixture-registry';targets=@($fixtureWorker)}
        $fixtureAux=New-AuxiliaryShellFixture
        $observation=Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data'
        Assert-FleetEvidence (-not $observation.registryReason) 'Production registry validation recognizes a live daemon-owned auxiliary shell without a launchId'
        Assert-FleetEvidence ((Test-FleetTargetAbsence $fixtureWorker $observation $observation 'fixture-data').absent) 'Validated auxiliary shell does not block a different stopped agent'
        $shellTarget=[pscustomobject]@{id=$fixtureAuxiliarySessionId;mode='tui'}
        Assert-FleetEvidence (-not (Test-FleetTargetAbsence $shellTarget $observation $observation 'fixture-data').absent) 'An exact matching auxiliary target remains present, never absent'
        $invalidShells=[ordered]@{
            'Unfenced ordinary agent'={param($x)$x.registry.entries[0].sessionId='mjobs-agent';$x.inventory.hosts[0].sessionId='mjobs-agent'}
            'Unrecognized shell ID'={param($x)$x.registry.entries[0].sessionId='shellterm-invalid';$x.inventory.hosts[0].sessionId='shellterm-invalid'}
            'Changed argv mode'={param($x)$x.inventory.hosts[0].auxiliaryShell=$false}
            'Different executable'={param($x)$x.inventory.hosts[0].executable='different-executable'}
            'Not a direct daemon child'={param($x)$x.inventory.byPid[[long]90202].ParentProcessId=[long]91999}
            'Missing parent metadata'={param($x)$x.inventory.byPid[[long]90202].PSObject.Properties.Remove('ParentProcessId')}
            'Missing host creation'={param($x)$x.inventory.byPid[[long]90202].CreationDate=$null}
            'Reused host creation'={param($x)$x.inventory.byPid[[long]90202].CreationDate=$fixtureNow}
            'Host predates daemon'={param($x)$x.inventory.hosts[0].createdAt=$fixtureNow.AddMinutes(-5);$x.inventory.byPid[[long]90202].CreationDate=$fixtureNow.AddMinutes(-5)}
            'Wrong process record identity'={param($x)$x.inventory.byPid[[long]90202].ProcessId=[long]91999}
            'Explicit null launch ID'={param($x)$x.registry.entries[0]|Add-Member -NotePropertyName launchId -NotePropertyValue $null}
            'Unrecognized registry property'={param($x)$x.registry.entries[0]|Add-Member -NotePropertyName unknownField -NotePropertyValue 'unverified'}
            'Nonloopback control address'={param($x)$x.registry.entries[0].pipePath='192.0.2.1:54321'}
            'Hostname control address'={param($x)$x.registry.entries[0].pipePath='localhost:54321'}
            'Invalid control port'={param($x)$x.registry.entries[0].pipePath='127.0.0.1:65536'}
            'Missing control port'={param($x)$x.registry.entries[0].pipePath='127.0.0.1:0'}
            'Unexpected address suffix'={param($x)$x.registry.entries[0].pipePath='127.0.0.1:54321/extra'}
            'Future registry time'={param($x)$x.registry.entries[0].registeredAt=$fixtureNow.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ssZ')}
            'Missing registry time'={param($x)$x.registry.entries[0].registeredAt=$null}
            'Non-native time zone format'={param($x)$x.registry.entries[0].registeredAt=$fixtureNow.AddMinutes(-1).ToString('yyyy-MM-ddTHH:mm:ss+00:00')}
            'Non-native fractional time format'={param($x)$x.registry.entries[0].registeredAt=$fixtureNow.AddMinutes(-1).ToString('o')}
            'Registry before host birth'={param($x)$x.registry.entries[0].registeredAt=$fixtureNow.AddMinutes(-2).ToString('yyyy-MM-ddTHH:mm:ssZ')}
            'Reused host PID'={param($x)$x.inventory.hosts[0].sessionId='other-session'}
            'Pending native reservation'={param($x)$x.registry.entries[0].ptyHostPid=[long]0;$x.registry.entries[0].pipePath='ao-conpty://startup-unresolved'}
        }
        foreach($invalidCase in $invalidShells.GetEnumerator()){
            $fixtureAux=New-AuxiliaryShellFixture
            & $invalidCase.Value $fixtureAux
            $rejectedObservation=Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data'
            Assert-FleetEvidence ([bool]$rejectedObservation.registryReason) ($invalidCase.Key+' remains an unknown registry owner')
            Assert-FleetEvidence (-not (Test-FleetTargetAbsence $fixtureWorker $rejectedObservation $rejectedObservation 'fixture-data').absent) ($invalidCase.Key+' cannot prove agent absence')
        }
        $fixtureAux=New-AuxiliaryShellFixture
        $after=Get-FleetObservation $fixtureRequest $fixtureExecutable 'fixture-data'
        $after.inventory.signature='changed-auxiliary-host'
        Assert-FleetEvidence (-not (Test-FleetTargetAbsence $fixtureWorker $observation $after 'fixture-data').absent) 'An auxiliary host changed between inventories still invalidates absence'
    }
    & {
        $fixtureNow=[DateTimeOffset]::UtcNow
        $provider=[pscustomobject]@{Name='node.exe';ProcessId=[long]91003;ParentProcessId=[long]91002;CreationDate=$fixtureNow.AddMinutes(-1)}
        $wrapper=[pscustomobject]@{Name='agy.exe';ProcessId=[long]91002;ParentProcessId=[long]91001;CreationDate=$fixtureNow.AddMinutes(-2)}
        $serviceProcess=[pscustomobject]@{Name='svchost.exe';ProcessId=[long]91001;ParentProcessId=[long]99999;CreationDate=$fixtureNow.AddMinutes(-3)}
        $records=@{([long]91003)=$provider;([long]91002)=$wrapper;([long]91001)=$serviceProcess}
        $anchor=[pscustomobject]@{pid=[long]91001;name='svchost.exe';executable='verified-windows-svchost';
            creationKey=$fixtureNow.AddMinutes(-3).UtcTicks.ToString([Globalization.CultureInfo]::InvariantCulture);
            services=@([pscustomobject]@{name='Schedule';state='Running';account='LocalSystem';argvHash='verified-service-command'})}
        $anchors=@{([long]91001)=$anchor}
        $scope=Get-FleetServiceAncestry $provider $records $anchors 'ao.exe'
        Assert-FleetEvidence ([bool]$scope) 'Opaque provider can be scoped to an attested live Windows service without relying on exited wininit parent'
        $anchor.services[0].argvHash='changed-service-command'
        Assert-FleetEvidence ((Get-FleetServiceAncestry $provider $records $anchors 'ao.exe').signature -cne $scope.signature) 'SCM changes invalidate the two-snapshot identity'
        $wrapper.Name='ao.exe'
        Assert-FleetEvidence (-not (Get-FleetServiceAncestry $provider $records $anchors 'ao.exe')) 'AO ancestry never qualifies as an unrelated service provider'
        $wrapper.Name='agy.exe';$wrapper.CreationDate=$fixtureNow
        Assert-FleetEvidence (-not (Get-FleetServiceAncestry $provider $records $anchors 'ao.exe')) 'Reused parent PID created after the child remains unknown'
        $wrapper.CreationDate=$fixtureNow.AddMinutes(-2);$wrapper.ParentProcessId=[long]91999
        Assert-FleetEvidence (-not (Get-FleetServiceAncestry $provider $records $anchors 'ao.exe')) 'Missing intermediary parent remains unknown'
        $wrapper.ParentProcessId=[long]91003
        Assert-FleetEvidence (-not (Get-FleetServiceAncestry $provider $records $anchors 'ao.exe')) 'Cyclic ancestry remains unknown'
        $wrapper.ParentProcessId=[long]91001;$anchor.creationKey='reused-service-pid'
        Assert-FleetEvidence (-not (Get-FleetServiceAncestry $provider $records $anchors 'ao.exe')) 'Service anchor creation must match the process inventory'
        $anchor.creationKey=$fixtureNow.AddMinutes(-3).UtcTicks.ToString([Globalization.CultureInfo]::InvariantCulture)
        Assert-FleetEvidence (-not (Get-FleetServiceAncestry $provider $records @{} 'ao.exe')) 'No SCM anchor is not an unrelated-provider proof'
        $nativeIdentity=Read-FleetLimitedProcessIdentity ([uint32]$PID)
        Assert-FleetEvidence ($nativeIdentity.ExecutablePath -and $nativeIdentity.CommandLine -and $nativeIdentity.CreatedAt -le $fixtureNow) 'Read-only limited native identity works for the test process'
        & {
            $fixtureService=[pscustomobject]@{Name='Schedule';State='Running';ProcessId=[long]91001;StartName='LocalSystem';
                PathName=('"'+(Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'svchost.exe')+'" -k netsvcs -p')}
            function Get-CimInstance { return $fixtureService }
            Assert-FleetEvidence ((Get-FleetServiceAnchors $records).Count -eq 1) 'Production SCM reader accepts the running Windows service identity'
            $fixtureService.PathName='"'+(Join-Path $PSHOME 'pwsh.exe')+'"'
            Assert-FleetEvidence ((Get-FleetServiceAnchors $records).Count -eq 0) 'Production SCM reader rejects service executables outside Windows svchost'
            $fixtureService.PathName='"'+(Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::System)) 'svchost.exe')+'"';$fixtureService.StartName='OtherAccount'
            Assert-FleetEvidence ((Get-FleetServiceAnchors $records).Count -eq 0) 'Production SCM reader rejects a different service account'
            $fixtureService.StartName='LocalSystem';$fixtureService.State='Stopped'
            Assert-FleetEvidence ((Get-FleetServiceAnchors $records).Count -eq 0) 'Production SCM reader requires current running status'
        }
    }
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
