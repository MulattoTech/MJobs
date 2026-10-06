param(
    [ValidateSet('PERSONAL','FIXTURE')][string]$Mode = 'PERSONAL',
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$feedbackRoot = Join-Path $root 'outputs\live-feedback'
$appUrl = if ($Mode -eq 'PERSONAL') { 'http://127.0.0.1:18701' } else { 'http://127.0.0.1:18700' }
$feedbackUrl = 'http://127.0.0.1:18702'

Push-Location $root
try {
    if ($Mode -eq 'PERSONAL') {
        & (Join-Path $root 'Start-MJobs.ps1') -Personal -NoOpen
    } else {
        & (Join-Path $root 'Start-MJobs.ps1') -NoOpen
    }
    if ($LASTEXITCODE -ne 0) { throw 'MJobs startup failed.' }

    & (Join-Path $root 'Start-Feedback.ps1') -NoOpen
    if ($LASTEXITCODE -ne 0) { throw 'Feedback workstation startup failed.' }

    New-Item -ItemType Directory -Force -Path $feedbackRoot | Out-Null
    $branch = (& git branch --show-current).Trim()
    $head = (& git rev-parse HEAD).Trim()
    $review = [ordered]@{
        schema_version = 1
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        mode = $Mode
        workspace = $root
        branch = $branch
        head = $head
        app_url = $appUrl
        feedback_url = $feedbackUrl
        guide = 'docs/GUIDED_PRODUCT_REVIEW.md'
        facilitator_prompt = 'prompts/VS_CODE_GUIDED_REVIEW.md'
        status = 'ACTIVE'
    }
    $review | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $feedbackRoot 'ACTIVE_REVIEW.json')

    Write-Host ''
    Write-Host "Guided MJobs review is ready."
    Write-Host "Mode:      $Mode"
    Write-Host "App:       $appUrl"
    Write-Host "Feedback:  $feedbackUrl"
    Write-Host "Guide:     docs\GUIDED_PRODUCT_REVIEW.md"
    Write-Host "Luna:      prompts\VS_CODE_GUIDED_REVIEW.md"
    Write-Host ''
    Write-Host 'Create one Ready feedback report per distinct finding.'

    if (-not $NoOpen) {
        if (Get-Command code -ErrorAction SilentlyContinue) {
            Start-Process -FilePath 'code' -ArgumentList @('-r', (Join-Path $root 'docs\GUIDED_PRODUCT_REVIEW.md'))
        }
        Start-Process $appUrl
        Start-Process $feedbackUrl
    }
} finally {
    Pop-Location
}
