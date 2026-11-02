param(
    [Parameter(Position = 0)][string]$PlatformUrl = "",
    [string]$HdcPath = "",
    [string]$BridgeName = ""
)

$ErrorActionPreference = "Stop"

$BridgeRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$BridgeSource = Join-Path $BridgeRoot "device-bridge"
$BridgeProgram = Join-Path $BridgeSource "hdc_device_bridge.py"
$Requirements = Join-Path $BridgeSource "requirements.txt"
$StateRoot = Join-Path $env:LOCALAPPDATA "HeterogeneousDeviceBridge"
$VenvRoot = Join-Path $StateRoot "venv"
$Python = Join-Path $VenvRoot "Scripts\python.exe"
$Config = Join-Path $StateRoot "bridge.json"
$RequirementsStamp = Join-Path $StateRoot "requirements.sha256"

function Read-ExistingBridgeConfig {
    if (-not (Test-Path $Config -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -Raw -Path $Config | ConvertFrom-Json
    }
    catch {
        Write-Warning "Ignoring an unreadable existing Bridge config: $Config"
        return $null
    }
}

function Resolve-HdcExecutable([object]$ExistingConfig) {
    $Candidates = @()
    if ($HdcPath) {
        $Candidates += $HdcPath
    }
    if ($ExistingConfig -and $ExistingConfig.hdc_path) {
        $Candidates += [string]$ExistingConfig.hdc_path
    }
    foreach ($CommandName in @("hdc.exe", "hdc")) {
        $Command = Get-Command $CommandName -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source) {
            $Candidates += $Command.Source
        }
    }
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path $Candidate -PathType Leaf)) {
            return (Resolve-Path $Candidate).Path
        }
    }
    throw "HDC was not found. Add hdc.exe to PATH, or pass -HdcPath C:\path\to\hdc.exe once."
}

function Resolve-PythonLauncher {
    foreach ($CommandName in @("py", "python", "python3")) {
        $Command = Get-Command $CommandName -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source) {
            return $Command.Source
        }
    }
    throw "Python 3 was not found. Install Python 3 and make py.exe or python.exe available in PATH."
}

if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is unavailable; the Windows Bridge cannot create its user-local runtime."
}

New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
$ExistingConfig = Read-ExistingBridgeConfig

if (-not $PlatformUrl) {
    if ($env:HETERO_PLATFORM_URL) {
        $PlatformUrl = $env:HETERO_PLATFORM_URL
    }
    elseif ($ExistingConfig -and $ExistingConfig.platform_http_url) {
        $PlatformUrl = [string]$ExistingConfig.platform_http_url
    }
}
if (-not $PlatformUrl) {
    throw "Platform URL is required on first run. Example: .\deploy\windows\run-device-bridge.ps1 http://linux-host:4032"
}
$PlatformUrl = $PlatformUrl.TrimEnd("/")
$ResolvedHdc = Resolve-HdcExecutable $ExistingConfig

if (-not (Test-Path $Python -PathType Leaf)) {
    $Launcher = Resolve-PythonLauncher
    Write-Host "Preparing the user-local Device Bridge runtime..."
    Write-Host "& `"$Launcher`" -m venv `"$VenvRoot`""
    & $Launcher -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Python -PathType Leaf)) {
        throw "Failed to create the Device Bridge Python environment."
    }
}

$RequirementsHash = (Get-FileHash -Algorithm SHA256 -Path $Requirements).Hash.ToLowerInvariant()
$InstalledRequirementsHash = ""
if (Test-Path $RequirementsStamp -PathType Leaf) {
    $InstalledRequirementsHash = (Get-Content -Raw -Path $RequirementsStamp).Trim().ToLowerInvariant()
}
if ($InstalledRequirementsHash -ne $RequirementsHash) {
    Write-Host "Installing Device Bridge dependencies..."
    Write-Host "& `"$Python`" -m pip install --disable-pip-version-check -r `"$Requirements`""
    & $Python -m pip install --disable-pip-version-check -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Device Bridge dependencies."
    }
    Set-Content -Path $RequirementsStamp -Value $RequirementsHash -Encoding Ascii
}

if (-not $BridgeName) {
    $BridgeName = "windows-device-$([guid]::NewGuid().ToString('D'))"
}

Write-Host "Registering or resuming the Windows Device Bridge..."
Write-Host "  Platform: $PlatformUrl"
Write-Host "  HDC:      $ResolvedHdc"
$PairArgs = @(
    $BridgeProgram,
    "pair",
    "--platform-url", $PlatformUrl,
    "--hdc", $ResolvedHdc,
    "--name", $BridgeName,
    "--config", $Config
)
& $Python @PairArgs
if ($LASTEXITCODE -ne 0) {
    throw "Device Bridge registration failed. Check that the Windows PC can open $PlatformUrl."
}

$DoctorArgs = @($BridgeProgram, "doctor", "--config", $Config)
& $Python @DoctorArgs
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Startup diagnosis needs attention. The Bridge will stay alive, keep discovering devices, and retry ordinary HTTP polling."
}

Write-Host "Device Bridge is running with resilient HTTP polling. Connect or reconnect a phone at any time; press Ctrl+C to stop."
Write-Host "This window hosts a foreground daemon. Enter is intentionally ignored; keep it open and use Ctrl+C to stop."
& $Python $BridgeProgram run --config $Config
exit $LASTEXITCODE
