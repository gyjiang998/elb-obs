// Copy the same capture implementation used by the Web platform into the app.
// No server, bridge, pip install or source checkout is required at runtime.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const output = path.join(__dirname, 'resources/capture-runtime');
const backendFiles = ['__init__.py', 'core.py', 'platform_paths.py', 'pmu_sampler.py',
  'scenario_capture_local_package.py', 'local_capture_package.py'];

function prepareCapture() {
  fs.rmSync(output, {recursive: true, force: true});
  fs.mkdirSync(path.join(output, 'backend'), {recursive: true});
  for (const file of backendFiles) fs.copyFileSync(path.join(root, 'backend', file), path.join(output, 'backend', file));
  fs.mkdirSync(path.join(output, 'tools/scenario_capture'), {recursive: true});
  fs.writeFileSync(path.join(output, 'tools/__init__.py'), '');
  fs.cpSync(path.join(root, 'tools/scenario_capture'), path.join(output, 'tools/scenario_capture'), {
    recursive: true,
    filter(source) {
      const name = path.basename(source);
      return !name.startsWith('test_') && !['__pycache__', '.DS_Store'].includes(name) && !name.endsWith('.pyc');
    },
  });
  fs.mkdirSync(path.join(output, 'deploy/windows'), {recursive: true});
  fs.copyFileSync(path.join(root, 'deploy/windows/run-local-scenario-capture.ps1'), path.join(output, 'deploy/windows/run-local-scenario-capture.ps1'));
  const version = JSON.parse(fs.readFileSync(path.join(root, 'desktop/package.json'), 'utf8')).version;
  fs.writeFileSync(path.join(output, '.platform-build.json'), JSON.stringify({version, app_version: version, distribution: 'desktop'}));
  const files = [];
  function walk(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(filename);
      else files.push({path: path.relative(output, filename).split(path.sep).join('/'), sha256: crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')});
    }
  }
  walk(output);
  fs.writeFileSync(path.join(output, 'desktop-runtime-manifest.json'), JSON.stringify({version, files}, null, 2));
  return output;
}
if (require.main === module) console.log(prepareCapture());
module.exports = {prepareCapture};
