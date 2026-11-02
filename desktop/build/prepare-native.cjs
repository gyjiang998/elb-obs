const fs = require('node:fs');
const path = require('node:path');
// node-pty 1.1.0 ships N-API binaries for Windows/macOS. Its npm tarball
// lacks the executable bit on the macOS helper; fix it in both dev and release.
for (const arch of ['arm64', 'x64']) {
  const helper = path.join(__dirname, '..', 'node_modules/node-pty/prebuilds', `darwin-${arch}`, 'spawn-helper');
  if (process.platform !== 'win32' && fs.existsSync(helper)) fs.chmodSync(helper, 0o755);
}
