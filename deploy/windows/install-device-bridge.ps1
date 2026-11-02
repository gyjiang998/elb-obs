param(
    [Parameter(Mandatory = $true)][string]$PlatformUrl,
    [Parameter(Mandatory = $true)][string]$HdcPath,
    [string]$BridgeName = "windows-phone"
)

$ErrorActionPreference = "Stop"
$BridgeRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$BridgeSource = Join-Path $BridgeRoot "device-bridge"
$StateRoot = Join-Path $env:LOCALAPPDATA "HeterogeneousDeviceBridge"
$VenvRoot = Join-Path $StateRoot "venv"
$Python = Join-Path $VenvRoot "Scripts\python.exe"

if (-not (Test-Path $HdcPath -PathType Leaf)) {
    throw "HDC executable not found: $HdcPath"
}

New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
if (-not (Test-Path $Python -PathType Leaf)) {
    py -m venv $VenvRoot
}

& $Python -m pip install --disable-pip-version-check -r (Join-Path $BridgeSource "requirements.txt")
$PairArgs = @(
    (Join-Path $BridgeSource "hdc_device_bridge.py"),
    "pair",
    "--platform-url", $PlatformUrl,
    "--hdc", $HdcPath,
    "--name", $BridgeName
)
& $Python @PairArgs

$DoctorArgs = @(
    (Join-Path $BridgeSource "hdc_device_bridge.py"),
    "doctor"
)
& $Python @DoctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Device Bridge was configured, but its startup diagnosis failed."
}

Write-Host "Device Bridge configured. Keep the following process running:"
Write-Host "& `"$(Join-Path $PSScriptRoot 'run-device-bridge.ps1')`""
Write-Host "For a full Windows-to-device transfer check:"
Write-Host "& `"$(Join-Path $PSScriptRoot 'test-device-bridge.ps1')`" -TransferCheck"
