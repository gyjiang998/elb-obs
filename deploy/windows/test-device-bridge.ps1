param(
    [switch]$TransferCheck,
    [string]$DeviceSerial = ""
)

$ErrorActionPreference = "Stop"

$BridgeRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$BridgeSource = Join-Path $BridgeRoot "device-bridge"
$Python = Join-Path $env:LOCALAPPDATA "HeterogeneousDeviceBridge\venv\Scripts\python.exe"
$Config = Join-Path $env:LOCALAPPDATA "HeterogeneousDeviceBridge\bridge.json"

if (-not (Test-Path $Python -PathType Leaf)) {
    throw "Device Bridge environment is missing. Run install-device-bridge.ps1 first."
}
if (-not (Test-Path $Config -PathType Leaf)) {
    throw "Device Bridge config is missing: $Config"
}

$DoctorArgs = @(
    (Join-Path $BridgeSource "hdc_device_bridge.py"),
    "doctor",
    "--config", $Config
)
if ($TransferCheck) {
    $DoctorArgs += "--transfer-check"
}
if ($DeviceSerial) {
    $DoctorArgs += @("--device-serial", $DeviceSerial)
}

Write-Host "Running Device Bridge diagnosis:"
Write-Host "& `"$Python`" $($DoctorArgs -join ' ')"
& $Python @DoctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Device Bridge diagnosis failed. Inspect status, failure_domain and command output above."
}
