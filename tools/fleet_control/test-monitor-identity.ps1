$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Quota-Monitor-Identity.ps1')
$testRoot='C:\Fleet Control Test'
$testNow=[DateTimeOffset]::Parse('2026-10-25T01:00:00Z')
$canonical={param([string]$Path) [IO.Path]::GetFullPath($Path)}
function Fixture {
 $childPath=Join-Path $testRoot 'codex-monitor\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe'
 $guardPath='C:\Program Files\nodejs\node.exe'
 $guardCreated=$testNow.AddMinutes(-3).UtcDateTime
 $childCreated=$testNow.AddMinutes(-2).UtcDateTime
 [pscustomobject]@{
  Details=[pscustomobject]@{Name='codex.exe';ProcessId=[uint32]4202;ParentProcessId=[uint32]4201;ExecutablePath=$childPath;CommandLine='"'+$childPath+'" app-server --listen stdio://';CreationDate=$childCreated}
  Guard=[pscustomobject]@{Name='node.exe';ProcessId=[uint32]4201;ExecutablePath=$guardPath;CommandLine='"'+$guardPath+'" "'+(Join-Path $testRoot 'quota-guard.js')+'"';CreationDate=$guardCreated}
  Status=[pscustomobject]@{pid=[uint32]4201;heartbeatAt=$testNow.AddSeconds(-5).ToString('o');monitorIdentity=[pscustomobject]@{
   monitorPid=[uint32]4202;monitorCreationKey=([DateTimeOffset]$childCreated).ToUniversalTime().Ticks.ToString();guardPid=[uint32]4201;guardCreationKey=([DateTimeOffset]$guardCreated).ToUniversalTime().Ticks.ToString();guardExecutablePath=$guardPath}}
 }
}
$script:count=0
function Case {
 param([string]$Name,[bool]$Expected,[scriptblock]$Mutate={},[scriptblock]$Resolve=$canonical)
 $f=Fixture
 & $Mutate $f
 $actual=Test-FleetQuotaMonitorIdentity -Details $f.Details -Guard $f.Guard -Status $f.Status -Root $testRoot -Now $testNow -Canonicalize $Resolve
 if($actual -isnot [bool] -or $actual -ne $Expected){throw "FAIL $($Name): expected $Expected; got $actual"}
 $script:count++
 Write-Output "PASS $Name"
}
Case 'exact isolated instance and quoted paths' $true
Case 'global shared app-server targets remain' $false {param($f) $f.Details.ExecutablePath='C:\Program Files\Codex\codex.exe';$f.Details.CommandLine='"C:\Program Files\Codex\codex.exe" app-server --listen stdio://'}
Case 'exec-server is not exempt' $false {param($f) $f.Details.CommandLine='"'+$f.Details.ExecutablePath+'" exec-server'}
Case 'remote frontend is not exempt' $false {param($f) $f.Details.CommandLine='"'+$f.Details.ExecutablePath+'" --remote unix://C:\Users\Fixture\agents-local.sock --yolo'}
Case 'extra remote flag is rejected' $false {param($f) $f.Details.CommandLine+=' --remote tcp://127.0.0.1:9000'}
Case 'different parent is rejected' $false {param($f) $f.Details.ParentProcessId=[uint32]9001}
Case 'reused child PID is rejected' $false {param($f) $f.Details.CreationDate=$f.Details.CreationDate.AddSeconds(1)}
Case 'reused guard PID is rejected' $false {param($f) $f.Guard.CreationDate=$f.Guard.CreationDate.AddSeconds(1)}
Case 'status guard PID must match' $false {param($f) $f.Status.pid=[uint32]9999}
Case 'stale heartbeat rejected' $false {param($f) $f.Status.heartbeatAt=$testNow.AddSeconds(-31).ToString('o')}
Case 'future heartbeat rejected' $false {param($f) $f.Status.heartbeatAt=$testNow.AddSeconds(1).ToString('o')}
Case 'missing identity rejected' $false {param($f) $f.Status.PSObject.Properties.Remove('monitorIdentity')}
Case 'missing executable rejected' $false {param($f) $f.Details.PSObject.Properties.Remove('ExecutablePath')}
Case 'creation key exact string required' $false {param($f) $f.Status.monitorIdentity.monitorCreationKey=[double]$f.Status.monitorIdentity.monitorCreationKey}
Case 'extra guard arguments rejected' $false {param($f) $f.Guard.CommandLine+=' --unverified-option'}
Case 'canonicalization failure rejected' $false -Resolve {param($Path) throw 'Synthetic access denied'}
Case 'reparse target rejected' $false -Resolve {param([string]$Path) if($Path.EndsWith('\codex.exe',[StringComparison]::OrdinalIgnoreCase)){return 'C:\Outside-Isolated-Monitor\codex.exe'};return [IO.Path]::GetFullPath($Path)}
Case 'different listener rejected' $false {param($f) $f.Details.CommandLine='"'+$f.Details.ExecutablePath+'" app-server --listen tcp://127.0.0.1:9999'}
Case 'heartbeat at 30 seconds allowed' $true {param($f) $f.Status.heartbeatAt=$testNow.AddSeconds(-30).ToString('o')}
Case 'real JSON deserialization preserves valid identity' $true {param($f) $f.Status=$f.Status|ConvertTo-Json -Depth 8|ConvertFrom-Json}
Case 'deserialized stale heartbeat rejected' $false {param($f) $f.Status.heartbeatAt=$testNow.AddSeconds(-31).ToString('o');$f.Status=$f.Status|ConvertTo-Json -Depth 8|ConvertFrom-Json}
Case 'deserialized future heartbeat rejected' $false {param($f) $f.Status.heartbeatAt=$testNow.AddSeconds(1).ToString('o');$f.Status=$f.Status|ConvertTo-Json -Depth 8|ConvertFrom-Json}
Case 'timezone-free DateTime rejected' $false {param($f) $f.Status.heartbeatAt=[DateTime]::SpecifyKind($testNow.UtcDateTime,[DateTimeKind]::Unspecified)}
Write-Output "PASS all $script:count identity cases; no agents launched or terminated"
