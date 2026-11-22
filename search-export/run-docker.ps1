<#
Abstract Search & Export in Docker, for Windows (PowerShell 5.1 or newer, Docker Desktop).

  .\run-docker.ps1                 start the browser app and show the link to open
  .\run-docker.ps1 link            show a new link (the app keeps running)
  .\run-docker.ps1 logs            follow the app's log
  .\run-docker.ps1 stop            stop the app
  .\run-docker.ps1 terminal        the guided terminal instead of the browser app
  .\run-docker.ps1 mcp             the MCP server on standard input and output, for an AI assistant
  .\run-docker.ps1 <command ...>   one command, for example: .\run-docker.ps1 datasets

Settings, all optional (environment variables):
  ABX_IMAGE      the image to run. Default: built from this folder when it holds a Dockerfile, otherwise pulled.
  ABX_PORT       the port on this computer (default 8765)
  ABX_EXPORTS    where exports are written on this computer (default: AbstractExports in your user folder)
To connect without typing in the page, set these first: ABSTRACT_VENDOR_ACCOUNT_ID, ABSTRACT_EXPECT_TENANT,
ABSTRACT_API_KEY. Docker keeps them in the container's settings until it is removed ("stop" removes it); typing the
key in the page, or a mounted secret file, keeps it out of them.

If Windows refuses to run the script: powershell -ExecutionPolicy Bypass -File .\run-docker.ps1
#>
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Name = if ($env:ABX_CONTAINER) { $env:ABX_CONTAINER } else { 'abstract-search' }
$Port = if ($env:ABX_PORT) { $env:ABX_PORT } else { '8765' }
$Out = if ($env:ABX_EXPORTS) { $env:ABX_EXPORTS } else { Join-Path $env:USERPROFILE 'AbstractExports' }
$Published = if ($env:ABX_PUBLISHED_IMAGE) { $env:ABX_PUBLISHED_IMAGE } else { 'ghcr.io/iamabs3c/abstract-search-export:latest' }
$script:Image = $env:ABX_IMAGE

function Stop-With([string]$Message) { Write-Host $Message -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Stop-With 'Docker was not found. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), start it, then run this again.'
}
$ErrorActionPreference = 'Continue'
docker info *> $null
if ($LASTEXITCODE -ne 0) {
  Stop-With 'Docker is installed but not running. Start Docker Desktop, wait until it says it is running, then run this again.'
}

function Test-Running {
  $state = docker inspect -f '{{.State.Running}}' $Name 2>$null
  return ($LASTEXITCODE -eq 0 -and "$state".Trim() -eq 'true')
}

function Resolve-Image {
  if ($script:Image) { return }
  if ((Test-Path (Join-Path $Here 'Dockerfile')) -and (Test-Path (Join-Path $Here 'abstract_search.py'))) {
    $version = (Get-Content (Join-Path $Here 'VERSION') -Raw).Trim()
    $script:Image = "abstract-search-export:$version"
    docker image inspect $script:Image *> $null
    if ($LASTEXITCODE -ne 0) {
      Write-Host 'Building the image from this folder (about a minute, the first time only)...'
      docker build -q -t $script:Image $Here | Out-Null
      if ($LASTEXITCODE -ne 0) { Stop-With 'The image could not be built. Run: docker build -t abstract-search-export . to see why.' }
    }
  } else {
    $script:Image = $Published
    Write-Host "Getting $($script:Image)..."
    docker pull -q $script:Image | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-With "Could not get $($script:Image). Check your network, or set ABX_IMAGE to an image you have." }
  }
}

# The flags every container gets: this computer's port only, the exports folder, no privileges, nothing written
# anywhere else.
function Get-Common {
  return @('--read-only', '--tmpfs', '/tmp', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
           '-v', "${Out}:/exports",
           '-e', 'ABSTRACT_VENDOR_ACCOUNT_ID', '-e', 'ABSTRACT_EXPECT_TENANT', '-e', 'ABSTRACT_API_KEY', '-e', 'ABSTRACT_BASE_URL')
}

function Get-LastLink {
  $log = (docker logs $Name 2>&1 | Out-String)
  $found = [regex]::Matches($log, 'https?://\S*\?code=[A-Za-z0-9_-]+')
  if ($found.Count -eq 0) { return $null }
  return $found[$found.Count - 1].Value
}

function Show-Link {
  $link = Get-LastLink
  if (-not $link) { Stop-With "The app did not print a link. See its log: docker logs $Name" }
  Write-Host ''
  Write-Host '  Abstract Search & Export is running. Open this link in your browser:'
  Write-Host ''
  Write-Host "    $link"
  Write-Host ''
  Write-Host '  The link works once. For another one: .\run-docker.ps1 link      To stop the app: .\run-docker.ps1 stop'
  Write-Host "  Exports are written to: $Out"
  Write-Host ''
  if ($env:ABX_NO_BROWSER -ne '1') { try { Start-Process $link } catch { } }
}

function New-Link {
  if (-not (Test-Running)) { Stop-With 'The app is not running. Start it with: .\run-docker.ps1' }
  $before = Get-LastLink
  docker exec $Name python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8765/api/relink', data=b'{}', headers={'Content-Type': 'application/json'}), timeout=5)" | Out-Null
  for ($i = 0; $i -lt 20 -and (Get-LastLink) -eq $before; $i++) { Start-Sleep -Milliseconds 300 }
  Show-Link
}

function Start-App {
  if (Test-Running) { Write-Host 'The app is already running.'; New-Link; return }
  Resolve-Image
  New-Item -ItemType Directory -Force -Path $Out | Out-Null
  docker rm $Name *> $null
  $common = Get-Common
  docker run -d --name $Name -p "127.0.0.1:${Port}:8765" -e "ABX_WEB_URL=http://127.0.0.1:${Port}/" @common $script:Image | Out-Null
  if ($LASTEXITCODE -ne 0) { Stop-With 'The app could not start. Is another program using the port? Set ABX_PORT to another number and try again.' }
  for ($i = 0; $i -lt 60; $i++) {
    if (Get-LastLink) { break }
    if (-not (Test-Running)) { docker logs --tail 20 $Name; Stop-With 'The app stopped while starting. Its last messages are above.' }
    Start-Sleep -Milliseconds 500
  }
  Show-Link
}

function Invoke-Once([string]$Mode, [string[]]$Rest) {
  Resolve-Image
  New-Item -ItemType Directory -Force -Path $Out | Out-Null
  $common = Get-Common
  $tty = if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) { '-i' } else { '-it' }
  if ($Mode -eq 'terminal') {
    docker run --rm $tty @common --entrypoint python $script:Image /app/abstract_search.py
  } else {
    docker run --rm $tty @common $script:Image @Rest
  }
  exit $LASTEXITCODE
}

$command = if ($args.Count -gt 0) { [string]$args[0] } else { 'start' }
switch ($command) {
  'start'    { Start-App }
  'link'     { New-Link }
  'logs'     { docker logs -f $Name }
  'stop'     { if (Test-Running) { docker rm -f $Name | Out-Null; Write-Host 'Stopped.' } else { docker rm $Name *> $null; Write-Host 'The app was not running.' } }
  'terminal' { Invoke-Once 'terminal' @() }
  { $_ -in @('-h', '--help', 'help') } { Get-Help $MyInvocation.MyCommand.Path }
  default    { Invoke-Once 'cli' ([string[]]$args) }
}
