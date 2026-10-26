param(
 [string]$Root='C:\Tools\AO-Fleet-Control',
 [string]$HubRoot='C:\Tools\Dev\MTechLabs-Hub',
 [switch]$InstallTask,
 [switch]$RunNow
)
$ErrorActionPreference='Stop'
$source=Join-Path $Root 'remote\public'
$site=Join-Path $HubRoot 'site'
$index=Join-Path $site 'index.html'
$destination=Join-Path $site 'mission-control'
$assets=@('index.html','app.js','style.css','agent-details.js','remote-controls.js')
if(!(Test-Path -LiteralPath $index -PathType Leaf)){throw 'The existing M-TechLabs hub was not found.'}
foreach($file in $assets){if(!(Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)){throw "Remote view asset missing: $file"}}
$cardPath=Join-Path $Root 'remote\hub-card.html'
if(!(Test-Path -LiteralPath $cardPath -PathType Leaf)){throw 'Remote view hub card is missing.'}
$html=[IO.File]::ReadAllText($index)
$newHtml=$html
if(!$html.Contains('id="fleet-mission-control-card"')){
 $anchor=[regex]::new('(?s)<section[^>]+aria-label="Running apps"[^>]*>.*?<div class="cards">')
 if($anchor.Matches($html).Count -ne 1){throw 'The hub layout changed. Add the remote/hub-card.html link to its service cards manually.'}
 $card=[IO.File]::ReadAllText($cardPath)
 $newHtml=$anchor.Replace($html,[Text.RegularExpressions.MatchEvaluator]{param($match) $match.Value+"`n"+$card},1)
}
$backup=Join-Path $HubRoot ('backups\mission-control-'+(Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
[void][IO.Directory]::CreateDirectory($backup)
Copy-Item -LiteralPath $index -Destination (Join-Path $backup 'hub-index.html')
if(Test-Path -LiteralPath $destination -PathType Container){Copy-Item -LiteralPath $destination -Destination (Join-Path $backup 'mission-control') -Recurse}
[void][IO.Directory]::CreateDirectory($destination)
foreach($file in $assets){Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $destination $file) -Force}
if($newHtml -cne $html){[IO.File]::WriteAllText($index,$newHtml,[Text.UTF8Encoding]::new($false))}
if($InstallTask){& (Join-Path $Root 'Install-Fleet-Remote-Task.ps1') -Root $Root -OutputPath $destination -RunNow:$RunNow}
[pscustomobject]@{installed=$true;url='https://m-techlabs.com/mission-control/';backup=$backup;taskInstalled=[bool]$InstallTask}|ConvertTo-Json -Compress
