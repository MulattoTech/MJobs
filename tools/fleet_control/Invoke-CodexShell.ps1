# Same-console PowerShell 5.1 -> PowerShell 7 bridge; no prompt is generated.
param([Parameter(Mandatory=$true)][string]$RequestBase64)
$ErrorActionPreference = 'Stop'
try {
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7 is required.' }
    $request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($RequestBase64)) | ConvertFrom-Json
    if ($request.workingDirectory -isnot [string] -or
        -not (Test-Path -LiteralPath $request.workingDirectory -PathType Container)) {
        throw 'The original working directory is unavailable.'
    }
    foreach ($argument in @($request.arguments)) {
        if ($argument -isnot [string]) { throw 'Codex arguments must be strings.' }
    }
    Set-Location -LiteralPath $request.workingDirectory
    Import-Module (Join-Path $PSScriptRoot 'Codex-Shell.psm1') -Force -ErrorAction Stop
    Invoke-CodexShell -Arguments ([string[]]@($request.arguments)) -WorkingDirectory $request.workingDirectory
    exit $LASTEXITCODE
} catch {
    Write-Error -ErrorRecord $_ -ErrorAction Continue
    exit 1
}
