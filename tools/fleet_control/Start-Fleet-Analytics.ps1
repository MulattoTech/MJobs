param([string]$Root='C:\Tools\AO-Fleet-Control',[string]$Publish='C:\Tools\Dev\MTechLabs-Hub\site\mission-control\analytics')
$ErrorActionPreference='Stop'
$python='C:\Tools\Dev\MJobs\.venv\Scripts\python.exe'
$script=Join-Path $Root 'collect-analytics.py'
if(!(Test-Path $python) -or !(Test-Path $script)){throw 'Analytics runtime is unavailable'}
$mutex=[Threading.Mutex]::new($false,'Local\MJobsFleetAnalyticsCollector')
$owned=$false
try {
 try {$owned=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owned=$true}
 if(!$owned){exit 0}
 New-Item -ItemType Directory -Force -Path (Join-Path $Root 'telemetry'),$Publish|Out-Null
 $env:PYTHONUTF8='1'
 & $python $script --root $Root --publish $Publish --loop *> (Join-Path $Root 'telemetry\collector.log')
 exit $LASTEXITCODE
} finally {if($owned){$mutex.ReleaseMutex()};$mutex.Dispose()}
