# Deterministic, offline plan/bridge serialization checks. Never invokes Codex.
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Codex-Shell.psm1') -Force
$passed = 0
function Check($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:passed++
}
$directory = 'C:\Synthetic Repository'
$cases = @(
    @{Args=@(); Shared=$true; Unlocked=$true; Directory=$true},
    @{Args=@('--help'); Shared=$false; Unlocked=$false; Directory=$false},
    @{Args=@('--version'); Shared=$false; Unlocked=$false; Directory=$false},
    @{Args=@('agents'); Shared=$true; Unlocked=$true; Directory=$true},
    @{Args=@('resume','--last'); Shared=$true; Unlocked=$true; Directory=$true},
    @{Args=@('exec','--help'); Shared=$false; Unlocked=$false; Directory=$false},
    @{Args=@('exec','test prompt'); Shared=$false; Unlocked=$true; Directory=$false},
    @{Args=@('-m','gpt-6.1-sol','exec','test prompt'); Shared=$false; Unlocked=$true; Directory=$false},
    @{Args=@('-m','gpt-6.1-sol','agents'); Shared=$true; Unlocked=$true; Directory=$true},
    @{Args=@('--remote','unix://C:\private.sock'); Shared=$false; Unlocked=$true; Directory=$false},
    @{Args=@('--no-daemon'); Shared=$false; Unlocked=$true; Directory=$false},
    @{Args=@('--worktree'); Shared=$false; Unlocked=$true; Directory=$false},
    @{Args=@('-C','C:\Repository With Spaces'); Shared=$true; Unlocked=$true; Directory=$false},
    @{Args=@('--cd=C:\Repository'); Shared=$true; Unlocked=$true; Directory=$false},
    @{Args=@('-CC:\Repository'); Shared=$true; Unlocked=$true; Directory=$false},
    @{Args=@('-c','model_reasoning_effort="high"'); Shared=$true; Unlocked=$true; Directory=$true},
    @{Args=@('--','--help'); Shared=$true; Unlocked=$true; Directory=$true},
    @{Args=@('-c','--help'); Shared=$true; Unlocked=$true; Directory=$true},
    @{Args=@('login'); Shared=$false; Unlocked=$false; Directory=$false}
)
foreach ($case in $cases) {
    $plan = Get-CodexLaunchPlan -Arguments $case.Args -WorkingDirectory $directory
    Check ($plan.UseSharedServer -eq $case.Shared) ('Shared server plan: ' + ($case.Args -join '|'))
    Check ($plan.RequireUnlocked -eq $case.Unlocked) ('Latch plan: ' + ($case.Args -join '|'))
    Check ($plan.AddDefaultDirectory -eq $case.Directory) ('Directory plan: ' + ($case.Args -join '|'))
    Check (($plan.Arguments | ConvertTo-Json -Compress) -ceq ($case.Args | ConvertTo-Json -Compress)) 'Argument list changed.'
}
$roundTrip = @('', 'quote "inside"', 'trailing\', 'C:\space here\', '--', '$literal', '`literal', "one`ntwo")
$request = @{arguments=$roundTrip;workingDirectory=$directory}
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Compress -Depth 4)))
$decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) | ConvertFrom-Json
Check (($decoded.arguments | ConvertTo-Json -Compress) -ceq ($roundTrip | ConvertTo-Json -Compress)) 'Bridge argument serialization changed.'
Check ($decoded.workingDirectory -ceq $directory) 'Bridge working directory changed.'
[ordered]@{
    SchemaVersion=1
    Suite='codex-shell-plan'
    Status='PASS'
    PowerShellVersion=$PSVersionTable.PSVersion.ToString()
    Passed=$passed
    ModelRequests=0
    NativeProcessesStarted=0
    RealProfilesChanged=0
    RemoteFilesChanged=0
} | ConvertTo-Json -Compress
