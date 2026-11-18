// Copies the daemon and installer into extension/bundled so a packaged
// extension can set up its own speech engine. Run automatically by
// `vsce package` / `vsce publish` (the vscode:prepublish script).
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const out = path.resolve(__dirname, '..', 'bundled');

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });
fs.cpSync(path.join(root, 'daemon'), path.join(out, 'daemon'), {
  recursive: true,
  filter: source => !/__pycache__|\.egg-info|\.pyc$/.test(source),
});
fs.copyFileSync(path.join(root, 'install.sh'), path.join(out, 'install.sh'));
fs.chmodSync(path.join(out, 'install.sh'), 0o755);
for (const name of ['LICENSE', 'CHANGELOG.md']) {
  fs.copyFileSync(path.join(root, name), path.join(__dirname, '..', name === 'LICENSE' ? 'LICENSE.txt' : name));
}
console.log(`bundled daemon and installer into ${path.relative(process.cwd(), out)}`);
