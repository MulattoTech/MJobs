<#
Invoked only by the fixed Interactive/Limited Task Scheduler task.
No parameters originate in the request other than the allowlisted mode.
Does not kill processes, resume agents, submit prompts, or edit Codex settings.
LoadFunctionsOnly exposes the decision function for fixture tests without I/O.
#>
[CmdletBinding()]
param([switch]$LoadFunctionsOnly)

$ErrorActionPreference = 'Stop'
$script:FleetRequestedRoot = $PSScriptRoot
$script:FleetAoCli = 'C:\Users\Dylan\AppData\Local\Programs\agent-orchestrator\resources\daemon\ao.exe'
$script:FleetAoUi = 'C:\Users\Dylan\AppData\Local\Programs\agent-orchestrator\agent-orchestrator.exe'

function Initialize-FleetStandardToken {
    if ('FleetStandardTokenV1.Reader' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace FleetStandardTokenV1 {
    public sealed class Info { public bool IsElevated; public int IntegrityRid; public uint Pid; public uint SessionId; }
    public static class Reader {
        [StructLayout(LayoutKind.Sequential)] private struct Label { public IntPtr Sid; public uint Attributes; }
        [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError=true)] private static extern bool ProcessIdToSessionId(uint pid, out uint session);
        [DllImport("advapi32.dll", SetLastError=true)] private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError=true)] private static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer, uint size, out uint required);
        [DllImport("advapi32.dll")] private static extern bool IsValidSid(IntPtr sid);
        [DllImport("advapi32.dll")] private static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
        [DllImport("advapi32.dll")] private static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);
        public static Info Inspect(uint pid) {
            IntPtr process = OpenProcess(0x1000, false, pid), token = IntPtr.Zero;
            if (process == IntPtr.Zero) throw new InvalidOperationException("PROCESS_TOKEN_UNREADABLE");
            try {
                if (!OpenProcessToken(process, 8, out token)) throw new InvalidOperationException("PROCESS_TOKEN_UNREADABLE");
                Info result = new Info { Pid=pid }; uint required, session;
                if (!ProcessIdToSessionId(pid, out session)) throw new InvalidOperationException("PROCESS_SESSION_UNREADABLE");
                result.SessionId = session;
                IntPtr elevation = Marshal.AllocHGlobal(4);
                try {
                    if (!GetTokenInformation(token,20,elevation,4,out required)) throw new InvalidOperationException("PROCESS_TOKEN_UNREADABLE");
                    result.IsElevated = Marshal.ReadInt32(elevation) != 0;
                } finally { Marshal.FreeHGlobal(elevation); }
                GetTokenInformation(token,25,IntPtr.Zero,0,out required);
                if (required < Marshal.SizeOf(typeof(Label)) || required > 65536) throw new InvalidOperationException("PROCESS_INTEGRITY_UNREADABLE");
                IntPtr integrity = Marshal.AllocHGlobal((int)required);
                try {
                    if (!GetTokenInformation(token,25,integrity,required,out required)) throw new InvalidOperationException("PROCESS_INTEGRITY_UNREADABLE");
                    Label label=(Label)Marshal.PtrToStructure(integrity,typeof(Label));
                    if(label.Sid==IntPtr.Zero || !IsValidSid(label.Sid)) throw new InvalidOperationException("PROCESS_INTEGRITY_UNREADABLE");
                    byte count=Marshal.ReadByte(GetSidSubAuthorityCount(label.Sid));
                    if(count<1) throw new InvalidOperationException("PROCESS_INTEGRITY_UNREADABLE");
                    result.IntegrityRid=Marshal.ReadInt32(GetSidSubAuthority(label.Sid,(uint)(count-1)));
                } finally { Marshal.FreeHGlobal(integrity); }
                return result;
            } finally { if(token!=IntPtr.Zero) CloseHandle(token); CloseHandle(process); }
        }
    }
}
'@ -ErrorAction Stop
}

function Get-FleetStandardToken {
    param([uint32]$ProcessId)
    Initialize-FleetStandardToken
    $info = [FleetStandardTokenV1.Reader]::Inspect($ProcessId)
    [pscustomobject]@{
        pid = [int]$info.Pid
        isElevated = [bool]$info.IsElevated
        integrityRid = [int]$info.IntegrityRid
        sessionId = [int]$info.SessionId
    }
}

function Test-FleetStandardToken {
    param($Token)
    return ($null -ne $Token -and $Token.isElevated -is [bool] -and
        $Token.isElevated -eq $false -and $Token.integrityRid -eq 8192 -and
        $Token.sessionId -gt 0 -and $Token.pid -gt 0)
}

function Get-FleetDaemonIdentity {
    $empty = [ordered]@{ present=$false; verified=$true; healthy=$false; pid=$null;
        isElevated=$null; integrityRid=$null; sessionId=$null; error=$null }
    try {
        # Enumerate listeners rather than treating any query failure as an
        # unoccupied port. We never kill a listener to free the port.
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalPort -eq 3001 })
        if ($listeners.Count -eq 0) { return [pscustomobject]$empty }
        $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -ne 1) { throw 'DAEMON_LISTENER_AMBIGUOUS' }
        $ownerId = [uint32]$owners[0]
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction Stop
        if ($null -eq $owner -or [string]::IsNullOrWhiteSpace($owner.ExecutablePath)) {
            throw 'DAEMON_LISTENER_IDENTITY_UNREADABLE'
        }
        if (-not [string]::Equals([IO.Path]::GetFullPath($owner.ExecutablePath),
            $script:FleetAoCli, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'DAEMON_PORT_OWNED_BY_OTHER_PROCESS'
        }
        $token = Get-FleetStandardToken -ProcessId $ownerId
        $empty.present=$true; $empty.pid=[int]$ownerId
        $empty.isElevated=$token.isElevated; $empty.integrityRid=$token.integrityRid
        $empty.sessionId=$token.sessionId
        if (-not (Test-FleetStandardToken $token)) { throw 'EXISTING_DAEMON_NOT_STANDARD_USER' }

        $handler = [Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $handler.UseProxy = $false
        $client = [Net.Http.HttpClient]::new($handler)
        try {
            $client.Timeout = [TimeSpan]::FromSeconds(2)
            $response = $client.GetAsync('http://127.0.0.1:3001/api/v1/sessions').GetAwaiter().GetResult()
            try {
                if ([int]$response.StatusCode -eq 200) {
                    $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                    if ($text.Length -le 2097152) {
                        $body = $text | ConvertFrom-Json -ErrorAction Stop
                        $empty.healthy = ($null -ne $body.PSObject.Properties['sessions'] -and
                            $body.sessions -is [Array])
                    }
                }
            } finally { $response.Dispose() }
        } catch {
            $empty.error = 'DAEMON_HEALTH_NOT_READY'
        } finally { $client.Dispose(); $handler.Dispose() }
        return [pscustomobject]$empty
    } catch {
        $empty.verified=$false
        $message = $_.Exception.Message
        $empty.error = if ($message -match '^[A-Z][A-Z0-9_]+$') { $message } else { 'DAEMON_STATE_UNVERIFIABLE' }
        return [pscustomobject]$empty
    }
}

function Get-FleetUiIdentity {
    $empty = [ordered]@{ running=$false; verified=$true; pid=$null; isElevated=$null;
        integrityRid=$null; sessionId=$null; error=$null }
    try {
        $candidates = @(Get-CimInstance Win32_Process -Filter "Name = 'agent-orchestrator.exe'" -ErrorAction Stop)
        $mainProcesses = @()
        foreach ($candidate in $candidates) {
            if ([string]::IsNullOrWhiteSpace($candidate.ExecutablePath) -or
                [string]::IsNullOrWhiteSpace($candidate.CommandLine)) { throw 'EXISTING_UI_IDENTITY_UNREADABLE' }
            if (-not [string]::Equals([IO.Path]::GetFullPath($candidate.ExecutablePath),
                $script:FleetAoUi, [StringComparison]::OrdinalIgnoreCase)) { throw 'OTHER_ORCHESTRATOR_INSTALL_RUNNING' }
            if ($candidate.CommandLine -notmatch '(?i)(?:^|\s)--type(?:=|\s)') { $mainProcesses += $candidate }
        }
        if ($mainProcesses.Count -eq 0) {
            if ($candidates.Count -gt 0) { throw 'UI_CHILDREN_WITHOUT_VERIFIED_MAIN' }
            return [pscustomobject]$empty
        }
        if ($mainProcesses.Count -ne 1) { throw 'DUPLICATE_ORCHESTRATOR_UI' }
        $token = Get-FleetStandardToken -ProcessId ([uint32]$mainProcesses[0].ProcessId)
        if (-not (Test-FleetStandardToken $token)) { throw 'EXISTING_UI_NOT_STANDARD_USER' }
        $empty.running=$true; $empty.pid=$token.pid; $empty.isElevated=$token.isElevated
        $empty.integrityRid=$token.integrityRid; $empty.sessionId=$token.sessionId
        return [pscustomobject]$empty
    } catch {
        $empty.verified=$false
        $message=$_.Exception.Message
        $empty.error=if($message -match '^[A-Z][A-Z0-9_]+$'){$message}else{'UI_STATE_UNVERIFIABLE'}
        return [pscustomobject]$empty
    }
}

function Start-FleetFixedProcess {
    param([ValidateSet('daemon','ui')][string]$Mode)
    $executable = if ($Mode -eq 'daemon') { $script:FleetAoCli } else { $script:FleetAoUi }
    if (-not [IO.File]::Exists($executable)) { throw 'ORCHESTRATOR_EXECUTABLE_MISSING' }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $executable
    $info.WorkingDirectory = [IO.Path]::GetDirectoryName($executable)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = if ($Mode -eq 'daemon') {
        [Diagnostics.ProcessWindowStyle]::Hidden
    } else { [Diagnostics.ProcessWindowStyle]::Normal }
    $info.Arguments = if ($Mode -eq 'daemon') { 'start --json' } else { '' }
    # Native fixed executable: no cmd/npm/powershell command interpolation.
    # Do not capture unbounded output or publish CLI content/credentials.
    $process = [Diagnostics.Process]::new()
    try {
        $process.StartInfo = $info
        if (-not $process.Start()) { throw 'ORCHESTRATOR_PROCESS_NOT_STARTED' }
        return [pscustomobject]@{ pid=$process.Id }
    } finally { $process.Dispose() }
}

function Invoke-FleetOrchestratorRequest {
    param($Request, [Parameter(Mandatory)][hashtable]$Adapters)
    $started = $false
    $result = [ordered]@{
        version=1; nonce=$Request.nonce; mode=$Request.mode; status='rejected'
        completedAt=$null; safeToRetry=$true; isElevated=$null; integrityRid=$null
        pid=$null; sessionId=$null; starterPid=$null; daemon=$null; ui=$null; error=$null
    }
    try {
        $now = [DateTimeOffset](& $Adapters.Now)
        if ($Request.version -ne 1 -or $Request.nonce -isnot [string] -or
            $Request.nonce -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
            $Request.mode -notin @('daemon','ui') -or $Request.requestedAt -isnot [string]) {
            throw 'INVALID_LAUNCH_REQUEST'
        }
        [DateTimeOffset]$requestedAt = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse($Request.requestedAt,
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None,
            [ref]$requestedAt)) { throw 'INVALID_REQUEST_TIME' }
        $age=($now-$requestedAt).TotalSeconds
        if ($age -lt -5 -or $age -gt 60) { throw 'STALE_LAUNCH_REQUEST' }
        if ($Adapters.ContainsKey('Cancelled') -and (& $Adapters.Cancelled $Request)) { throw 'LAUNCH_CANCELLED' }
        $token = & $Adapters.Token
        $result.pid=$token.pid; $result.isElevated=$token.isElevated
        $result.integrityRid=$token.integrityRid; $result.sessionId=$token.sessionId
        if (-not (Test-FleetStandardToken $token)) { throw 'LAUNCHER_NOT_STANDARD_INTERACTIVE_USER' }

        $result.daemon = & $Adapters.GetDaemon
        if (-not $result.daemon.verified) { throw $result.daemon.error }
        if ($result.daemon.healthy -and -not (Test-FleetStandardToken $result.daemon)) { throw 'DAEMON_NOT_STANDARD_USER' }
        if ($result.daemon.healthy -and $result.daemon.sessionId -ne $token.sessionId) { throw 'DAEMON_IN_OTHER_SESSION' }
        if ($result.daemon.present -and -not $result.daemon.healthy) { throw 'EXISTING_DAEMON_UNHEALTHY_NO_RESTART' }

        if ($Request.mode -eq 'daemon' -and $result.daemon.healthy) {
            $result.status='ok'; $result.error=$null
        } else {
            if ($Request.mode -eq 'ui') {
                $result.ui = & $Adapters.GetUi
                if (-not $result.ui.verified) { throw $result.ui.error }
                if ($result.ui.running -and -not (Test-FleetStandardToken $result.ui)) { throw 'UI_NOT_STANDARD_USER' }
                if ($result.ui.running -and $result.ui.sessionId -ne $token.sessionId) { throw 'UI_IN_OTHER_SESSION' }
                if ($result.ui.running) { $result.status='ok'; $result.error=$null }
            }
            if ($result.status -ne 'ok') {
                # The broker's cancellation can arrive while native identity
                # checks run. Recheck it at the last boundary before spawn.
                if ($Adapters.ContainsKey('Cancelled') -and (& $Adapters.Cancelled $Request)) { throw 'LAUNCH_CANCELLED' }
                # Mark before invoking: an exception may happen after a child
                # was created, so an ambiguous failure may not be retried.
                $started=$true; $result.safeToRetry=$false
                $startInfo = if ($Request.mode -eq 'daemon') { & $Adapters.StartDaemon } else { & $Adapters.StartUi }
                $result.starterPid=$startInfo.pid
                $deadline = ([DateTimeOffset](& $Adapters.Now)).AddSeconds(18)
                $readinessTimer = [Diagnostics.Stopwatch]::StartNew()
                do {
                    if ($Request.mode -eq 'daemon') {
                        $result.daemon = & $Adapters.GetDaemon
                        if (-not $result.daemon.verified) { throw $result.daemon.error }
                        if ($result.daemon.healthy -and -not (Test-FleetStandardToken $result.daemon)) { throw 'DAEMON_NOT_STANDARD_USER' }
                        if ($result.daemon.healthy -and $result.daemon.sessionId -ne $token.sessionId) { throw 'DAEMON_IN_OTHER_SESSION' }
                        if ($result.daemon.healthy) { $result.status='ok'; break }
                    } else {
                        $result.ui = & $Adapters.GetUi
                        if (-not $result.ui.verified) { throw $result.ui.error }
                        if ($result.ui.running -and -not (Test-FleetStandardToken $result.ui)) { throw 'UI_NOT_STANDARD_USER' }
                        if ($result.ui.running -and $result.ui.sessionId -ne $token.sessionId) { throw 'UI_IN_OTHER_SESSION' }
                        if ($result.ui.running) { $result.status='ok'; break }
                    }
                    & $Adapters.Sleep 250
                } while ([DateTimeOffset](& $Adapters.Now) -lt $deadline -and $readinessTimer.ElapsedMilliseconds -lt 18000)
                $readinessTimer.Stop()
                if ($result.status -ne 'ok') { throw 'STARTED_BUT_READINESS_UNVERIFIED' }
            }
        }
    } catch {
        $result.status='error'; $result.safeToRetry=(-not $started)
        $message=$_.Exception.Message
        $result.error=if($message -match '^[A-Z][A-Z0-9_]+$'){$message}else{'LAUNCH_VERIFICATION_FAILED'}
    } finally {
        $result.completedAt=([DateTimeOffset](& $Adapters.Now)).ToUniversalTime().ToString('o')
    }
    return [pscustomobject]$result
}

function Test-FleetLaunchCancelled {
    param($Request)
    $latchPath=Join-Path $script:FleetRequestedRoot 'emergency-latch.json'
    try { $latch=[IO.File]::ReadAllText($latchPath)|ConvertFrom-Json -DateKind String -ErrorAction Stop }
    catch { throw 'EMERGENCY_STATE_UNREADABLE' }
    if ($latch.enabled -isnot [bool]) { throw 'EMERGENCY_STATE_INVALID' }
    if ($latch.enabled) { throw 'EMERGENCY_LOCKOUT' }
    $cancelPath=Join-Path $script:FleetRequestedRoot 'orchestrator-launch-cancel.json'
    if (-not [IO.File]::Exists($cancelPath)) { return $false }
    try { $cancel=[IO.File]::ReadAllText($cancelPath)|ConvertFrom-Json -DateKind String -ErrorAction Stop }
    catch { throw 'LAUNCH_CANCELLATION_UNREADABLE' }
    return ($cancel.version -eq 1 -and $cancel.nonce -ceq $Request.nonce -and $cancel.mode -ceq $Request.mode)
}

function Write-FleetLaunchResult {
    param($Result)
    $requestPath=Join-Path $script:FleetRequestedRoot 'orchestrator-launch-request.json'
    $current=[IO.File]::ReadAllText($requestPath) | ConvertFrom-Json -DateKind String -ErrorAction Stop
    if ($current.nonce -cne $Result.nonce) { return }
    $destination=Join-Path $script:FleetRequestedRoot 'orchestrator-launch-result.json'
    $temporary=$destination+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    try {
        [IO.File]::WriteAllText($temporary,($Result|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($destination)) { [IO.File]::Replace($temporary,$destination,[NullString]::Value) }
        else { [IO.File]::Move($temporary,$destination) }
    } finally { if([IO.File]::Exists($temporary)){[IO.File]::Delete($temporary)} }
}

if ($LoadFunctionsOnly) { return }

$launchMutex = [Threading.Mutex]::new($false,'Local\MJobsFleet-OrchestratorStandard-Launch')
$hasMutex=$false
$launchResult=$null
try {
    try { $hasMutex=$launchMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $hasMutex=$true }
    # Another task instance may be fulfilling the same nonce. Never overwrite
    # its eventual result or start another process while the mutex is held.
    if (-not $hasMutex) { exit 3 }
    $requestPath=Join-Path $script:FleetRequestedRoot 'orchestrator-launch-request.json'
    $request=[IO.File]::ReadAllText($requestPath) | ConvertFrom-Json -DateKind String -ErrorAction Stop
    $resultPath=Join-Path $script:FleetRequestedRoot 'orchestrator-launch-result.json'
    $previous=$null
    try { $previous=[IO.File]::ReadAllText($resultPath)|ConvertFrom-Json -DateKind String -ErrorAction Stop } catch {}
    if ($null -ne $previous -and $previous.version -eq 1 -and $previous.nonce -ceq $request.nonce -and
        $previous.mode -ceq $request.mode -and $previous.status -in @('ok','error','rejected','busy')) { exit 0 }
    # Verify this Limited process can atomically publish a result before any
    # process start. The broker does not mistake 'accepted' for readiness.
    Write-FleetLaunchResult ([pscustomobject]@{
        version=1; nonce=$request.nonce; mode=$request.mode; status='accepted'
        completedAt=[DateTimeOffset]::UtcNow.ToString('o'); safeToRetry=$false
        isElevated=$null; integrityRid=$null; pid=$null; error=$null
    })
    $adapters=@{
        Now={ [DateTimeOffset]::UtcNow }
        Token={ Get-FleetStandardToken -ProcessId ([uint32]$PID) }
        GetDaemon={ Get-FleetDaemonIdentity }
        GetUi={ Get-FleetUiIdentity }
        StartDaemon={ Start-FleetFixedProcess -Mode daemon }
        StartUi={ Start-FleetFixedProcess -Mode ui }
        Sleep={ param($Milliseconds) Start-Sleep -Milliseconds $Milliseconds }
        Cancelled={ param($PendingRequest) Test-FleetLaunchCancelled $PendingRequest }
    }
    $launchResult=Invoke-FleetOrchestratorRequest -Request $request -Adapters $adapters
    Write-FleetLaunchResult $launchResult
} finally {
    if ($hasMutex) { $launchMutex.ReleaseMutex() }
    $launchMutex.Dispose()
}
if ($null -ne $launchResult) {
    if ($launchResult.status -eq 'ok') { exit 0 } else { exit 2 }
}
