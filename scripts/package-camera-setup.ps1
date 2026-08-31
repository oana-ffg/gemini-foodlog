[CmdletBinding()]
param(
    [string]$Version = '0.1.1',
    [string]$FirmwarePath = 'clients\camera-firmware\.pio\build\freenove-esp32-s3-wroom\foodlog-camera-fnk0085.bin',
    [string]$OutputPath = 'assets\brand\downloads\foodlog-camera-setup.zip'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$resolvedFirmware = (Resolve-Path -LiteralPath (Join-Path $repositoryRoot $FirmwarePath)).Path
$sourceRoot = Join-Path $repositoryRoot 'tools\camera-setup'
$resolvedOutput = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $OutputPath))
$outputDirectory = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null

$temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$staging = Join-Path $temporaryRoot ("foodlog-camera-setup-" + [guid]::NewGuid().ToString('N'))
$stagingRoot = [System.IO.Path]::GetFullPath($staging)
if (-not $stagingRoot.StartsWith($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create a staging directory outside the operating-system temporary directory.'
}

try {
    $firmwareDirectory = Join-Path $stagingRoot 'firmware'
    New-Item -ItemType Directory -Path $firmwareDirectory -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'README.md') -Destination $stagingRoot
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'setup-foodlog-camera.ps1') -Destination $stagingRoot
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'provision_camera.py') -Destination $stagingRoot
    $stagedFirmware = Join-Path $firmwareDirectory 'foodlog-camera-fnk0085.bin'
    Copy-Item -LiteralPath $resolvedFirmware -Destination $stagedFirmware

    $firmwareHash = (Get-FileHash -LiteralPath $stagedFirmware -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest = [ordered]@{
        package_version = $Version
        firmware_version = "foodlog-fnk0085-$Version"
        supported_board = 'Freenove FNK0085 ESP32-S3 WROOM N8R8'
        firmware_sha256 = $firmwareHash
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText(
        (Join-Path $stagingRoot 'manifest.json'),
        $manifest + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )

    if (Test-Path -LiteralPath $resolvedOutput) {
        Remove-Item -LiteralPath $resolvedOutput -Force
    }
    Compress-Archive -Path (Join-Path $stagingRoot '*') -DestinationPath $resolvedOutput -CompressionLevel Optimal
    $zipHash = (Get-FileHash -LiteralPath $resolvedOutput -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText(
        "$resolvedOutput.sha256",
        "$zipHash  foodlog-camera-setup.zip$([Environment]::NewLine)",
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Output "Packaged $resolvedOutput"
    Write-Output "Firmware SHA-256: $firmwareHash"
    Write-Output "ZIP SHA-256: $zipHash"
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        $verifiedStaging = [System.IO.Path]::GetFullPath($stagingRoot)
        if (-not $verifiedStaging.StartsWith($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
            -not (Split-Path -Leaf $verifiedStaging).StartsWith('foodlog-camera-setup-')) {
            throw 'Refusing to remove an unverified staging directory.'
        }
        Remove-Item -LiteralPath $verifiedStaging -Recurse -Force
    }
}
