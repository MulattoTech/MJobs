# Shared interactive shell entry point. Importing this module launches nothing.
# PowerShell 5.1 imports it, but all Codex execution is delegated to PowerShell 7
# in the SAME console. The existing Fleet guard remains authoritative.
Set-StrictMode -Version 2.0
$script:ModuleDirectory = $PSScriptRoot
$script:GuardPath = Join-Path $PSScriptRoot 'Codex-Profile-Guard.ps1'

function Assert-CodexShellUnlocked {
    if (-not (Test-Path -LiteralPath $script:GuardPath -PathType Leaf)) {
        throw 'The Fleet Codex guard is missing. No Codex process was launched.'
    }
    # Load the current guard on each launch; do not cache a replaced safety file.
    . $script:GuardPath
    Assert-CodexFleetUnlocked
}

function Get-CodexLaunchPlan {
    param([AllowEmptyCollection()][string[]]$Arguments = @(),
          [Parameter(Mandatory=$true)][string]$WorkingDirectory)
    $commands = @('agents','resume','fork','exec','e','review','login','logout','mcp',
        'plugin','app-server','remote-control','app','completion','update','doctor',
        'sandbox','debug','apply','a','queue','archive','delete','migrate-rollouts',
        'unarchive','cloud','exec-server','features','help')
    $valueOptions = @('--config','-c','--model','-m','--profile','-p','--cd','-C',
        '--image','-i','--remote','--remote-auth-token-env','--sandbox','-s',
        '--ask-for-approval','-a','--local-provider','--add-dir','--enable','--disable')
    $booleanOptions = @('--search','--oss','--full-auto','--no-alt-screen','--last','--all',
        '--dangerously-bypass-approvals-and-sandbox','--yolo','--strict-config')
    $command = $null; $infoOnly = $false; $explicitConnection = $false
    $hasDirectory = $false; $localOnly = $false; $unknownOption = $false
    $firstPositionalSeen = $false; $hasOverrides = $false
    for ($i=0; $i -lt $Arguments.Count; $i++) {
        $token = $Arguments[$i]
        if ($token -ceq '--') { break }
        if ($token -cin @('-c','--config','--enable','--disable','--search') -or $token -cmatch '^--(config|enable|disable)=' -or $token -cmatch '^-c.+') { $hasOverrides=$true; $localOnly=$true }
        if ($token -cin @('--help','-h','--version','-V')) { $infoOnly=$true; continue }
        if ($token -ceq '--no-daemon') { $explicitConnection=$true; continue }
        if ($token -ceq '--worktree') { $localOnly=$true; continue }
        if ($token -cmatch '^--remote(?:=|$)' -or $token -cmatch '^--remote-auth-token-env(?:=|$)') { $explicitConnection=$true }
        if ($token -cmatch '^--cd(?:=|$)' -or $token -cmatch '^-C') { $hasDirectory=$true }
        if ($token -cin $valueOptions) { $i++; continue }
        if ($token -cmatch '^--[^=]+=') { continue }
        if ($token -cmatch '^-[cmpisaC].+') { continue }
        if ($token -cin $booleanOptions) { continue }
        if ($token.StartsWith('-')) { $unknownOption=$true; continue }
        if (-not $firstPositionalSeen) {
            $firstPositionalSeen=$true
            if ($token -cin $commands) { $command=$token }
        }
    }
    $readOnlyCommand = $command -cin @('help','login','logout','features','completion','doctor')
    $useShared = -not ($infoOnly -or $explicitConnection -or $localOnly -or $unknownOption) -and
        ($null -eq $command -or $command -cin @('agents','resume','fork'))
    [pscustomobject]@{
        Arguments = [string[]]@($Arguments)
        WorkingDirectory = $WorkingDirectory
        Command = $command
        UseSharedServer = [bool]$useShared
        UseEmbeddedServer = [bool]($hasOverrides -and -not ($infoOnly -or $explicitConnection) -and ($null -eq $command -or $command -cin @('resume','fork')))
        RequireUnlocked = -not ($infoOnly -or $readOnlyCommand)
        AddDefaultDirectory = [bool]($useShared -and -not $hasDirectory)
    }
}

function Resolve-CodexNativeExecutable {
    $launcher = Get-Command codex.cmd -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    $npmDirectory = Split-Path -Parent $launcher.Source
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
    $triple = if ($arch -eq 'arm64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
    $relative = 'node_modules\@openai\codex\node_modules\@openai\codex-win32-' + $arch + '\vendor\' + $triple + '\bin\codex.exe'
    $native = Join-Path $npmDirectory $relative
    if (-not (Test-Path -LiteralPath $native -PathType Leaf)) {
        throw 'The currently installed npm Codex native executable is missing. Repair that installation; no alternative or older CLI was launched.'
    }
    return $native
}

function Get-CodexLocalServerAddress {
    param([Parameter(Mandatory=$true)][string]$NativeExecutable)
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Shared server setup requires PowerShell 7.' }
    . $script:GuardPath
    Assert-CodexFleetUnlocked
    $codexDirectory = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $socketPath = Join-Path $codexDirectory 'app-server-control\agents-local.sock'
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $socketPath) -PathType Container)) {
        throw 'The private Codex socket directory is missing. No backend was launched.'
    }
    $canConnect = {
        $socket = [System.Net.Sockets.Socket]::new(
            [System.Net.Sockets.AddressFamily]::Unix,
            [System.Net.Sockets.SocketType]::Stream,
            [System.Net.Sockets.ProtocolType]::Unspecified)
        try {
            $socket.Connect([System.Net.Sockets.UnixDomainSocketEndPoint]::new($socketPath))
            return $true
        } catch { return $false }
        finally { $socket.Dispose() }
    }
    $mutex = [Threading.Mutex]::new($false,'Local\CodexSharedServerLaunch')
    $owned = $false
    try {
        try { $owned=$mutex.WaitOne(15000) }
        catch [Threading.AbandonedMutexException] { $owned=$true }
        if (-not $owned) { throw 'Another Codex backend launch is still in progress.' }
        if (-not (& $canConnect)) {
            $existing = Get-CimInstance Win32_Process -Filter "Name='codex.exe'" -ErrorAction Stop |
                Where-Object { $_.CommandLine -like '*agents-local.sock*' }
            if ($existing) {
                throw 'The shared Codex server exists but is not responding or is inaccessible to this user. No duplicate backend was launched.'
            }
            if (Test-Path -LiteralPath $socketPath) {
                Remove-Item -LiteralPath $socketPath -Force -ErrorAction Stop
            }
            $server = Start-CodexLocalServerHidden -Executable $NativeExecutable -Address ('unix://' + $socketPath)
            $ready = $false
            for ($attempt=0; $attempt -lt 100; $attempt++) {
                if (& $canConnect) { $ready=$true; break }
                $server.Refresh()
                if ($server.HasExited) { break }
                Start-Sleep -Milliseconds 100
            }
            if (-not $ready) {
                # Never fall back to a second server or terminate unrelated work.
                throw ('The shared Codex server did not become ready (PID ' + $server.Id + '). No retry or frontend was started; inspect the backend before retrying.')
            }
        }
        Assert-CodexFleetUnlocked
        return 'unix://' + $socketPath
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Invoke-CodexShell {
    param([AllowEmptyCollection()][string[]]$Arguments = @(),
          [string]$WorkingDirectory = $PWD.ProviderPath)
    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        throw 'Run Codex from a filesystem directory.'
    }
    $plan = Get-CodexLaunchPlan -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    if ($plan.RequireUnlocked) { Assert-CodexShellUnlocked }
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        $pwsh = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
        if (-not (Test-Path -LiteralPath $pwsh -PathType Leaf)) {
            throw 'PowerShell 7 is required for the shared hidden Codex backend. No fallback launch was attempted.'
        }
        $request = @{arguments=[string[]]@($Arguments); workingDirectory=$WorkingDirectory}
        $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Compress -Depth 4)))
        # Call operator, not Start-Process: inherit this existing terminal/console.
        & $pwsh -NoLogo -NoProfile -File (Join-Path $script:ModuleDirectory 'Invoke-CodexShell.ps1') -RequestBase64 $encoded
        $global:LASTEXITCODE = $LASTEXITCODE
        return
    }
    $native = Resolve-CodexNativeExecutable
    $effective = [Collections.Generic.List[string]]::new()
    if ($plan.UseSharedServer) {
        $address = Get-CodexLocalServerAddress -NativeExecutable $native
        $effective.Add('--remote'); $effective.Add($address)
        if ($plan.AddDefaultDirectory) {
            $effective.Add('--cd'); $effective.Add($WorkingDirectory)
        }
    }
    if ($plan.UseEmbeddedServer) { $effective.Add('--no-daemon') }
    foreach ($argument in $Arguments) { $effective.Add($argument) }
    if ($plan.RequireUnlocked) { Assert-CodexShellUnlocked }
    # PowerShell 7 preserves empty arguments and embedded quotation marks for .exe.
    $PSNativeCommandArgumentPassing = 'Standard'
    & $native @effective
    $global:LASTEXITCODE = $LASTEXITCODE
}

function codex {
    Invoke-CodexShell -Arguments ([string[]]@($args)) -WorkingDirectory $PWD.ProviderPath
}

function Invoke-CodexAgents {
    param([string]$CodexCli)
    Invoke-CodexShell -Arguments @('agents') -WorkingDirectory $PWD.ProviderPath
}

Export-ModuleMember -Function codex,Invoke-CodexAgents,Invoke-CodexShell,Get-CodexLaunchPlan
