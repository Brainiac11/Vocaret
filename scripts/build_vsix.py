#!/usr/bin/env python3
"""Package the extension as a .vsix using only the Python standard library.

Produces the same layout as `vsce package` (which CI and Marketplace
publishing use): the extension under extension/, with the daemon and
installer bundled in extension/bundled/ so the extension can set up its own
speech engine. This script exists so install.sh needs no Node tooling.

Usage: build_vsix.py [output.vsix]
"""

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
EXTENSION_FILES = ["package.json", "extension.js", "README.md", "icon.png"]
SKIP = ("__pycache__", ".egg-info", ".pyc")

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension=".json" ContentType="application/json"/>
<Default Extension=".vsixmanifest" ContentType="text/xml"/>
<Default Extension=".js" ContentType="application/javascript"/>
<Default Extension=".md" ContentType="text/markdown"/>
<Default Extension=".txt" ContentType="text/plain"/>
<Default Extension=".png" ContentType="image/png"/>
<Default Extension=".py" ContentType="text/x-python"/>
<Default Extension=".toml" ContentType="application/toml"/>
<Default Extension=".sh" ContentType="application/x-sh"/>
</Types>
"""


def manifest(pkg: dict) -> str:
    kinds = ",".join(pkg.get("extensionKind", ["workspace"]))
    repo = pkg.get("repository", {}).get("url", "").removesuffix(".git")
    links = ""
    if repo:
        links = f"""
      <Property Id="Microsoft.VisualStudio.Services.Links.Source" Value={quoteattr(repo + ".git")}/>
      <Property Id="Microsoft.VisualStudio.Services.Links.Getstarted" Value={quoteattr(repo + ".git")}/>
      <Property Id="Microsoft.VisualStudio.Services.Links.GitHub" Value={quoteattr(repo + ".git")}/>
      <Property Id="Microsoft.VisualStudio.Services.Links.Support" Value={quoteattr(repo + "/issues")}/>
      <Property Id="Microsoft.VisualStudio.Services.Links.Learn" Value={quoteattr(repo + "#readme")}/>"""
    banner = pkg.get("galleryBanner", {})
    if banner:
        links += f"""
      <Property Id="Microsoft.VisualStudio.Services.Branding.Color" Value={quoteattr(banner.get("color", ""))}/>
      <Property Id="Microsoft.VisualStudio.Services.Branding.Theme" Value={quoteattr(banner.get("theme", "dark"))}/>"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id={quoteattr(pkg["name"])} Version={quoteattr(pkg["version"])} Publisher={quoteattr(pkg["publisher"])}/>
    <DisplayName>{escape(pkg["displayName"])}</DisplayName>
    <Description xml:space="preserve">{escape(pkg["description"])}</Description>
    <Tags>{escape(",".join(pkg.get("keywords", [])))}</Tags>
    <Categories>{escape(",".join(pkg.get("categories", ["Other"])))}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value={quoteattr(pkg["engines"]["vscode"])}/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value={quoteattr(kinds)}/>
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value=""/>{links}
    </Properties>
    <License>extension/LICENSE.txt</License>
    <Icon>extension/icon.png</Icon>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Changelog" Path="extension/CHANGELOG.md" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="extension/icon.png" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def main() -> None:
    pkg = json.loads((EXT / "package.json").read_text())
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / f"vocaret-{pkg['version']}.vsix"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("extension.vsixmanifest", manifest(pkg))
        for name in EXTENSION_FILES:
            zf.write(EXT / name, f"extension/{name}")
        zf.write(ROOT / "LICENSE", "extension/LICENSE.txt")
        zf.write(ROOT / "CHANGELOG.md", "extension/CHANGELOG.md")
        # The daemon and installer travel inside the extension.
        for path in sorted((ROOT / "daemon").rglob("*")):
            if path.is_file() and not any(part in str(path) for part in SKIP):
                zf.write(path, f"extension/bundled/{path.relative_to(ROOT)}")
        info = zipfile.ZipInfo("extension/bundled/install.sh")
        info.external_attr = 0o755 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, (ROOT / "install.sh").read_bytes())
    print(out)


if __name__ == "__main__":
    main()
