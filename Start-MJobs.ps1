param(
    [switch]$NoBuild,
    [switch]$NoOpen,
    [ValidatePattern('^[a-z][a-z0-9-]{0,39}$')][string]$Project = 'mjobs',
    [ValidateRange(1024,65535)][int]$Port = 18700
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$priorPort = $env:MJOBS_PORT
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker Desktop with Linux containers is required.' }
    docker info --format '{{.OSType}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop, then run this command again.' }
    $envPath = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        $bytes = New-Object byte[] 32
        $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
        $secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
        $stream = [System.IO.File]::Open($envPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
        try {
            $content = [System.Text.Encoding]::UTF8.GetBytes("POSTGRES_PASSWORD=$secret`n")
            $stream.Write($content,0,$content.Length)
        } finally { $stream.Dispose(); $secret = $null; $content = $null; $bytes = $null }
        Write-Host 'Created the ignored project-local database configuration.'
    }
    if ([System.IO.File]::ReadAllText($envPath).Contains('replace-with-private-generated-value')) {
        throw 'The .env file still contains the example placeholder. Replace it with a private generated password before first startup.'
    }
    $env:MJOBS_PORT = [string]$Port
    $composeArgs = @('compose','-p',$Project,'up','-d','--wait','--wait-timeout','180')
    if (-not $NoBuild) { $composeArgs += '--build' }
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) { throw 'Project startup failed. Inspect docker compose logs; existing data has been preserved.' }
    $address = "http://127.0.0.1:$Port"
    $deadline = (Get-Date).AddSeconds(120)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try { $health = Invoke-RestMethod "$address/readiness" -TimeoutSec 5; $ready = $health.status -eq 'ready' } catch { $ready = $false }
        if ($ready) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "Services are not ready. Open $address and inspect Activity or docker compose logs." }
    Write-Host "MJobs is ready at $address. Real external submission is OFF."
    if (-not $NoOpen) { Start-Process $address }
} finally {
    $env:MJOBS_PORT = $priorPort
    Pop-Location
}
