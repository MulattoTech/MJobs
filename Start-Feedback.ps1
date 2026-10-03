param(
    [ValidateRange(1024,65535)][int]$Port = 18702,
    [string]$OutputRoot = '',
    [switch]$NoOpen,
    [switch]$Foreground
)
$ErrorActionPreference = 'Stop'
if (-not $OutputRoot) {
    # Worktrees share the repository's private feedback directory, so the usual
    # root-workspace processing prompt finds GUI handoffs without copying files.
    $dataWorkspace = $PSScriptRoot
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $commonGit = & git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null
        if ($LASTEXITCODE -eq 0 -and $commonGit -and (Test-Path -LiteralPath $commonGit -PathType Container)) {
            $dataWorkspace = Split-Path -Parent $commonGit
        }
    }
    $OutputRoot = Join-Path $dataWorkspace 'outputs\live-feedback'
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$address = "http://127.0.0.1:$Port"
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
if ($Foreground) {
    Push-Location $PSScriptRoot
    try {
        $arguments = @('-m','cli.feedback_gui','--port',[string]$Port,'--output-root',$OutputRoot)
        if ($NoOpen) { $arguments += '--no-open' }
        & $python @arguments
        if ($LASTEXITCODE -ne 0) { throw 'Feedback server stopped with an error.' }
    } finally { Pop-Location }
    exit
}
$state = $null
try { $state = Invoke-RestMethod "$address/api/state" -TimeoutSec 2 } catch { }
if ($null -ne $state) {
    if ($state.service -ne 'mjobs-feedback-gui' -or [System.IO.Path]::GetFullPath($state.output_root) -ne $OutputRoot -or [System.IO.Path]::GetFullPath($state.workspace) -ne $PSScriptRoot) {
        throw "Port $Port belongs to another workspace or service. Close that service or choose another port."
    }
} else {
    # Start-Process joins arguments on Windows; quote each filesystem argument explicitly.
    if ($OutputRoot.Contains('"') -or $python.Contains('"')) { throw 'Paths cannot contain quote characters.' }
    $arguments = '-m cli.feedback_gui --no-open --port ' + $Port + ' --output-root "' + $OutputRoot + '"'
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $OutputRoot 'server.stdout.log') -RedirectStandardError (Join-Path $OutputRoot 'server.stderr.log')
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) { throw "Feedback server could not start. See $OutputRoot\server.stderr.log" }
        try { $state = Invoke-RestMethod "$address/api/state" -TimeoutSec 1; break } catch { Start-Sleep -Milliseconds 200 }
    }
    if ($null -eq $state -or $state.service -ne 'mjobs-feedback-gui' -or [System.IO.Path]::GetFullPath($state.output_root) -ne $OutputRoot -or [System.IO.Path]::GetFullPath($state.workspace) -ne $PSScriptRoot) {
        throw 'Feedback startup could not verify the expected workspace. Existing drafts were preserved.'
    }
}
Write-Host "Development & Feedback: $address"
Write-Host "Private drafts and reports: $OutputRoot"
if (-not $NoOpen) { Start-Process $address }
