const esbuild = require('esbuild');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
require('./prepare-capture.cjs').prepareCapture();
require('./prepare-analysis-worker.cjs').prepareAnalysisWorker();
fs.mkdirSync(path.join(root, 'dist'), { recursive: true });
esbuild.buildSync({
  entryPoints: [path.join(root, 'renderer/renderer.js')],
  bundle: true, outfile: path.join(root, 'dist/renderer.js'),
  platform: 'browser', target: 'chrome140', sourcemap: true,
  assetNames: 'assets/[name]-[hash]',
  loader: { '.ttf': 'file' },
  // Shared React pages live above desktop/. Without aliases Node resolution
  // would bundle one React from the repository root and another from
  // desktop/node_modules, which breaks hooks at runtime.
  alias: {
    react: path.join(root, 'node_modules/react'),
    'react-dom': path.join(root, 'node_modules/react-dom'),
    'lucide-react': path.join(root, 'node_modules/lucide-react'),
    mermaid: path.join(root, 'node_modules/mermaid'),
  },
});
esbuild.buildSync({
  entryPoints: [path.join(root, 'node_modules/monaco-editor/esm/vs/editor/editor.worker.js')],
  bundle: true,
  outfile: path.join(root, 'dist/monaco-editor-worker.js'),
  platform: 'browser',
  target: 'chrome140',
  format: 'iife',
  legalComments: 'none',
  loader: { '.css': 'empty' },
});
fs.copyFileSync(path.join(root, 'renderer/index.html'), path.join(root, 'dist/index.html'));
fs.copyFileSync(path.join(root, 'renderer/styles.css'), path.join(root, 'dist/styles.css'));
fs.copyFileSync(path.join(root, 'renderer/workbench-theme.css'), path.join(root, 'dist/workbench-theme.css'));
fs.copyFileSync(path.join(root, 'renderer/theme-variants.css'), path.join(root, 'dist/theme-variants.css'));
fs.copyFileSync(path.join(root, 'renderer/theme-bootstrap.js'), path.join(root, 'dist/theme-bootstrap.js'));
const webStyles = fs.readFileSync(path.join(root, '../app/globals.css'), 'utf8').replace(/^:root\s*\{/m, ':scope {');
fs.writeFileSync(path.join(root, 'dist/migrated-web.css'), `@scope (.migrated-web-page) {\n${webStyles}\n}\n`);
fs.copyFileSync(path.join(root, 'node_modules/@xterm/xterm/css/xterm.css'), path.join(root, 'dist/xterm.css'));
const monacoLicenseSource = path.join(root, 'node_modules/monaco-editor');
const monacoLicenseTarget = path.join(root, 'dist/licenses/monaco-editor');
fs.mkdirSync(monacoLicenseTarget, { recursive: true });
for (const filename of ['LICENSE', 'ThirdPartyNotices.txt']) {
  fs.copyFileSync(path.join(monacoLicenseSource, filename), path.join(monacoLicenseTarget, filename));
}
