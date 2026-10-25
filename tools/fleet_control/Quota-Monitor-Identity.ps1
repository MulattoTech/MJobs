# Read-only verification for the one isolated, guard-owned quota monitor.
function Initialize-FleetIdentityNative {
 if ('FleetQuotaIdentity.Native' -as [type]) { return }
 Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace FleetQuotaIdentity {
 public static class Native {
  [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  private static extern IntPtr CommandLineToArgvW(string commandLine,out int argumentCount);
  [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  private static extern SafeFileHandle CreateFileW(string name,uint access,uint share,IntPtr security,uint creation,uint flags,IntPtr template);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle,StringBuilder path,uint length,uint flags);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] private static extern uint GetDriveTypeW(string root);
  public static string[] Argv(string commandLine) {
   if(String.IsNullOrWhiteSpace(commandLine)||commandLine.Length>32767||commandLine.IndexOf('\0')>=0)throw new ArgumentException("Invalid command line");
   int count; IntPtr pointer=CommandLineToArgvW(commandLine,out count);
   if(pointer==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
   try {string[] result=new string[count];for(int i=0;i<count;i++)result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer,i*IntPtr.Size));return result;}
   finally {LocalFree(pointer);}
  }
  public static string FinalLocalFilePath(string path) {
   if(String.IsNullOrWhiteSpace(path)||path.Length<3||!Char.IsLetter(path[0])||path[1]!=':'||path[2]!='\\')throw new ArgumentException("Absolute local path required");
   uint drive=GetDriveTypeW(path.Substring(0,3));
   if(drive!=2&&drive!=3&&drive!=6)throw new ArgumentException("Local volume required");
   using(SafeFileHandle handle=CreateFileW(path,0,7,IntPtr.Zero,3,0x80,IntPtr.Zero)) {
    if(handle.IsInvalid)throw new Win32Exception(Marshal.GetLastWin32Error());
    StringBuilder buffer=new StringBuilder(512);
    uint length=GetFinalPathNameByHandleW(handle,buffer,(uint)buffer.Capacity,0);
    if(length==0)throw new Win32Exception(Marshal.GetLastWin32Error());
    if(length>=buffer.Capacity) {
     if(length>32767)throw new ArgumentException("Final path too long");
     buffer=new StringBuilder((int)length+1);
     length=GetFinalPathNameByHandleW(handle,buffer,(uint)buffer.Capacity,0);
     if(length==0||length>=buffer.Capacity)throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    string finalPath=buffer.ToString();
    if(finalPath.StartsWith(@"\\?\UNC\",StringComparison.OrdinalIgnoreCase))throw new ArgumentException("Network paths are not allowed");
    if(finalPath.StartsWith(@"\\?\",StringComparison.Ordinal))finalPath=finalPath.Substring(4);
    if(finalPath.Length<3||!Char.IsLetter(finalPath[0])||finalPath[1]!=':'||finalPath[2]!='\\')throw new ArgumentException("Unsupported final path");
    return finalPath;
   }
  }
 }
}
'@ -ErrorAction Stop
}
function Resolve-FleetFinalFilePath {
 param([Parameter(Mandatory)][string]$Path)
 Initialize-FleetIdentityNative
 [FleetQuotaIdentity.Native]::FinalLocalFilePath($Path)
}
function Get-FleetIdentityProperty {
 param($Value,[string]$Name)
 if($null -eq $Value){return $null}
 if($Value -is [System.Collections.IDictionary]){if(!$Value.Contains($Name)){return $null};return $Value[$Name]}
 $property=$Value.PSObject.Properties[$Name]
 if($null -eq $property){return $null}
 return $property.Value
}
function Get-FleetIdentityProcessId {
 param($Value)
 if($null -eq $Value){return $null}
 $text=[string]$Value
 if($text -notmatch '^[1-9][0-9]{0,9}$'){return $null}
 [uint32]$number=0
 if(![uint32]::TryParse($text,[ref]$number)){return $null}
 return $number
}
function Get-FleetIdentityCreationKey {
 param($Value)
 if($Value -is [DateTimeOffset]){$timestamp=$Value}
 elseif($Value -is [DateTime]){$timestamp=[DateTimeOffset]$Value}
 else{return $null}
 return $timestamp.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
}
function Test-FleetQuotaMonitorIdentity {
 param($Details,$Guard,$Status,[Parameter(Mandatory)][string]$Root,
       [DateTimeOffset]$Now=[DateTimeOffset]::UtcNow,
       [scriptblock]$Canonicalize={param([string]$Path) Resolve-FleetFinalFilePath -Path $Path})
 try {
  $comparison=[StringComparison]::OrdinalIgnoreCase
  if($Root -notmatch '^[A-Za-z]:\\'){return $false}
  $rootPath=[IO.Path]::GetFullPath($Root).TrimEnd('\')
  $expectedChild=Join-Path $rootPath 'codex-monitor\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe'
  $expectedScript=Join-Path $rootPath 'quota-guard.js'
  if(![string]::Equals([string](Get-FleetIdentityProperty $Details 'Name'),'codex.exe',$comparison)){return $false}
  if(![string]::Equals([string](Get-FleetIdentityProperty $Guard 'Name'),'node.exe',$comparison)){return $false}
  $identity=Get-FleetIdentityProperty $Status 'monitorIdentity'
  if($null -eq $identity){return $false}
  $childId=Get-FleetIdentityProcessId (Get-FleetIdentityProperty $Details 'ProcessId')
  $parentId=Get-FleetIdentityProcessId (Get-FleetIdentityProperty $Details 'ParentProcessId')
  $guardId=Get-FleetIdentityProcessId (Get-FleetIdentityProperty $Guard 'ProcessId')
  $statusGuardId=Get-FleetIdentityProcessId (Get-FleetIdentityProperty $Status 'pid')
  $registeredChildId=Get-FleetIdentityProcessId (Get-FleetIdentityProperty $identity 'monitorPid')
  $registeredGuardId=Get-FleetIdentityProcessId (Get-FleetIdentityProperty $identity 'guardPid')
  if($null -eq $childId -or $null -eq $parentId -or $null -eq $guardId -or $childId -ne $registeredChildId -or $guardId -ne $registeredGuardId -or $guardId -ne $statusGuardId -or $parentId -ne $guardId -or $childId -eq $guardId){return $false}
  $childKey=Get-FleetIdentityCreationKey (Get-FleetIdentityProperty $Details 'CreationDate')
  $guardKey=Get-FleetIdentityCreationKey (Get-FleetIdentityProperty $Guard 'CreationDate')
  $registeredChildKey=Get-FleetIdentityProperty $identity 'monitorCreationKey'
  $registeredGuardKey=Get-FleetIdentityProperty $identity 'guardCreationKey'
  if($null -eq $childKey -or $null -eq $guardKey -or $registeredChildKey -isnot [string] -or $registeredGuardKey -isnot [string] -or $childKey -cne $registeredChildKey -or $guardKey -cne $registeredGuardKey){return $false}
  $heartbeatText=Get-FleetIdentityProperty $Status 'heartbeatAt'
  [DateTimeOffset]$heartbeat=[DateTimeOffset]::MinValue
  # PowerShell 7 converts ISO JSON values to DateTime by default.
  if($heartbeatText -is [DateTimeOffset]){$heartbeat=$heartbeatText}
  elseif($heartbeatText -is [DateTime] -and $heartbeatText.Kind -ne [DateTimeKind]::Unspecified){$heartbeat=[DateTimeOffset]$heartbeatText}
  else {
   if($heartbeatText -isnot [string] -or $heartbeatText -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})$'){return $false}
   if(![DateTimeOffset]::TryParse($heartbeatText,[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::None,[ref]$heartbeat)){return $false}
  }
  $age=($Now-$heartbeat).TotalSeconds
  if($age -lt 0 -or $age -gt 30){return $false}
  $childPath=Get-FleetIdentityProperty $Details 'ExecutablePath'
  $guardPath=Get-FleetIdentityProperty $Guard 'ExecutablePath'
  $registeredGuardPath=Get-FleetIdentityProperty $identity 'guardExecutablePath'
  foreach($candidatePath in @($childPath,$guardPath,$registeredGuardPath)){if($candidatePath -isnot [string] -or $candidatePath -notmatch '^[A-Za-z]:\\'){return $false}}
  Initialize-FleetIdentityNative
  [string[]]$childArgs=[FleetQuotaIdentity.Native]::Argv([string](Get-FleetIdentityProperty $Details 'CommandLine'))
  [string[]]$guardArgs=[FleetQuotaIdentity.Native]::Argv([string](Get-FleetIdentityProperty $Guard 'CommandLine'))
  if($childArgs.Count -ne 4 -or ![string]::Equals($childArgs[0],$expectedChild,$comparison) -or $childArgs[1] -cne 'app-server' -or $childArgs[2] -cne '--listen' -or $childArgs[3] -cne 'stdio://'){return $false}
  if($guardArgs.Count -ne 2 -or ![string]::Equals($guardArgs[0],$registeredGuardPath,$comparison) -or ![string]::Equals($guardArgs[1],$expectedScript,$comparison)){return $false}
  $resolved=@{}
  foreach($entry in @(@{Key='expectedChild';Path=$expectedChild},@{Key='child';Path=$childPath},@{Key='expectedScript';Path=$expectedScript},@{Key='guard';Path=$guardPath},@{Key='registeredGuard';Path=$registeredGuardPath})){
   $result=@(& $Canonicalize $entry.Path)
   if($result.Count -ne 1 -or $result[0] -isnot [string] -or [string]::IsNullOrWhiteSpace($result[0])){return $false}
   $resolved[$entry.Key]=$result[0]
  }
  if(![string]::Equals($resolved.expectedChild,$expectedChild,$comparison) -or ![string]::Equals($resolved.expectedScript,$expectedScript,$comparison)){return $false}
  if(![string]::Equals($resolved.child,$resolved.expectedChild,$comparison) -or ![string]::Equals($resolved.guard,$resolved.registeredGuard,$comparison)){return $false}
  return $true
 }catch{return $false}
}
