# Publishing

For maintainers. Nothing here is needed to use or develop Vocaret.

## Before the first release

1. **Publisher id.** `extension/package.json` has `"publisher": "vocaret"`.
   The VS Code Marketplace and Open VSX each require you to own that
   publisher/namespace. Create it, or change the field to one you own. The
   extension id (`publisher.name`) changes with it; `install.sh` reads it
   from the manifest.
2. **Name.** The Marketplace requires `name` and `displayName` to be unique.
   Search both marketplaces for the name first.
3. **Repository URL.** `repository`, `bugs` and `homepage` in
   `extension/package.json`, and `REPO_URL` in `daemon/vocaret/providers.py`,
   point at the GitHub repository. Update them if it moves.

## Cutting a release

1. Bump the version in **three** places (a test checks they agree):
   `daemon/pyproject.toml`, `daemon/vocaret/__init__.py`,
   `extension/package.json`. Add a `CHANGELOG.md` entry.
2. Run the tests in `CONTRIBUTING.md`, including the in-editor one.
3. Tag and push:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```

   The `Release` workflow packages the extension with `vsce` and attaches the
   `.vsix` to a GitHub release. People can install that file directly
   (Extensions > ... > Install from VSIX). The release notes are the
   version's `CHANGELOG.md` section plus install steps
   (`scripts/release_notes.sh`); the workflow stops if the tag doesn't match
   the version in `extension/package.json` or the changelog has no section
   for it.

## Marketplaces

Packaging runs `extension/scripts/bundle.js` first (the `vscode:prepublish`
script), which copies `daemon/` and `install.sh` into `extension/bundled/` so
the published extension can install its own speech engine.

By hand, from `extension/`:

```bash
npx @vscode/vsce package
```

```bash
npx @vscode/vsce publish
```

```bash
npx ovsx publish -p "$OVSX_PAT"
```

From CI, the `Release` workflow publishes when these repository settings exist:

- `VSCE_PAT` secret: VS Code Marketplace personal access token (scope
  Marketplace > Manage, all accessible organizations).
- `OVSX_PAT` secret: Open VSX access token. Create the namespace once with
  `npx ovsx create-namespace <publisher> -p <token>`.

Azure DevOps is retiring global personal access tokens on 1 December 2026.
After that, publish to the VS Code Marketplace from GitHub Actions with
trusted publishing (`vsce publish --oidc`) instead of `VSCE_PAT`; see
https://code.visualstudio.com/api/working-with-extensions/publishing-extension
for the current setup steps. The workflow's Marketplace step will need that
change; it has not been exercised yet.

## What gets published

`extension/.vscodeignore` keeps the package to `extension.js`,
`package.json`, the README, changelog, license, icon and `bundled/`. Check
with:

```bash
npx @vscode/vsce ls
```
