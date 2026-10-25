# Offline native installer fixture. Only freshly created temporary profiles change.
# Neither real user profiles nor any Codex/MCP process are loaded or started.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$passed = 0
function Check($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:passed++
}
function BytesKey([string]$File) {
    return [Convert]::ToBase64String([IO.File]::ReadAllBytes($File))
}
$installer = Join-Path $PSScriptRoot 'Install-CodexShellProfiles.ps1'
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('mjobs-codex-shell-installer-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
$firstChanges = 0
$secondChanges = 0
$backupCount = 0
try {
    # Apostrophes, spaces and brackets exercise literal paths and profile quoting.
    $package = Join-Path $fixtureRoot "package's [fixture]"
    [IO.Directory]::CreateDirectory($package) | Out-Null
    foreach ($sourceFile in @('Codex-Shell.psm1','Invoke-CodexShell.ps1')) {
        [IO.File]::Copy((Join-Path $PSScriptRoot $sourceFile), (Join-Path $package $sourceFile))
    }
    # The installer only checks existence. If it ever EXECUTES the guard, fail.
    [IO.File]::WriteAllText((Join-Path $package 'Codex-Profile-Guard.ps1'),
        "throw 'Installer fixture must never invoke a Codex guard or launch.'", [Text.UTF8Encoding]::new($true))
    $modulePath = Join-Path $package 'Codex-Shell.psm1'
    $profilePaths = @(
        (Join-Path $fixtureRoot 'PS5\Microsoft.PowerShell_profile.ps1'),
        (Join-Path $fixtureRoot 'PS7\Microsoft.PowerShell_profile.ps1')
    )
    $originalTexts = @(
        ("# Existing PS5 profile " + [char]0x03A9 + "`r`nfunction title { 'keep PS5' }`r`n"),
        ("# Existing PS7 profile " + [char]0x00E9 + "`nfunction unrelated { 'keep PS7' }`n")
    )
    $encodings = @([Text.UnicodeEncoding]::new($false,$true), [Text.UTF8Encoding]::new($true))
    $originalBytes = @{}
    for ($index=0; $index -lt $profilePaths.Count; $index++) {
        $profile = $profilePaths[$index]
        [IO.Directory]::CreateDirectory((Split-Path -Parent $profile)) | Out-Null
        [IO.File]::WriteAllText($profile, $originalTexts[$index], $encodings[$index])
        $originalBytes[$profile] = BytesKey $profile
    }
    $first = @(& $installer -ModulePath $modulePath -ProfilePaths $profilePaths)
    Check ($first.Count -eq 2) 'First install must return exactly two profile results.'
    $firstChanges = @($first | Where-Object { $_.Changed -eq $true }).Count
    Check ($firstChanges -eq 2) 'First install must change both temporary profiles.'
    $installedBytes = @{}
    for ($index=0; $index -lt $profilePaths.Count; $index++) {
        $profile = $profilePaths[$index]
        $record = @($first | Where-Object { $_.Profile -ceq $profile })
        Check ($record.Count -eq 1) 'Each profile must have one installer result.'
        Check (-not [string]::IsNullOrWhiteSpace($record[0].Backup)) 'Original profile needs a backup.'
        Check ([IO.File]::Exists($record[0].Backup)) 'Backup file must exist.'
        Check ((BytesKey $record[0].Backup) -ceq $originalBytes[$profile]) 'Backup must preserve original bytes and encoding exactly.'
        $text = [IO.File]::ReadAllText($profile)
        Check ($text.StartsWith($originalTexts[$index], [StringComparison]::Ordinal)) 'Unrelated profile text or line endings changed.'
        Check ([regex]::Matches($text, '(?m)^# BEGIN MJOBS MANAGED CODEX SHELL\r?$').Count -eq 1) 'Expected one begin marker.'
        Check ([regex]::Matches($text, '(?m)^# END MJOBS MANAGED CODEX SHELL\r?$').Count -eq 1) 'Expected one end marker.'
        $expectedImport = "Import-Module '" + $modulePath.Replace("'", "''") + "' -Force -ErrorAction Stop"
        Check ($text.Contains($expectedImport)) 'Managed import did not quote the module path correctly.'
        $tokens = $null; $parseErrors = $null
        $null = [Management.Automation.Language.Parser]::ParseFile($profile,[ref]$tokens,[ref]$parseErrors)
        Check (@($parseErrors).Count -eq 0) 'Generated profile failed native PowerShell parsing.'
        $installedBytes[$profile] = BytesKey $profile
    }
    $second = @(& $installer -ModulePath $modulePath -ProfilePaths $profilePaths)
    Check ($second.Count -eq 2) 'Second install must return exactly two profile results.'
    $secondChanges = @($second | Where-Object { $_.Changed -eq $true }).Count
    Check ($secondChanges -eq 0) 'Second install must be a no-op.'
    foreach ($profile in $profilePaths) {
        $record = @($second | Where-Object { $_.Profile -ceq $profile })
        Check ($record.Count -eq 1) 'Expected one second-run result per profile.'
        Check ($null -eq $record[0].Backup) 'No-op install must not create a backup.'
        Check ((BytesKey $profile) -ceq $installedBytes[$profile]) 'No-op install changed profile bytes.'
    }
    $backups = @(Get-ChildItem -LiteralPath $fixtureRoot -Recurse -File -Filter '*.pre-codex-shell-*.bak')
    $backupCount = $backups.Count
    Check ($backupCount -eq 2) 'Exactly two original backups must remain after both installs.'
    $temporaryFiles = @(Get-ChildItem -LiteralPath $fixtureRoot -Recurse -File -Filter '.codex-profile-*.tmp')
    Check ($temporaryFiles.Count -eq 0) 'Atomic-write temporary files were left behind.'
} finally {
    # Exact directory created above, not a caller-supplied path or wildcard.
    if ([IO.Directory]::Exists($fixtureRoot)) {
        [IO.Directory]::Delete($fixtureRoot, $true)
    }
}
Check (-not [IO.Directory]::Exists($fixtureRoot)) 'The isolated fixture directory was not removed.'
[ordered]@{
    SchemaVersion=1
    Suite='codex-shell-installer'
    Status='PASS'
    PowerShellVersion=$PSVersionTable.PSVersion.ToString()
    Passed=$passed
    FirstChanges=$firstChanges
    SecondChanges=$secondChanges
    ExactBackups=$backupCount
    FixtureRemoved=$true
    ModelRequests=0
    NativeProcessesStarted=0
    RealProfilesChanged=0
} | ConvertTo-Json -Compress
