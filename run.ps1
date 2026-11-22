# Start Abstract Search & Export in Docker from a checkout of this repository. Same commands as
# search-export\run-docker.ps1: .\run.ps1, .\run.ps1 link, .\run.ps1 stop, .\run.ps1 terminal, .\run.ps1 <command ...>
& (Join-Path $PSScriptRoot 'search-export\run-docker.ps1') @args
exit $LASTEXITCODE
