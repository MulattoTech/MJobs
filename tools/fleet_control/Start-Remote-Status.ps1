param(
 [string]$Root=$PSScriptRoot,
 [string]$OutputPath='C:\Tools\Dev\MTechLabs-Hub\site\mission-control'
)
$ErrorActionPreference='Stop'
$node=Join-Path $env:ProgramFiles 'nodejs\node.exe'
$exporter=Join-Path $Root 'remote\export-status.js'
if(!(Test-Path -LiteralPath $node -PathType Leaf)){throw 'Node.js is unavailable.'}
if(!(Test-Path -LiteralPath $exporter -PathType Leaf)){throw 'The remote status exporter is unavailable.'}
if(!(Test-Path -LiteralPath $OutputPath -PathType Container)){throw 'Install the remote status page before starting its exporter.'}
$mutex=[Threading.Mutex]::new($false,'Local\MJobsFleetRemoteStatusExporter')
$ownsMutex=$false
try {
 try{$ownsMutex=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$ownsMutex=$true}
 if(!$ownsMutex){return}
 $diagnostics=Join-Path $Root 'diagnostics'
 [void][IO.Directory]::CreateDirectory($diagnostics)
 & $node $exporter --output $OutputPath --watch *> (Join-Path $diagnostics 'remote-exporter.log')
 if($LASTEXITCODE -ne 0){throw 'The remote status exporter stopped with an error. See diagnostics/remote-exporter.log.'}
} finally {
 if($ownsMutex){$mutex.ReleaseMutex()}
 $mutex.Dispose()
}
