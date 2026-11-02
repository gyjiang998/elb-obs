'use strict';
const fs = require('node:fs');
const path = require('node:path');

function captureEnvironment({resourcesPath, appPath, platform = process.platform} = {}) {
  const packagedRoot = path.join(resourcesPath || '', 'capture-runtime');
  const developmentRoot = path.join(appPath || path.dirname(__dirname), 'build/resources/capture-runtime');
  const runtimeRoot = fs.existsSync(path.join(packagedRoot, 'tools/scenario_capture/desktop_entry.py')) ? packagedRoot : developmentRoot;
  const resourceRoot = runtimeRoot === packagedRoot ? resourcesPath : path.dirname(developmentRoot);
  const bundledPython = path.join(resourceRoot, platform === 'win32' ? 'python-win-x64/python.exe' : 'python-mac-arm64/bin/python3');
  // A Windows release is self-contained and must not inherit a workstation
  // override. Source runs and non-Windows development can select a local
  // Python 3.11-3.15 installation explicitly.
  const packagedWindows = runtimeRoot === packagedRoot && platform === 'win32';
  const configuredPython = packagedWindows ? '' : process.env.HETERO_DESKTOP_PYTHON?.trim();
  const bundled = fs.existsSync(bundledPython);
  const python = packagedWindows ? bundledPython
    : (configuredPython || (bundled ? bundledPython : (platform === 'win32' ? bundledPython : 'python3')));
  // A workstation Python runs in isolated, no-site mode so user packages,
  // PYTHONPATH and sitecustomize cannot change the capture contract. The
  // embedded Windows runtime is already isolated by its checked _pth file.
  const external = !packagedWindows && (Boolean(configuredPython) || !bundled);
  return {runtimeRoot, pythonCommand: external ? [python, '-B', '-I', '-S', '-X', 'utf8'] : [python],
    launchOptions: {env: {...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1'}}};
}
module.exports = {captureEnvironment};
