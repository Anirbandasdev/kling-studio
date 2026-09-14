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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import updater  # noqa: E402

ASSET = "KlingStudio-{version}.exe"

INSTALL_HELP = """

## Installing

**Windows** — download `KlingStudio-Setup-<version>.exe` below and run it.
SmartScreen shows a blue *"Windows protected your PC"* box for anything that is
not code-signed: click **More info**, then **Run anyway**. It installs into your
user folder, so there is no administrator prompt, and later updates install
themselves from inside the app.

**macOS** — download the `.dmg`, open it, drag Kling Studio to Applications.
The first launch needs **right-click the app, then Open** (macOS blocks apps it
cannot check, once). "Read me first.txt" inside the disk image says the same.

Both need a kie.ai API key, which the app asks for on first launch. kie.ai's own
prices are built in: a frame costs 8, 12 or 18 credits at 1K, 2K or 4K, and a
5-second Pro video 90 (135 with sound). Every run shows the total first.
"""


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
    # The handbook names the installer files and carries a version badge; left alone it
    # goes stale one release at a time until it is telling the reader to look for a
    # download that no longer exists. Match only where a version can appear — a bare
    # \d+.\d+.\d+ would rewrite the SVG path coordinates all over that document.
    touched = []
    for doc in (ROOT / "docs").glob("handbook*.html"):
        text = doc.read_text(encoding="utf-8")
        fixed = text
        for pattern in (r"(?<=version )\d+\.\d+\.\d+",
                        r"(?<=KlingStudio-Setup-)\d+\.\d+\.\d+(?=\.exe)",
                        r"(?<=KlingStudio-)\d+\.\d+\.\d+(?=-mac\.)",
                        r"(?<=Kling Studio )\d+\.\d+\.\d+"):
            fixed = re.sub(pattern, version, fixed)
        if fixed != text:
            doc.write_text(fixed, encoding="utf-8")
            touched.append(doc.relative_to(ROOT).as_posix())
    print(f"  version   {version} written into app.py and installer.iss"
          + (f" and {', '.join(Path(t).name for t in touched)}" if touched else ""))
    return touched


def build():
    print("  building  dist\\Kling Studio.exe (PyInstaller)…")
    r = subprocess.run([str(ROOT / "build_exe.bat")], cwd=str(ROOT), shell=True)
    if r.returncode:
        sys.exit("build_exe.bat failed, nothing was published.")


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def commit_the_bump(version, docs=()):
    """Commit and push just the version bump, so the tag lands on the built code.

    Without this the release tag points at whatever was already on the remote, and
    the installers CI builds from that tag carry the previous version number.

    Every file bump() rewrote goes in, the handbooks included — staging only app.py
    and installer.iss leaves the docs' version strings dangling in the working tree
    after each release, which is how they quietly drift out of step.
    """
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        print("  git       not a repo, so the tag will point at whatever is on the remote")
        return None
    files = ["app.py", "installer.iss", *docs]
    if git("status", "--porcelain", *files):
        git("add", *files)
        git("commit", "-m", f"Release {version}")
        print(f"  git       committed the bump to {version}")
    else:
        print("  git       version files were already committed")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if git("remote"):
        git("push", "origin", branch)
        print(f"  git       pushed {branch}")
    return git("rev-parse", "HEAD")


def gh_exe():
    """gh on PATH, or where winget puts it when this shell's PATH is still stale"""
    found = shutil.which("gh")
    if found:
        return found
    guesses = [Path(r"C:\Program Files\GitHub CLI\gh.exe"),
               Path(r"C:\Program Files (x86)\GitHub CLI\gh.exe")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        guesses.append(Path(local) / "GitHubCLI" / "bin" / "gh.exe")
    return next((str(g) for g in guesses if g.is_file()), None)


def clean_notes(raw):
    """Tidy the release notes, refusing a set that was clearly split on spaces.

    Quoting is lost between shells often enough that --notes arrives as one word per
    note. 3.9.1 shipped that way, and the app's update dialog — which is where anyone
    actually reads these — showed a checklist with "the" on a line of its own. Two or
    three deliberate one-word notes ("Faster", "Bug fixes") are still fine.
    """
    notes = [str(n).strip() for n in raw if str(n).strip()]
    loose = [n for n in notes if " " not in n]
    if len(notes) > 2 and len(loose) > len(notes) / 2:
        raise SystemExit(f"--notes looks like it was split on spaces: {notes[:6]}…\n"
                         "Each note should be a whole sentence. Quote them, and remember\n"
                         "that a shell inside another shell needs them to survive both.")
    return notes


def main(argv=None):
    ap = argparse.ArgumentParser(prog="publish", description="Publish a Kling Studio update.")
    ap.add_argument("version", help="the new version, e.g. 3.1.0")
    ap.add_argument("--notes", nargs="*", default=[], help="one short line per change")
    ap.add_argument("--repo", default=updater.APP_REPO, help=f"GitHub repo (default {updater.APP_REPO})")
    ap.add_argument("--no-build", action="store_true", help="use the exe already in dist\\")
    ap.add_argument("--no-upload", action="store_true", help="write the files but don't touch GitHub")
    args = ap.parse_args(argv)

    notes = clean_notes(args.notes)
    version, now = args.version.strip(), current_version()
    updater.parse_version(version)
    if not updater.is_newer(version, now):
        sys.exit(f"{version} is not newer than the current {now}. Pick a higher version.")

    print(f"Publishing Kling Studio {version} (from {now})")
    docs = bump(version)
    if not args.no_build:
        build()
    commit = commit_the_bump(version, docs)

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
        "notes": notes or ["Small fixes and improvements."],
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

    gh = gh_exe()
    if args.no_upload or not gh:
        if not gh:
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
    notes = "\n".join(f"- {n}" for n in manifest["notes"]) + INSTALL_HELP
    cmd = [gh, "release", "create", tag, str(asset), str(out / "latest.json"),
           "--repo", args.repo, "--title", f"Kling Studio {version}", "--notes", notes, "--latest"]
    if commit:
        cmd += ["--target", commit]      # tag exactly the code that was built
    if subprocess.run(cmd).returncode:
        print("\n  gh failed (is the repo created and are you logged in with `gh auth login`?).")
        print(f"  The files are ready in {out} if you'd rather upload them by hand.")
        return 1
    print(f"\nDone. Kling Studio {version} is live — the app offers it within a few hours,")
    print("or straight away with Settings → Updates → Check now.")
    print("GitHub Actions is now building the installers for both platforms and will")
    print(f"attach them to  https://github.com/{args.repo}/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
