param(
    [Parameter(Position = 0)][string]$Package = "",
    [string]$HdcPath = "",
    [string]$DeviceSerial = "",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"

function Resolve-PythonLauncher {
    $Candidates = @()
    if ($env:LOCALAPPDATA) {
        $BridgePython = Join-Path $env:LOCALAPPDATA "HeterogeneousDeviceBridge\venv\Scripts\python.exe"
        if (Test-Path $BridgePython -PathType Leaf) {
            $Candidates += @{ Command = (Resolve-Path $BridgePython).Path; Prefix = @() }
        }
    }
    foreach ($Name in @("py", "python", "python3")) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source) {
            if ($Name -eq "py") {
                $Candidates += @{ Command = $Command.Source; Prefix = @("-3") }
            }
            else {
                $Candidates += @{ Command = $Command.Source; Prefix = @() }
            }
        }
    }
    foreach ($Candidate in $Candidates) {
        $ProbeArguments = @()
        $ProbeArguments += $Candidate.Prefix
        $ProbeArguments += @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)")
        & $Candidate.Command @ProbeArguments 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return $Candidate
        }
    }
    throw "Python 3.11 or newer was not found. Install Python 3.11+, or rerun the current Windows Device Bridge installer."
}

function Read-BridgeConfig {
    if (-not $env:LOCALAPPDATA) {
        return $null
    }
    $Path = Join-Path $env:LOCALAPPDATA "HeterogeneousDeviceBridge\bridge.json"
    if (-not (Test-Path $Path -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    }
    catch {
        throw "The Windows Device Bridge configuration cannot be read: $Path"
    }
}

function Resolve-HdcExecutable([object]$BridgeConfig) {
    $Candidates = @()
    if ($HdcPath) {
        $Candidates += $HdcPath
    }
    if ($BridgeConfig -and $BridgeConfig.hdc_path) {
        $Candidates += [string]$BridgeConfig.hdc_path
    }
    foreach ($Name in @("hdc.exe", "hdc")) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source) {
            $Candidates += $Command.Source
        }
    }
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path $Candidate -PathType Leaf)) {
            return (Resolve-Path $Candidate).Path
        }
    }
    throw "HDC was not found. Pass -HdcPath C:\path\to\hdc.exe or run the Windows Device Bridge once."
}

function Resolve-PackageDirectory([string]$InputPath) {
    $Resolved = (Resolve-Path $InputPath).Path
    if (Test-Path $Resolved -PathType Container) {
        $Base = $Resolved
        $Temporary = $null
    }
    elseif ((Test-Path $Resolved -PathType Leaf) -and $Resolved.EndsWith(".zip", [System.StringComparison]::OrdinalIgnoreCase)) {
        $Temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("hetero-local-capture-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $Temporary | Out-Null
        try {
            Expand-Archive -LiteralPath $Resolved -DestinationPath $Temporary
        }
        catch {
            Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
            throw
        }
        $Base = $Temporary
    }
    else {
        throw "-Package must name an extracted package directory or a .zip file."
    }

    if (Test-Path (Join-Path $Base "package.json") -PathType Leaf) {
        return @{ Path = $Base; Temporary = $Temporary }
    }
    $Matches = @(Get-ChildItem -LiteralPath $Base -Directory | Where-Object {
        Test-Path (Join-Path $_.FullName "package.json") -PathType Leaf
    })
    if ($Matches.Count -eq 1) {
        return @{ Path = $Matches[0].FullName; Temporary = $Temporary }
    }
    if ($Temporary) {
        Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw "package.json was not found at the package root. Extract the complete package and keep its directory structure."
}

if (-not $Package) {
    $Package = $PSScriptRoot
}

$BridgeConfig = Read-BridgeConfig
$ResolvedHdc = Resolve-HdcExecutable $BridgeConfig
$Python = Resolve-PythonLauncher
$ResolvedPackage = Resolve-PackageDirectory $Package
$Runner = Join-Path $ResolvedPackage.Path "runtime\tools\scenario_capture\run_local_package.py"
if (-not (Test-Path $Runner -PathType Leaf)) {
    # Developer convenience when invoking this script from the repository.
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $RepositoryRunner = Join-Path $RepositoryRoot "tools\scenario_capture\run_local_package.py"
    if ((Test-Path (Join-Path $ResolvedPackage.Path "package.json") -PathType Leaf) -and
        (Test-Path $RepositoryRunner -PathType Leaf)) {
        $Runner = $RepositoryRunner
    }
    else {
        if ($ResolvedPackage.Temporary) {
            Remove-Item -LiteralPath $ResolvedPackage.Temporary -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw "The package is incomplete: runtime\tools\scenario_capture\run_local_package.py is missing."
    }
}

if (-not $OutputRoot) {
    if ($env:USERPROFILE) {
        $OutputRoot = Join-Path $env:USERPROFILE "Documents\HeterogeneousCapture"
    }
    elseif ($env:LOCALAPPDATA) {
        $OutputRoot = Join-Path $env:LOCALAPPDATA "HeterogeneousDeviceBridge\manual-captures"
    }
    else {
        throw "OutputRoot is required because neither USERPROFILE nor LOCALAPPDATA is available."
    }
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$ResolvedOutput = (Resolve-Path $OutputRoot).Path

$Arguments = @()
$Arguments += $Python.Prefix
$Arguments += @(
    $Runner,
    "--package", $ResolvedPackage.Path,
    "--hdc", $ResolvedHdc,
    "--output-root", $ResolvedOutput
)
if ($DeviceSerial) {
    $Arguments += @("--device-serial", $DeviceSerial)
}

$PreviousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = "1"
try {
    Write-Host "Running the selected scenario directly through this Windows PC..."
    Write-Host "  Package: $($ResolvedPackage.Path)"
    Write-Host "  HDC:     $ResolvedHdc"
    Write-Host "  Output:  $ResolvedOutput"
    Write-Host "  Device:  explicit -DeviceSerial, package binding, or the unique local HDC target"
    Write-Host '  Multiple devices: rerun .\run-capture.ps1 -DeviceSerial "YOUR_DEVICE_SERIAL"'
    & $Python.Command @Arguments
    $RunnerExitCode = $LASTEXITCODE
    if ($RunnerExitCode -ne 0) {
        throw "Local scenario capture did not complete successfully (exit code $RunnerExitCode). The retained run and diagnostics remain under $ResolvedOutput."
    }
    Write-Host "Local capture completed. Register the generated .capture.zip in Trace management when ready."
}
finally {
    $env:PYTHONUTF8 = $PreviousPythonUtf8
    if ($ResolvedPackage.Temporary) {
        Remove-Item -LiteralPath $ResolvedPackage.Temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
