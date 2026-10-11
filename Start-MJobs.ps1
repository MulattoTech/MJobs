param(
    [switch]$NoBuild,
    [switch]$NoOpen,
    [switch]$Personal,
    [ValidatePattern('^[a-z][a-z0-9-]{0,39}$')][string]$Project = 'mjobs',
    [ValidateRange(1024,65535)][int]$Port = 18700
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$priorPort = $env:MJOBS_PORT
$priorMode = $env:MJOBS_OPERATION_MODE
$priorMock = $env:MJOBS_COMPOSE_MOCK
$priorProfiles = $env:COMPOSE_PROFILES
try {
    if ($Personal) {
        if (-not $PSBoundParameters.ContainsKey('Project')) { $Project = 'mjobs-personal' }
        if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = 18701 }
        if ($Project -eq 'mjobs') { throw 'Personal operation requires a separate Compose project from the fictional verification workspace.' }
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker Desktop with Linux containers is required.' }
    docker info --format '{{.OSType}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop, then run this command again.' }
    $envPath = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        $bytes = New-Object byte[] 32
        $keyBytes = New-Object byte[] 32
        $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $temporaryEnv = Join-Path $PSScriptRoot ('.env.init-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        try {
            $generator.GetBytes($bytes)
            $generator.GetBytes($keyBytes)
            $secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
            $key = [Convert]::ToBase64String($keyBytes).Replace('+','-').Replace('/','_')
            $stream = [System.IO.File]::Open($temporaryEnv, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
            try {
                $content = [System.Text.Encoding]::UTF8.GetBytes("POSTGRES_PASSWORD=$secret`nMJOBS_CREDENTIAL_ENCRYPTION_KEY=$key`n")
                $stream.Write($content,0,$content.Length)
                $stream.Flush($true)
            } finally { $stream.Dispose() }
            try {
                # Move publishes complete bytes without replacing another launcher's winner.
                [System.IO.File]::Move($temporaryEnv, $envPath)
                Write-Host 'Created the ignored project-local database and credential configuration.'
            } catch [System.IO.IOException] {
                if (-not (Test-Path -LiteralPath $envPath)) { throw }
            }
        } finally {
            $generator.Dispose()
            if (Test-Path -LiteralPath $temporaryEnv) { Remove-Item -LiteralPath $temporaryEnv }
            [Array]::Clear($bytes, 0, $bytes.Length)
            [Array]::Clear($keyBytes, 0, $keyBytes.Length)
            $secret = $null; $key = $null; $content = $null
        }
    }
    $configuration = [System.IO.File]::ReadAllText($envPath)
    if ($configuration.Contains('replace-with-private-generated-value')) {
        throw 'The .env file still contains the example placeholder. Replace it with a private generated password before first startup.'
    }
    $effectiveKey = $env:MJOBS_CREDENTIAL_ENCRYPTION_KEY
    if (-not $effectiveKey) {
        $keyLines = [regex]::Matches($configuration, '(?m)^[ \t]*(?:export[ \t]+)?MJOBS_CREDENTIAL_ENCRYPTION_KEY[ \t]*=[ \t]*(?:"([^"]*)"|''([^'']*)''|([^\s#]*))[ \t]*(?:#[^\r\n]*)?\r?$')
        if ($keyLines.Count -gt 0) {
            foreach ($group in $keyLines[$keyLines.Count - 1].Groups | Select-Object -Skip 1) {
                if ($group.Success) { $effectiveKey = $group.Value; break }
            }
        }
    }
    try {
        if ($effectiveKey -cnotmatch '^[A-Za-z0-9+/_-]{43}=$') { throw 'Invalid key encoding' }
        $decodedKey = [Convert]::FromBase64String($effectiveKey.Replace('-','+').Replace('_','/'))
        if ($decodedKey.Length -ne 32) { throw 'Invalid key length' }
    } catch {
        throw 'MJOBS_CREDENTIAL_ENCRYPTION_KEY is missing or invalid. Existing configuration was preserved. Restore the original key from your private configuration backup if credentials were stored; see docs/runbooks/WORKSPACE_MODE.md. Never replace an existing encryption key to retry startup.'
    } finally {
        if ($decodedKey) { [Array]::Clear($decodedKey, 0, $decodedKey.Length) }
        $effectiveKey = $null; $configuration = $null; $keyLines = $null
    }
    $env:MJOBS_PORT = [string]$Port
    $env:MJOBS_OPERATION_MODE = if ($Personal) { 'PERSONAL' } else { 'FIXTURE' }
    $env:MJOBS_COMPOSE_MOCK = if ($Personal) { '0' } else { '1' }
    $env:COMPOSE_PROFILES = if ($Personal) { '' } else { 'fixture' }
    $composeArgs = @('compose','-p',$Project,'up','-d','--wait','--wait-timeout','180')
    if (-not $NoBuild) { $composeArgs += '--build' }
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) { throw 'Project startup failed. Inspect docker compose logs; existing data has been preserved. For WORKSPACE_MODE_UNBOUND, see docs/runbooks/WORKSPACE_MODE.md.' }
    $address = "http://127.0.0.1:$Port"
    $deadline = (Get-Date).AddSeconds(120)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try { $health = Invoke-RestMethod "$address/readiness" -TimeoutSec 5; $ready = $health.status -eq 'ready' } catch { $ready = $false }
        if ($ready) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "Services are not ready. Open $address and inspect Activity or docker compose logs." }
    $operationLabel = if ($Personal) { 'Personal operation; fictional actions OFF' } else { 'Fictional verification' }
    Write-Host "MJobs is ready at $address. $operationLabel. Real external submission is OFF."
    if (-not $NoOpen) { Start-Process $address }
} finally {
    $env:MJOBS_PORT = $priorPort
    $env:MJOBS_OPERATION_MODE = $priorMode
    $env:MJOBS_COMPOSE_MOCK = $priorMock
    $env:COMPOSE_PROFILES = $priorProfiles
    Pop-Location
}
