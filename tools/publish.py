"""Ship an update: bump the version, build the exe, write latest.json, publish a release.

    publish.bat 3.1.0 --notes "Frames retry faster" "Tour covers the API key"

What it does, in order:
  1. checks the version is newer than the one in app.py
  2. writes it into app.py and installer.iss
  3. runs build_exe.bat  (skip with --no-build)
  4. copies dist\\Kling Studio.exe to dist\\release\\KlingStudio-<version>.exe
  5. writes dist\\release\\latest.json with the sha256, size and your notes
  6. creates the GitHub release with both files attached  (skip with --no-upload)

The app reads latest.json from the release tagged "latest" on GitHub, so step 6 is
what makes the update appear on your coworker's machine. Without the gh CLI the
files are left in dist\\release\\ with instructions for uploading them by hand.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import updater  # noqa: E402

ASSET = "KlingStudio-{version}.exe"


def current_version():
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    return re.search(r'^APP_VERSION = "([^"]+)"', text, re.M).group(1)


def bump(version):
    app = ROOT / "app.py"
    text = app.read_text(encoding="utf-8")
    app.write_text(re.sub(r'^APP_VERSION = "[^"]+"', f'APP_VERSION = "{version}"', text, count=1, flags=re.M),
                   encoding="utf-8")
    iss = ROOT / "installer.iss"
    if iss.exists():
        t = iss.read_text(encoding="utf-8")
        iss.write_text(re.sub(r'#define AppVersion "[^"]+"', f'#define AppVersion "{version}"', t, count=1),
                       encoding="utf-8")
    print(f"  version   {version} written into app.py and installer.iss")


def build():
    print("  building  dist\\Kling Studio.exe (PyInstaller)…")
    r = subprocess.run([str(ROOT / "build_exe.bat")], cwd=str(ROOT), shell=True)
    if r.returncode:
        sys.exit("build_exe.bat failed, nothing was published.")


def gh_available():
    return shutil.which("gh") is not None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="publish", description="Publish a Kling Studio update.")
    ap.add_argument("version", help="the new version, e.g. 3.1.0")
    ap.add_argument("--notes", nargs="*", default=[], help="one short line per change")
    ap.add_argument("--repo", default=updater.APP_REPO, help=f"GitHub repo (default {updater.APP_REPO})")
    ap.add_argument("--no-build", action="store_true", help="use the exe already in dist\\")
    ap.add_argument("--no-upload", action="store_true", help="write the files but don't touch GitHub")
    args = ap.parse_args(argv)

    version, now = args.version.strip(), current_version()
    updater.parse_version(version)
    if not updater.is_newer(version, now):
        sys.exit(f"{version} is not newer than the current {now}. Pick a higher version.")

    print(f"Publishing Kling Studio {version} (from {now})")
    bump(version)
    if not args.no_build:
        build()

    built = ROOT / "dist" / "Kling Studio.exe"
    if not built.is_file():
        sys.exit(f"{built} is missing. Run without --no-build.")

    out = ROOT / "dist" / "release"
    out.mkdir(parents=True, exist_ok=True)
    asset = out / ASSET.format(version=version)
    shutil.copy2(built, asset)
    digest, size = updater.sha256_of(asset), asset.stat().st_size
    tag = f"v{version}"
    manifest = {
        "version": version,
        "notes": [n.strip() for n in args.notes if n.strip()] or ["Small fixes and improvements."],
        "page": f"https://github.com/{args.repo}/releases/tag/{tag}",
        "windows": {
            "url": f"https://github.com/{args.repo}/releases/download/{tag}/{asset.name}",
            "sha256": digest,
            "size": size,
        },
    }
    (out / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"  exe       {asset.name}  ({size / 1048576:.1f} MB)")
    print(f"  sha256    {digest}")
    print(f"  manifest  {out / 'latest.json'}")

    if args.no_upload or not gh_available():
        if not gh_available():
            print("\n  gh (GitHub CLI) isn't installed, so nothing was uploaded. Either:")
            print("    winget install GitHub.cli   then   gh auth login")
            print(f"    and re-run:  publish.bat {version} --no-build")
            print("  or upload by hand:")
        print(f"\n  Create a release tagged {tag} on https://github.com/{args.repo}/releases/new")
        print(f"  and attach BOTH files from {out}:")
        print(f"    {asset.name}\n    latest.json")
        print("  Mark it as the latest release, and the app will find it.")
        return 0

    print(f"  uploading release {tag} to {args.repo}…")
    notes = "\n".join(f"- {n}" for n in manifest["notes"])
    cmd = ["gh", "release", "create", tag, str(asset), str(out / "latest.json"),
           "--repo", args.repo, "--title", f"Kling Studio {version}", "--notes", notes, "--latest"]
    if subprocess.run(cmd).returncode:
        print("\n  gh failed (is the repo created and are you logged in with `gh auth login`?).")
        print(f"  The files are ready in {out} if you'd rather upload them by hand.")
        return 1
    print(f"\nDone. Kling Studio {version} is live — the app offers it within a few hours,")
    print("or straight away with Settings → Updates → Check now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
