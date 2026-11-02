[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$BuildOnly,
    [switch]$Package
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$desktopDirectory = $PSScriptRoot
$repositoryDirectory = Split-Path -Parent $desktopDirectory
$locationChanged = $false
$originalElectronNodeMode = [Environment]::GetEnvironmentVariable('ELECTRON_RUN_AS_NODE', 'Process')
$originalDesktopPython = [Environment]::GetEnvironmentVariable('HETERO_DESKTOP_PYTHON', 'Process')

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments, [string]$Stage)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed (exit code $LASTEXITCODE). See the command output above."
    }
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This launcher is for Windows. Use desktop/README.md for other systems.' }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $nodeCommand -or -not $npmCommand) {
        throw 'Install Node.js 24 or newer (including npm), then open a new PowerShell window.'
    }
    $nodeVersion = & $nodeCommand.Source '--version'
    if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^v(\d+)\.' -or [int]$Matches[1] -lt 24) { throw 'Node.js 24 or newer is required.' }
    $nodePlatform = & $nodeCommand.Source -p '[process.platform, process.arch].join(String.fromCharCode(45))'
    if ($LASTEXITCODE -ne 0 -or $nodePlatform -ne 'win32-x64') {
        throw 'Use Windows x64 Node.js. The bundled Python and desktop release target Windows x64.'
    }

    $sourceElectron = Join-Path $desktopDirectory 'node_modules\electron\dist\electron.exe'
    $running = @(Get-Process -Name 'electron', 'Heterogeneous Workbench' -ErrorAction SilentlyContinue | Where-Object {
        try { $_.ProcessName -eq 'Heterogeneous Workbench' -or $_.Path -eq $sourceElectron } catch { $false }
    })
    if ($running.Count -gt 0) {
        throw 'Close Heterogeneous Workbench first, after any active capture finishes. No process was stopped by this script.'
    }

    if ($Update) {
        $gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
        if (-not $gitCommand) { throw 'Install Git for Windows to use -Update, then open a new PowerShell window.' }
        Write-Host 'Updating this checkout (git pull --ff-only)...'
        Invoke-Checked -Executable $gitCommand.Source -Arguments @('-C', $repositoryDirectory, 'pull', '--ff-only') -Stage 'Git update'
        # Re-read the updated launcher, so newly added preparation steps take
        # effect in this run rather than waiting until the next invocation.
        $nextArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath)
        if ($BuildOnly) { $nextArguments += '-BuildOnly' }
        if ($Package) { $nextArguments += '-Package' }
        $powerShellExecutable = (Get-Process -Id $PID).Path
        Invoke-Checked -Executable $powerShellExecutable -Arguments $nextArguments -Stage 'Updated desktop launcher'
        return
    }

    [Environment]::SetEnvironmentVariable('ELECTRON_RUN_AS_NODE', $null, 'Process')
    Push-Location $desktopDirectory
    $locationChanged = $true
    & $nodeCommand.Source 'build/source-launcher.cjs' '--check-deps'
    $dependencyResult = $LASTEXITCODE
    if ($dependencyResult -eq 10) {
        Write-Host 'Installing locked desktop dependencies (first run or dependency/runtime change)...'
        Invoke-Checked -Executable $npmCommand.Source -Arguments @('ci', '--include=dev') -Stage 'Dependency installation'
        Invoke-Checked -Executable $nodeCommand.Source -Arguments @('build/source-launcher.cjs', '--record-packages') -Stage 'Package verification'
    } elseif ($dependencyResult -ne 0) {
        throw "Dependency inspection failed (exit code $dependencyResult)."
    } else {
        Write-Host 'Reusing installed desktop dependencies.'
    }

    # Running through npm imports effective .npmrc proxy/CA/Electron mirror
    # settings into the lifecycle environment. The Node step sanitizes that
    # environment before starting the binary downloader.
    Invoke-Checked -Executable $npmCommand.Source -Arguments @('run', '--silent', 'prepare:electron') -Stage 'Electron runtime preparation'
    Invoke-Checked -Executable $nodeCommand.Source -Arguments @('build/prepare-native.cjs') -Stage 'Native runtime preparation'
    Invoke-Checked -Executable $nodeCommand.Source -Arguments @('build/source-launcher.cjs', '--record-deps') -Stage 'Complete dependency verification'
    $useSystemPython = $false
    if ($Package) {
        # A distributed EXE must remain self-contained and must be verified
        # against the pinned runtime, regardless of the build host.
        [Environment]::SetEnvironmentVariable('HETERO_DESKTOP_PYTHON', $null, 'Process')
    } elseif ($null -ne $originalDesktopPython) {
        $pythonIdentityJson = & $nodeCommand.Source 'build/system-python.cjs' $originalDesktopPython
        if ($LASTEXITCODE -ne 0) {
            throw 'System Python validation failed. HETERO_DESKTOP_PYTHON was explicit, so the launcher did not fall back to a download.'
        }
        $pythonIdentity = $pythonIdentityJson | ConvertFrom-Json
        if ([string]::IsNullOrWhiteSpace($pythonIdentity.executable)) { throw 'System Python validation returned no executable path.' }
        [Environment]::SetEnvironmentVariable('HETERO_DESKTOP_PYTHON', [string]$pythonIdentity.executable, 'Process')
        $pythonVersion = @($pythonIdentity.version) -join '.'
        Write-Host "Using validated system Python $pythonVersion at $($pythonIdentity.executable)."
        Write-Host 'Skipping embedded Python download for this source run.'
        $useSystemPython = $true
    }
    if (-not $useSystemPython) {
        # Python preparation uses the same allowlisted npm proxy/CA bridge as
        # Electron and also supports a separately verified offline ZIP.
        Invoke-Checked -Executable $npmCommand.Source -Arguments @('run', '--silent', 'prepare:python') -Stage 'Python runtime preparation'
    }
    Invoke-Checked -Executable $npmCommand.Source -Arguments @('run', 'build') -Stage 'Desktop and capture runtime build'
    Invoke-Checked -Executable $nodeCommand.Source -Arguments @('build/check-capture.cjs') -Stage 'Capture runtime verification'

    if ($Package) {
        Invoke-Checked -Executable $nodeCommand.Source -Arguments @('node_modules/electron-builder/cli.js', '--win', 'portable', 'nsis', '--x64') -Stage 'Windows packaging'
        Write-Host "Windows packages are ready in: $(Join-Path $desktopDirectory 'release')"
    } elseif ($BuildOnly) {
        Write-Host 'Desktop build completed. Start with run-desktop.ps1 when ready.'
    } else {
        Write-Host 'Starting Heterogeneous Workbench with your existing AppData settings.'
        Write-Host 'Keep this PowerShell window open. Close the app before closing this window; active captures may otherwise stop.'
        Invoke-Checked -Executable $nodeCommand.Source -Arguments @('build/source-launcher.cjs', '--launch') -Stage 'Desktop execution'
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    if ($locationChanged) { Pop-Location }
    [Environment]::SetEnvironmentVariable('ELECTRON_RUN_AS_NODE', $originalElectronNodeMode, 'Process')
    [Environment]::SetEnvironmentVariable('HETERO_DESKTOP_PYTHON', $originalDesktopPython, 'Process')
}
