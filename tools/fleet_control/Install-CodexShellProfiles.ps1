# Installs a managed import into BOTH user profiles, preserving all unrelated text.
# Does not launch Codex, alter security policy, modify MCP config, or start a server.
[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$ModulePath = (Join-Path $PSScriptRoot 'Codex-Shell.psm1'),
    [string[]]$ProfilePaths = @(
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1'),
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'PowerShell\Microsoft.PowerShell_profile.ps1')
    )
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ModulePath -PathType Leaf)) { throw 'Codex shell module is missing.' }
$moduleFile = (Resolve-Path -LiteralPath $ModulePath).ProviderPath
$moduleDirectory = Split-Path -Parent $moduleFile
foreach ($required in @('Invoke-CodexShell.ps1','Codex-Profile-Guard.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $moduleDirectory $required) -PathType Leaf)) {
        throw ('Required companion file is missing: ' + $required)
    }
}
$begin = '# BEGIN MJOBS MANAGED CODEX SHELL'
$end = '# END MJOBS MANAGED CODEX SHELL'
$newline = "`r`n"
$escapedPath = $moduleFile.Replace("'", "''")
$block = $begin + $newline + "Import-Module '$escapedPath' -Force -ErrorAction Stop" + $newline + $end + $newline
foreach ($profileFile in $ProfilePaths) {
    $exists = Test-Path -LiteralPath $profileFile -PathType Leaf
    $original = if ($exists) { [IO.File]::ReadAllText($profileFile) } else { '' }
    $pattern = '(?ms)^' + [regex]::Escape($begin) + '\r?\n.*?^' + [regex]::Escape($end) + '(?:\r?\n|$)'
    $matches = [regex]::Matches($original, $pattern)
    if ($matches.Count -gt 1 -or
        (($original.Contains($begin) -or $original.Contains($end)) -and $matches.Count -ne 1)) {
        throw ('The managed markers are malformed in ' + $profileFile + '; no changes made to this profile.')
    }
    $base = [regex]::Replace($original, $pattern, '')
    # The import is deliberately appended: it supersedes the old Codex function
    # without deleting unrelated profile content or heuristically removing code.
    if ($base.Length -and -not $base.EndsWith("`n")) { $base += $newline }
    $updated = $base + $block
    if ($updated -ceq $original) {
        [pscustomobject]@{Profile=$profileFile; Changed=$false; Backup=$null}
        continue
    }
    if ($PSCmdlet.ShouldProcess($profileFile, 'Back up profile and install shared Codex import')) {
        $directory = Split-Path -Parent $profileFile
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
        $backup = $null
        if ($exists) {
            $backup = $profileFile + '.pre-codex-shell-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N') + '.bak'
            Copy-Item -LiteralPath $profileFile -Destination $backup -ErrorAction Stop
        }
        $temporary = Join-Path $directory ('.codex-profile-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        try {
            [IO.File]::WriteAllText($temporary, $updated, [Text.UTF8Encoding]::new($true))
            if ($exists) { [IO.File]::Replace($temporary, $profileFile, [NullString]::Value) }
            else { [IO.File]::Move($temporary, $profileFile) }
        } finally {
            if (Test-Path -LiteralPath $temporary -PathType Leaf) {
                Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
            }
        }
        [pscustomobject]@{Profile=$profileFile; Changed=$true; Backup=$backup}
    }
}
