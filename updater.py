"""Updates: read a manifest you publish, download the new build, swap it in.

Nothing here is a push. The app pulls a small JSON manifest from a place only you
can write to (a GitHub release by default), compares versions, and offers the user
an Install button. The download must match the SHA-256 in the manifest or it is
thrown away, and the swap happens through a tiny script that waits for this process
to exit first, because Windows will not let a running .exe overwrite itself.

Publishing side: tools/publish.py builds the exe, writes latest.json and uploads
both to a GitHub release.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

APP_REPO = "Anirbandasdev/kling-studio"
DEFAULT_SOURCE = f"https://github.com/{APP_REPO}/releases/latest/download/latest.json"
RELEASES_PAGE = f"https://github.com/{APP_REPO}/releases/latest"

MANIFEST_LIMIT = 64 * 1024        # a manifest is a few hundred bytes; anything huge is wrong
MAX_UPDATE_BYTES = 400 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
PLATFORM_KEY = "windows" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "linux"


class UpdateError(Exception):
    """something is wrong with the manifest or the download; the message is shown to the user"""


# ------------------------------------------------------------------ versions

def parse_version(text):
    if not VERSION_RE.match(str(text or "").strip()):
        raise UpdateError(f"“{text}” is not a version number like 3.1.0.")
    return tuple(int(p) for p in str(text).strip().split("."))


def is_newer(candidate, current):
    try:
        a, b = parse_version(candidate), parse_version(current)
    except UpdateError:
        return False
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


def can_self_install():
    """a packaged Windows build can replace itself; a source checkout cannot"""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def current_exe():
    return Path(sys.executable).resolve()


# ------------------------------------------------------------------ the manifest

def scheme_of(text):
    r"""urlparse reads "C:\dir" as scheme "c", so a one-letter scheme means a drive."""
    s = urlparse(str(text or "")).scheme.lower()
    return s if len(s) > 1 else ""


def read_source(source):
    """Fetch the manifest from an https URL. A plain path is for offline testing only."""
    source = str(source or "").strip()
    if not source:
        raise UpdateError("No update source is set.")
    kind = scheme_of(source)
    if kind in ("http", "https"):
        if kind != "https":
            raise UpdateError("The update source must be an https address.")
        # GitHub's "latest/download" alias sits behind a CDN that can serve a
        # minutes-old manifest right after a release: ask it not to
        req = Request(source, headers={"User-Agent": "kling-studio-updater",
                                       "Accept": "application/json",
                                       "Cache-Control": "no-cache", "Pragma": "no-cache"})
        try:
            with urlopen(req, timeout=20) as r:
                raw = r.read(MANIFEST_LIMIT + 1)
        except Exception as e:                                   # noqa: BLE001 - shown to the user
            raise UpdateError(f"Couldn't reach the update server: {e}") from None
    elif kind in ("", "file"):
        path = Path(source[7:] if kind == "file" else source)
        try:
            raw = path.read_bytes()[:MANIFEST_LIMIT + 1]
        except OSError as e:
            raise UpdateError(f"Couldn't read {path}: {e.strerror or e}") from None
    else:
        raise UpdateError("The update source must be an https address or a folder path.")
    if len(raw) > MANIFEST_LIMIT:
        raise UpdateError("That update source returned something far too big to be a manifest.")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("The update source did not return a manifest (expected JSON).") from None
    if not isinstance(data, dict):
        raise UpdateError("The manifest must be a JSON object.")
    return data


def validate(manifest, current_version, source=""):
    """Normalise a manifest, or explain why it can't be trusted.

    Returns {version, notes, url, sha256, size, page, newer}. A manifest that is
    merely older or the same is returned with newer=False, not as an error.
    """
    version = str(manifest.get("version") or "").strip()
    parse_version(version)
    notes = manifest.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    notes = [str(n).strip() for n in notes if str(n).strip()][:8]
    page = str(manifest.get("page") or "").strip()
    if page and not page.startswith("https://"):
        raise UpdateError("The manifest's page link must start with https://.")

    out = {"version": version, "notes": notes, "page": page or "",
           "url": "", "sha256": "", "size": 0,
           "newer": is_newer(version, current_version)}

    part = manifest.get(PLATFORM_KEY)
    if not isinstance(part, dict):
        part = manifest if "url" in manifest else {}
    url = str(part.get("url") or "").strip()
    if url:
        if scheme_of(url) == "https":
            pass
        elif scheme_of(url) in ("", "file") and scheme_of(source) not in ("http", "https"):
            pass                                  # a folder or share: the file sits next to the manifest
        else:
            raise UpdateError("The download link in the manifest must be an https address.")
        sha = str(part.get("sha256") or "").strip().lower()
        if not SHA_RE.match(sha):
            raise UpdateError("The manifest is missing a valid sha256 for the download.")
        size = int(part.get("size") or 0)
        if size < 0 or size > MAX_UPDATE_BYTES:
            raise UpdateError("The manifest's size looks wrong.")
        out.update(url=url, sha256=sha, size=size)
    return out


# ------------------------------------------------------------------ download

def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def staging_dir():
    d = Path(tempfile.gettempdir()) / "KlingStudioUpdate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_update(info, progress=None, download=None):
    """Fetch the build into a temp folder and check it against the manifest's hash."""
    if not info.get("url"):
        raise UpdateError("This update has no download for this computer.")
    dest = staging_dir() / f"Kling Studio {info['version']}.exe"
    if dest.exists():
        try:
            dest.unlink()
        except OSError:
            dest = staging_dir() / f"Kling Studio {info['version']}.{int(time.time())}.exe"
    url = info["url"]
    if scheme_of(url) in ("", "file"):
        src = Path(url[7:] if url.startswith("file:") else url)
        dest.write_bytes(src.read_bytes())
        got = dest.stat().st_size
    else:
        got = (download or _http_download)(url, dest, progress)
    if info["size"] and got != info["size"]:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"The download is {got} bytes, but the manifest says {info['size']}.")
    if sha256_of(dest) != info["sha256"]:
        dest.unlink(missing_ok=True)
        raise UpdateError("The download didn't match the manifest's checksum, so it was deleted.")
    return dest


def _http_download(url, dest, progress=None):
    import pipeline as pl        # reuse the stall-retry downloader used for frames and videos
    return pl.download_file(url, dest, progress)


# ------------------------------------------------------------------ applying it

SWAP_SCRIPT = """@echo off
setlocal enableextensions
rem Waits for Kling Studio to let go of its .exe, swaps the new build in, starts it
rem again, then deletes itself. Everything it says goes into the log beside it.
set "LOG={log}"
echo [%date% %time%] waiting for process {pid} to exit> "%LOG%"
set /a tries=0
:wait
set /a tries+=1
if %tries% gtr 150 goto late
tasklist /fi "PID eq {pid}" /nh 2>nul | findstr /i "{pid}" >nul || goto ready
ping -n 2 127.0.0.1 >nul
goto wait
:late
echo [%time%] it is still running after five minutes; trying anyway>> "%LOG%"
:ready
ping -n 2 127.0.0.1 >nul
if exist "{backup}" del /q "{backup}"
rem the file can stay locked for a moment after the process goes (antivirus, indexer)
set /a moves=0
:move
set /a moves+=1
move /y "{target}" "{backup}" >nul 2>&1
if not exist "{target}" goto swap
if %moves% gtr 20 goto locked
ping -n 2 127.0.0.1 >nul
goto move
:swap
move /y "{new}" "{target}" >nul 2>&1
if not exist "{target}" goto restore
echo [%time%] swapped in the new build>> "%LOG%"
goto restart
:restore
echo [%time%] the new build would not move in; putting the old one back>> "%LOG%"
move /y "{backup}" "{target}" >nul 2>&1
goto restart
:locked
echo [%time%] the old exe is still locked, so nothing was changed>> "%LOG%"
:restart
echo [%time%] starting "{target}">> "%LOG%"
rem This script inherited PyInstaller's private _PYI_* variables from the app that
rem wrote it, and would hand them to the new build, which then takes itself for a
rem child process of this script and checks that its parent is the same program.
rem By then the script has exited, so the check fails and the new build dies with
rem "Security validation failure" instead of starting. This tells the bootloader to
rem start clean; it is PyInstaller's own way of launching a fresh copy of an app.
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" "{target}"
del /q "%~f0"
"""


def swap_log():
    return staging_dir() / "update.log"


def write_swap_script(new_file: Path, target: Path, pid: int):
    script = staging_dir() / f"swap-{pid}-{int(time.time())}.cmd"
    script.write_text(SWAP_SCRIPT.format(pid=pid, new=new_file, target=target, log=swap_log(),
                                         backup=target.with_suffix(".bak.exe")), encoding="utf-8")
    return script


# CREATE_NO_WINDOW gives the script its own hidden console, which tasklist, findstr
# and ping all need. DETACHED_PROCESS must not be added: with no console at all the
# very first piped command hangs and the update never happens.
SWAP_FLAGS = 0x08000000 | 0x00000200            # NO_WINDOW | NEW_PROCESS_GROUP


def apply_update(new_file: Path, target: Path = None, pid: int = None):
    """Hand the swap to a script of its own and return; the caller then exits.

    The script outlives this process: Windows does not kill children when a parent
    goes, and its own process group keeps it clear of anything aimed at ours.
    """
    if not can_self_install():
        raise UpdateError("This copy runs from Python, so it can't replace itself. "
                          "Download the new build and copy it over the old one.")
    target = Path(target or current_exe())
    script = write_swap_script(Path(new_file), target, pid or os.getpid())
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=SWAP_FLAGS, close_fds=True,
                     cwd=str(staging_dir()))
    return script


def rollback_path(target: Path = None):
    return Path(target or current_exe()).with_suffix(".bak.exe")


def check(source, current_version):
    """One-shot: read the manifest and say whether it offers something newer."""
    return validate(read_source(source), current_version, source)
