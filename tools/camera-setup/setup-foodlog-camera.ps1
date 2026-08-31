[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$ConfigFile,

    [string]$Port
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$utilityRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$firmwarePath = Join-Path $utilityRoot 'firmware\foodlog-camera-fnk0085.bin'
$manifestPath = Join-Path $utilityRoot 'manifest.json'
$provisionerPath = Join-Path $utilityRoot 'provision_camera.py'

function Resolve-PythonCommand {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        & $pythonCommand.Source -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
        if ($LASTEXITCODE -eq 0) {
            return @($pythonCommand.Source)
        }
    }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        & $launcher.Source -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
        if ($LASTEXITCODE -eq 0) {
            return @($launcher.Source, '-3')
        }
    }

    throw @'
Python 3.10 or newer is required for the one-time flasher.
Install Python 3.12 from https://www.python.org/downloads/windows/ or run:
  winget install --id Python.Python.3.12 -e
Then reopen PowerShell and run this script again.
'@
}

function Invoke-Python {
    param(
        [Parameter(Mandatory)]
        [string[]]$PythonCommand,

        [Parameter(ValueFromRemainingArguments)]
        [string[]]$Arguments
    )

    $executable = $PythonCommand[0]
    $prefixArguments = @($PythonCommand | Select-Object -Skip 1)
    & $executable @prefixArguments @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $firmwarePath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $provisionerPath -PathType Leaf)) {
    throw 'This utility is incomplete. Download and extract the FoodLog camera setup ZIP again.'
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$actualHash = (Get-FileHash -LiteralPath $firmwarePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne [string]$manifest.firmware_sha256) {
    throw 'Firmware checksum mismatch. Delete this copy and download the setup ZIP again.'
}

if ([string]::IsNullOrWhiteSpace($ConfigFile)) {
    $ConfigFile = Read-Host 'Path to the private camera setup JSON downloaded from FoodLog'
}
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigFile).Path

$pythonCommand = Resolve-PythonCommand
$venvRoot = Join-Path $env:LOCALAPPDATA 'FoodLogCameraSetup\python-v1'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host 'Preparing the local flashing tools (first run only)...'
    Invoke-Python -PythonCommand $pythonCommand -Arguments @('-m', 'venv', $venvRoot)
}

& $venvPython -c 'import esptool, serial' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing the pinned Espressif flasher and serial library...'
    & $venvPython -m pip install --disable-pip-version-check 'esptool==4.11.0' 'pyserial==3.5'
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not install the local flashing dependencies.'
    }
}

& $venvPython $provisionerPath --config $resolvedConfig --validate-config
if ($LASTEXITCODE -ne 0) {
    throw 'The selected FoodLog camera setup file is invalid.'
}

if ([string]::IsNullOrWhiteSpace($Port)) {
    $portsJson = & $venvPython $provisionerPath --list-ports-json
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not list serial ports.'
    }
    $ports = @($portsJson | ConvertFrom-Json)
    if ($ports.Count -eq 0) {
        throw 'No serial port was found. Connect the Freenove camera by USB and try again.'
    }
    if ($ports.Count -eq 1) {
        $Port = [string]$ports[0].device
    } else {
        Write-Host 'Available serial ports:'
        for ($index = 0; $index -lt $ports.Count; $index++) {
            Write-Host "  $($index + 1). $($ports[$index].device) - $($ports[$index].description)"
        }
        $selection = Read-Host 'Choose the camera port number'
        $selectedIndex = 0
        if (-not [int]::TryParse($selection, [ref]$selectedIndex) -or
            $selectedIndex -lt 1 -or $selectedIndex -gt $ports.Count) {
            throw 'The selected serial-port number is invalid.'
        }
        $Port = [string]$ports[$selectedIndex - 1].device
    }
}

Write-Host "Flashing FoodLog firmware to $Port..."
& $venvPython -m esptool --chip esp32s3 --port $Port --baud 921600 `
    --before default_reset --after hard_reset write_flash --compress 0x0 $firmwarePath
if ($LASTEXITCODE -ne 0) {
    throw 'Flashing failed. If prompted by Windows, reconnect the board and retry.'
}

Start-Sleep -Seconds 2
& $venvPython $provisionerPath --config $resolvedConfig --port $Port
if ($LASTEXITCODE -ne 0) {
    throw 'Firmware flashed, but local provisioning failed. Run this script again with the same setup file.'
}

Write-Host ''
Write-Host 'FoodLog camera setup is complete.' -ForegroundColor Green
Write-Host 'Keep the private setup JSON safe until the website reports the first image, then delete it.'
