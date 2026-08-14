# Starts the stock app. First run creates a virtual environment and installs
# the dependencies; later runs just start the server.
#
#   .\run.ps1              -- listen on the network, port 8000
#   .\run.ps1 -Port 8080   -- different port
#   .\run.ps1 -LocalOnly   -- only this machine can reach it
#   .\run.ps1 -Reload      -- restart automatically when the code changes

param(
    [int]$Port = 8000,
    [switch]$LocalOnly,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Setting up the virtual environment (one-off, takes a minute)..." -ForegroundColor Cyan
    py -3 -m venv $venv
    & $python -m pip install --upgrade pip --quiet
    & $python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
    Write-Host "Done." -ForegroundColor Green
}

$listen = if ($LocalOnly) { "127.0.0.1" } else { "0.0.0.0" }

Write-Host ""
Write-Host "IT stock app starting." -ForegroundColor Green
Write-Host "  On this machine:  http://localhost:$Port"
if (-not $LocalOnly) {
    # Show the LAN address colleagues should use, rather than making someone
    # go and run ipconfig.
    $addresses = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -ne "WellKnown" } |
        Select-Object -ExpandProperty IPAddress
    foreach ($ip in $addresses) {
        Write-Host "  On the network:   http://${ip}:$Port"
    }
    Write-Host "  (If colleagues can't reach it, see the firewall note in README.md.)" -ForegroundColor DarkGray
}
Write-Host "  Stop with Ctrl+C."
Write-Host ""

$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", $listen, "--port", "$Port")
if ($Reload) { $uvicornArgs += "--reload" }

& $python @uvicornArgs
