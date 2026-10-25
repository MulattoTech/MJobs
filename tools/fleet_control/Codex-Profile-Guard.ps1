# Loaded by the user's PowerShell 7 profile. Defining these functions starts nothing.
function Assert-CodexFleetUnlocked {
 param([string]$LatchPath='C:\Tools\AO-Fleet-Control\emergency-latch.json')
 if(!(Test-Path -LiteralPath $LatchPath)){return}
 try {
  $latch=Get-Content -LiteralPath $LatchPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
  if($latch.enabled -isnot [bool]){throw 'Invalid emergency state'}
 }catch{throw 'Fleet emergency state is unreadable. Open http://127.0.0.1:18729/fleet/ before launching Codex.'}
 if($latch.enabled){throw 'Codex is stopped by the Fleet emergency latch. After usage resets, explicitly REARM at http://127.0.0.1:18729/fleet/. No Codex backend was launched.'}
}
function New-CodexLocalServerStartInfo {
 param([Parameter(Mandatory)][string]$Executable,[Parameter(Mandatory)][string]$Address)
 if(!$Address.StartsWith('unix://',[StringComparison]::Ordinal)){throw 'A private local Unix socket is required'}
 $info=[Diagnostics.ProcessStartInfo]::new()
 $info.FileName=$Executable
 $info.UseShellExecute=$false
 $info.CreateNoWindow=$true
 $info.WindowStyle=[Diagnostics.ProcessWindowStyle]::Hidden
 $info.ArgumentList.Add('app-server')
 $info.ArgumentList.Add('--listen')
 $info.ArgumentList.Add($Address)
 return $info
}
function Start-CodexLocalServerHidden {
 param([Parameter(Mandatory)][string]$Executable,[Parameter(Mandatory)][string]$Address)
 Assert-CodexFleetUnlocked
 if(!(Test-Path -LiteralPath $Executable -PathType Leaf)){throw 'Codex executable is missing'}
 return [Diagnostics.Process]::Start((New-CodexLocalServerStartInfo -Executable $Executable -Address $Address))
}
