"""
Kling Studio v3: a small local web server plus an app window.

The UI is ui/index.html, shown in Microsoft Edge (or Chrome) in --app mode so it
looks like a normal desktop window. All pipeline work lives in pipeline.py and the
master prompt is read by paste.py. The server only listens on 127.0.0.1, and every
API call needs the per-launch token baked into the page.
"""

import argparse
import base64
import binascii
import collections
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

import paste
import pipeline as pl
import updater

APP_NAME = "Kling Studio"
APP_VERSION = "3.8.1"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_FILE = BASE_DIR / "ui" / "index.html"
MAX_BODY = 48 * 1024 * 1024      # dropped pictures arrive as base64 in the body
IDLE_LIMIT = 75                  # no heartbeat (sent every 15 s) for this long means the window is gone
CREATE_NO_WINDOW = 0x08000000
NAME = r"([A-Za-z0-9_-]{1,60})"
CLIP = r"([A-Za-z0-9_-]{1,80})"
LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})  \[(info|warn|error)\] (.*)$")
STAGES = ("frames", "videos")


# ------------------------------------------------------------------ config

def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def config_dir():
    override = os.environ.get("KLING_STUDIO_HOME")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "KlingStudio" if appdata else Path.home() / ".kling_studio"


OLD_FLAT_FRAME_CREDITS = 12      # what every frame cost before the prices were looked up


class Config:
    """API key and output folder, saved per user in %APPDATA%\\KlingStudio"""

    def __init__(self):
        self.path = config_dir() / "config.json"
        try:
            data = pl.read_json(self.path, {}) or {}
        except (OSError, ValueError):
            data = {}
        self.api_key = data.get("api_key", "")
        self.output_dir = Path(data.get("output_dir") or Path.home() / "Documents" / APP_NAME)
        # Frames are priced by resolution (pipeline.FRAME_CREDITS). This is only an
        # override for when kie.ai's prices move: None means "use the published ones".
        saved = data.get("frame_credits")
        try:
            saved = int(saved) if saved else None
        except (TypeError, ValueError):
            saved = None
        # every config written before the prices were known holds the flat 12 this app
        # used to charge for every frame; that is the old default, not a choice, and
        # keeping it would hide the real 8 at 1K and 18 at 4K
        self.frame_credits = None if saved == OLD_FLAT_FRAME_CREDITS else saved
        self.update_source = str(data.get("update_source") or updater.DEFAULT_SOURCE)
        self.auto_update_check = bool(data.get("auto_update_check", True))
        # clips left rendering when the app closed are asked about again at the next
        # launch; that only reads status, so it can never cost anything
        self.auto_collect = bool(data.get("auto_collect", True))
        self.tutorial_done = bool(data.get("tutorial_done"))  # the guided tour only greets a new user once

    def save(self):
        pl.write_json(self.path, {"api_key": self.api_key, "output_dir": str(self.output_dir),
                                  "frame_credits": self.frame_credits,
                                  "tutorial_done": self.tutorial_done,
                                  "update_source": self.update_source,
                                  "auto_update_check": self.auto_update_check,
                                  "auto_collect": self.auto_collect})

    def public(self):
        """what the page may see: never the key itself"""
        key = self.api_key
        return {"has_key": bool(key), "key_hint": key[-4:] if len(key) >= 8 else "",
                "output_dir": str(self.output_dir), "config_path": str(self.path),
                "frame_credits": self.frame_credits, "tutorial_done": self.tutorial_done,
                "update_source": self.update_source, "auto_update_check": self.auto_update_check,
                "auto_collect": self.auto_collect}


# ------------------------------------------------------------------ OS helpers

def open_path(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal_path(path):
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        open_path(path.parent)


PICKER_LOCK = threading.Lock()


def pick_folder(initial):
    """Show the native folder picker in a child process; returns {"path": folder or None}.

    The app runs in the background, so a dialog it opened directly would appear
    behind the window. The child process owns its own top-most dialog.
    """
    if not PICKER_LOCK.acquire(blocking=False):
        raise ApiError(409, "The folder picker is already open. It may be behind another window.")
    out = Path(tempfile.gettempdir()) / f"kling-studio-folder-{secrets.token_hex(6)}.json"
    try:
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
        cmd += ["--pick-folder", str(initial or ""), "--pick-folder-out", str(out)]
        try:
            # a windowless .exe has no console handles, so hand the child explicit ones
            subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=900, creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ApiError(500, f"Couldn't open the folder picker ({e}). Type the folder path instead.") from None
        try:
            result = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ApiError(500, "Couldn't open the folder picker. Type the folder path instead.") from None
        if result.get("error"):
            raise ApiError(500, f"Couldn't open the folder picker ({result['error']}). Type the path instead.")
        return {"path": result.get("path")}
    finally:
        PICKER_LOCK.release()
        try:
            out.unlink()
        except OSError:
            pass


def run_folder_picker(initial, out_path):
    """child side of pick_folder: show the dialog and write {"path"} or {"error"} to out_path"""
    result = {"path": None}
    try:
        if sys.platform == "win32":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        import tkinter as tk
        from tkinter import filedialog

        start = Path(initial) if initial else Path.home()
        while not start.is_dir() and start.parent != start:
            start = start.parent
        if not start.is_dir():
            start = Path.home()
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # keeps the dialog above the app window
        root.update()
        chosen = filedialog.askdirectory(parent=root, initialdir=str(start), mustexist=False,
                                         title=f"Choose where {APP_NAME} saves batches")
        root.destroy()
        result["path"] = str(Path(chosen)) if chosen else None
    except Exception as e:
        result["error"] = str(e) or e.__class__.__name__
    Path(out_path).write_text(json.dumps(result), encoding="utf-8")


def write_error_log(text):
    try:
        with open(config_dir() / "error.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ core

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


ARCHIVE_DIR = "_archive"      # batch folders moved out of the way; listings skip names starting with _


def folder_bytes(path: Path):
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass              # a file that vanished mid-walk just doesn't count
    return total


def save_upload(folder: Path, filename, data_b64, stem=None):
    """write a picture the page sent as base64; returns the saved file name"""
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in pl.IMG_EXT:
        raise ApiError(400, "That file isn't a .png, .jpg or .webp picture.")
    try:
        raw = base64.b64decode(str(data_b64 or "").split(",")[-1], validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "The picture didn't arrive in one piece. Try again.") from None
    if not raw:
        raise ApiError(400, "That picture is empty.")
    name = f"{stem}{ext}" if stem else re.sub(r"[^A-Za-z0-9_.-]", "_", Path(str(filename)).name)[-80:]
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (name + ".part")
    tmp.write_bytes(raw)
    pl.replace_path(tmp, folder / name)
    return name


class Core:
    """shared state behind the API: open batches, running batches, activity logs"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.batches = {}       # name -> pl.Batch
        self.runners = {}       # name -> pl.Runner
        self.logs = {}          # name -> deque of log entries
        self.stop_reasons = {}  # name -> why the last run stopped early
        self.anchors = {}       # name -> thread drawing that batch's anchor
        self.last_ping = time.time()
        self.bye_at = 0.0
        self.shutdown = None      # set by create_server, so an install can close the server
        self.window_proc = None   # the browser showing the app, so an install can close it
        # state: idle | checking | current | ready | downloading | installing | error
        self.update = {"state": "idle", "checked": 0.0, "version": "", "notes": [], "page": "",
                       "url": "", "sha256": "", "size": 0, "progress": 0, "error": ""}

    def active_runs(self):
        with self.lock:
            return [n for n, r in self.runners.items() if r.is_alive()]

    # ---- updates

    def update_state(self):
        with self.lock:
            u = {k: v for k, v in self.update.items() if k not in ("url", "sha256")}
        u.update(app_version=APP_VERSION, can_install=updater.can_self_install(),
                 has_download=bool(self.update["url"]), platform=updater.PLATFORM_KEY,
                 source=self.cfg.update_source, auto=self.cfg.auto_update_check,
                 busy=bool(self.active_runs()))
        return u

    def check_update(self):
        """Read the manifest. Never installs anything, never raises."""
        with self.lock:
            if self.update["state"] in ("checking", "downloading", "installing"):
                return self.update_state()
            self.update.update(state="checking", error="")
        try:
            info = updater.check(self.cfg.update_source, APP_VERSION)
        except Exception as e:                                  # noqa: BLE001 - the message is the UI
            with self.lock:
                self.update.update(state="error", error=str(e), checked=time.time())
            return self.update_state()
        with self.lock:
            self.update.update(checked=time.time(), error="", progress=0,
                               version=info["version"], notes=info["notes"],
                               page=info["page"] or updater.RELEASES_PAGE,
                               url=info["url"], sha256=info["sha256"], size=info["size"],
                               state="ready" if info["newer"] else "current")
        return self.update_state()

    def install_update(self):
        with self.lock:
            state, version = self.update["state"], self.update["version"]
            if state != "ready":
                raise ApiError(409, "There's no update ready to install. Check again first.")
            if not self.update["url"]:
                raise ApiError(400, "That release has no download for this computer yet. "
                                    "Open the release page to get it.")
            if self.active_runs():
                raise ApiError(409, "Finish or stop the running batch before updating.")
            if not updater.can_self_install():
                raise ApiError(400, "This copy runs from Python, so it can't replace itself. "
                                    "Open the release page and copy the new build over the old one.")
            self.update.update(state="downloading", progress=0, error="")
        threading.Thread(target=self._install_worker, name=f"install-{version}", daemon=True).start()
        return self.update_state()

    def _install_worker(self):
        def progress(done, total, attempt):
            with self.lock:
                self.update["progress"] = int(done / total * 100) if total else 0
        try:
            with self.lock:
                info = {k: self.update[k] for k in ("version", "url", "sha256", "size")}
            path = updater.download_update(info, progress)
            with self.lock:
                self.update.update(state="installing", progress=100)
            updater.apply_update(path)
        except Exception as e:                                  # noqa: BLE001 - the message is the UI
            with self.lock:
                self.update.update(state="ready", progress=0, error=f"Update failed: {e}")
            return
        # From here the swap script is waiting for this process to end, so getting out
        # matters more than getting out tidily: every step is allowed to fail.
        time.sleep(3.5)          # the page sees "installing", says so, and closes itself
        self.close_window()      # and if the browser refused, close it from here
        if self.shutdown:
            # shutdown() waits for the serving loop, which an odd connection could hold
            # up; it runs on its own thread so a slow one can't strand the update
            threading.Thread(target=self._quietly, args=(self.shutdown,), daemon=True).start()
        time.sleep(0.6)
        os._exit(0)

    @staticmethod
    def _quietly(fn):
        try:
            fn()
        except Exception:                                       # noqa: BLE001 - best effort
            pass

    def close_window(self):
        """Shut the app window before the swap, so the page can't sit there looking stuck.

        The page closes itself when it can; this is the belt for the cases where a
        browser refuses window.close(). Without it the old window stays on screen
        beside the new one the restarted app opens.
        """
        proc = self.window_proc
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:                                       # noqa: BLE001 - best effort
            pass

    # ---- logs

    def _log_buffer(self, batch):
        buf = self.logs.get(batch.name)
        if buf is None:
            buf = collections.deque(maxlen=300)
            p = batch.root / "activity.log"
            if p.exists():
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]:
                    m = LOG_LINE_RE.match(line)
                    if m:
                        buf.append({"time": m.group(1), "level": m.group(2), "text": m.group(3)})
            self.logs[batch.name] = buf
        return buf

    def log(self, batch, text, level="info"):
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "text": text}
        with self.lock:
            self._log_buffer(batch).append(entry)
        try:
            with open(batch.root / "activity.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d')} {entry['time']}  [{level}] {text}\n")
        except OSError:
            pass

    # ---- batches

    def get_batch(self, name):
        with self.lock:
            root = self.cfg.output_dir / name
            b = self.batches.get(name)
            if b is None or b.root != root:
                if not (root / "job.json").exists():
                    raise ApiError(404, f"There's no batch named “{name}”.")
                b = pl.Batch(root)
                thread = self.anchors.get(name)
                if b.anchor().get("status") == "working" and not (thread and thread.is_alive()):
                    # a copy of the app stopped while this was being drawn; without this
                    # the dialog would spin forever waiting for a thread that is gone
                    b.set_anchor(status="failed", error="The app closed while this was being "
                                                        "drawn, so it never arrived. Draw it again.")
                self.batches[name] = b
            return b

    def _drawing(self, name, b):
        """is a reference being drawn right now?

        The thread stays alive for a moment after the picture lands, so the status is
        what decides; the thread only rules out a draw that died without clearing it.
        """
        thread = self.anchors.get(name)
        return b.anchor().get("status") == "working" and bool(thread and thread.is_alive())

    def delete_batch(self, name):
        """Remove a batch and everything in its folder. Refuses while it is running."""
        with self.lock:
            if name in self.active_runs():
                raise ApiError(409, "Stop the batch before deleting it.")
            if name in self.anchors and self.anchors[name].is_alive():
                raise ApiError(409, "A reference is still being drawn. Wait for it, then delete.")
            out = self.cfg.output_dir.resolve()
            root = (self.cfg.output_dir / name).resolve()
            if root == out or out not in root.parents:
                raise ApiError(400, "That isn't a batch folder.")
            if not (root / "job.json").exists():
                raise ApiError(404, f"There's no batch named “{name}”.")
            try:
                shutil.rmtree(root)
            except OSError as e:
                raise ApiError(409, f"Couldn't delete the folder: {e.strerror or e}. "
                                    "Close anything that has those files open and try again.") from None
            for store in (self.batches, self.logs, self.stop_reasons, self.runners):
                store.pop(name, None)
        return {"deleted": name}

    def anchor(self, name, body):
        """The character portrait a batch locks onto: write the prompt, draw it,
        look at it, lock it. Only "generate" spends anything."""
        b = self.get_batch(name)
        with self.lock:
            if name in self.runners:
                raise ApiError(409, "Wait for this batch to finish before changing the anchor.")
            if self._drawing(name, b):
                raise ApiError(409, "The anchor is still being drawn.")
        action = str(body.get("action") or "prompt")

        if action == "prompt":
            b.set_anchor(prompt=str(body.get("prompt") or "").strip())
            return self.detail(b)

        if action in ("use", "lock"):      # "lock" kept for older pages
            a = b.anchor()
            if not (a.get("file") and (b.refs_dir / a["file"]).is_file()):
                raise ApiError(400, "Draw it (or drop a picture in) first.")
            refs = b.job.setdefault("references", [])
            if not any(r.get("file") == a["file"] for r in refs):
                refs.append({"kind": "file", "file": a["file"]})
            b.job["anchor"] = {"prompt": a.get("prompt", "")}     # the prompt stays for a redraw
            b.save_job()
            self.log(b, "Reference kept: every frame in this batch is drawn with it.")
            return self.detail(b)

        if action == "unlock":                                    # older pages
            b.set_anchor(locked=False)
            return self.detail(b)

        if action in ("clear", "discard"):
            a = b.anchor()
            file = a.get("file")
            if file and not any((r.get("file") == file) for r in (b.job.get("references") or [])):
                (b.refs_dir / file).unlink(missing_ok=True)       # only if it never joined the list
            b.job["anchor"] = {} if action == "clear" else {"prompt": a.get("prompt", "")}
            b.save_job()
            self.log(b, "Draft reference discarded.")
            return self.detail(b)

        if action == "generate":
            if not self.cfg.api_key:
                raise ApiError(400, "Add your kie.ai API key in Settings first.")
            prompt = str(body.get("prompt") or b.anchor().get("prompt") or "").strip()
            if not prompt:
                raise ApiError(400, "Write what the anchor should show first.")
            b.set_anchor(prompt=prompt, status="working", error="", started_at=time.time())
            t = threading.Thread(target=self._anchor_worker, args=(b, prompt), daemon=True,
                                 name=f"anchor-{name}")
            with self.lock:
                self.anchors[name] = t
            t.start()
            return self.detail(b)

        raise ApiError(400, "Unknown anchor action.")

    def _anchor_worker(self, b, prompt):
        """one nano-banana-2 still, saved into refs/ - the same call a frame makes"""
        try:
            self.log(b, "Anchor: sending to kie.ai…")
            task = pl.submit_image(prompt, b.job.get("image_settings", {}), [], self.cfg.api_key)
            b.record_spend("reference", self.frame_price(b), task=task,   # charged now, at the estimate
                           signature=pl.frame_signature(b.job.get("image_settings")))
            deadline = time.time() + pl.POLL_TIMEOUT
            url = None
            while time.time() < deadline:
                time.sleep(max(3, pl.POLL_EVERY // 4))
                status, result, raw = pl.check_task(task, self.cfg.api_key, pl.IMAGE_SUFFIXES)
                if status in ("done", "failed"):
                    moved = b.settle_spend(task, pl.credits_consumed(raw))
                    if moved and moved[0] != moved[1]:
                        self.log(b, f"kie.ai charged {moved[1]:g} credits for that picture"
                                    f"{f', not the {moved[0]:g} estimated' if moved[0] else ''}.")
                if status == "done":
                    url = result
                    break
                if status == "failed":
                    raise RuntimeError(result or "kie.ai rejected the anchor prompt")
            if not url:
                raise RuntimeError("kie.ai didn't finish the anchor in time")
            old = b.anchor().get("file")
            name = f"anchor-{int(time.time())}.png"
            pl.download_file(url, b.refs_dir / name)
            if old and old != name and not self._in_use(b, old):
                (b.refs_dir / old).unlink(missing_ok=True)
            b.set_anchor(file=name, status="ready", error="", locked=False, version=int(time.time()))
            self.log(b, "Reference drawn. Look at it, then keep it.")
        except Exception as e:                                  # noqa: BLE001 - the message is the UI
            b.set_anchor(status="failed", error=str(e))
            self.log(b, f"Anchor failed: {e}", "error")

    def _in_use(self, b, file):
        """is this picture still pointed at by the batch or by any clip?"""
        if any(r.get("file") == file for r in (b.job.get("references") or [])):
            return True
        if (b.anchor().get("file") or "") == file:
            return True
        return any((c.get("ref") or {}).get("file") == file for c in b.clips())

    # ---- what this batch's work costs

    def frame_price(self, b):
        """credits for one frame of this batch: your override, then what kie.ai last
        charged this batch for a frame like it, then the published price"""
        image_settings = b.job.get("image_settings")
        return (self.cfg.frame_credits
                or b.last_charged("frame", pl.frame_signature(image_settings))
                or pl.credits_per_frame(image_settings))

    def video_price(self, b):
        settings = {**pl.DEFAULT_SETTINGS, **(b.job.get("settings") or {})}
        return b.last_charged("video", pl.video_signature(settings)) or pl.credits_per_video(settings)

    def prices(self, b):
        """the numbers behind every estimate the page shows for this batch"""
        settings = {**pl.DEFAULT_SETTINGS, **(b.job.get("settings") or {})}
        video = self.video_price(b)
        seconds = 0
        try:
            seconds = int(settings.get("duration", 5))
        except (TypeError, ValueError):
            pass
        learned = bool(b.last_charged("frame", pl.frame_signature(b.job.get("image_settings")))
                       or b.last_charged("video", pl.video_signature(settings)))
        return {"frame": self.frame_price(b), "video": video,
                "video_per_second": (round(video / seconds, 2) if video and seconds else None),
                "override": bool(self.cfg.frame_credits),
                "learned": learned}

    def _forget_file(self, b, file):
        """bin a picture in refs/ once nothing at all points at it any more"""
        if file and Path(file).name == file and not self._in_use(b, file):
            (b.refs_dir / file).unlink(missing_ok=True)

    def batch_references(self, name, body):
        """Manage the pictures that apply to the whole batch, and the anchor frame."""
        b = self.get_batch(name)
        with self.lock:
            if name in self.runners:
                raise ApiError(409, "Wait for this batch to finish before changing references.")
        remove = str(body.get("remove") or "")
        if remove:
            refs = b.job.get("references") or []
            kept = [r for r in refs if r.get("file") != remove and r.get("path") != remove]
            if len(kept) == len(refs):
                raise ApiError(404, "That reference isn't on this batch.")
            b.job["references"] = kept
            b.save_job()
            # a clip may still be pointing at the same picture: only bin the file
            # once nothing at all refers to it
            self._forget_file(b, remove)
            self.log(b, "Reference removed.")
            return self.detail(b)

        if "anchor" in body:
            clips = b.clips()
            anchor = body.get("anchor")
            if anchor in (None, 0, ""):
                b.job["anchor_frame"] = 0
                self.log(b, "Anchor frame cleared.")
            else:
                try:
                    index = int(anchor)
                except (TypeError, ValueError):
                    raise ApiError(400, "The anchor has to be a clip number.") from None
                if not 0 < index <= len(clips):
                    raise ApiError(400, f"This batch has {plural(len(clips), 'clip')}.")
                b.job["anchor_frame"] = index
                self.log(b, f"Frame {index} is the anchor: it is drawn first, then sent with "
                            "every other clip as well as whatever that clip already uses.")
            b.save_job()
            return self.detail(b)

        raise ApiError(400, "Nothing to do: send remove or anchor.")

    def summaries(self):
        out = []
        try:
            folders = [d for d in self.cfg.output_dir.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))]
        except OSError:
            return out
        for d in folders:
            with self.lock:
                b = self.batches.get(d.name)
                runner = self.runners.get(d.name)
            if b is None or b.root != d:
                # open batches (including running ones) aren't re-read from disk, so a runner
                # writing state.json can't make them vanish from the list
                if not (d / "job.json").exists():
                    continue
                try:
                    b = pl.Batch(d)
                except (OSError, ValueError):
                    continue
            out.append({"name": b.name, "created": b.job.get("created", ""), "counts": b.counts(),
                        "running": runner is not None, "stage": runner.stage if runner else None})
        return sorted(out, key=lambda s: s["created"], reverse=True)

    def detail(self, b):
        snap = b.snapshot()
        with self.lock:
            runner = self.runners.get(b.name)
            log = list(self._log_buffer(b))[-200:]
            stop = self.stop_reasons.get(b.name) or {}
        clips = []
        for i, c in enumerate(b.clips()):
            n = c["name"]
            st = snap.get(n) or {}
            frame, video = dict(st.get("frame") or {"status": "pending"}), dict(st.get("video") or {"status": "pending"})
            frame_file = b.frame_path(n)
            frame_ready = b.frame_ready(n)
            video_ready = b.video_ready(n)
            need = b.reference_urls_needed(n)
            plan = b.reference_plan(n)
            clips.append({
                "index": i, "name": n, "image": c.get("image", ""), "motion": c.get("motion", ""),
                "ref": c.get("ref"), "needs_reference": bool(need and need[0] == "ask"),
                "reference_note": (need[1] if need and need[0] == "ask" else ""),
                "references_planned": len(plan),
                "reference_labels": [it["label"] for it in plan],
                # enough for the page to show each picture and say where it came from
                "reference_items": [{
                    "label": it["label"], "kind": it["kind"], "file": it["file"],
                    "clip": it["clip"], "waiting": bool(it["waiting_on"]),
                    "path": "" if (it["file"] or it["clip"]) else str(it["path"]),
                } for it in plan],
                "frame": {**frame, "ready": frame_ready,
                          "version": int(frame_file.stat().st_mtime) if frame_ready else None},
                "video": {**video, "ready": video_ready,
                          "version": int(b.video_path(n).stat().st_mtime) if video_ready else None},
            })
        return {
            "name": b.name, "created": b.job.get("created", ""),
            "settings": {**pl.DEFAULT_SETTINGS, **b.job.get("settings", {})},
            "image_settings": {**pl.DEFAULT_IMAGE_SETTINGS, **b.job.get("image_settings", {})},
            "references": b.job.get("references") or [],
            "anchor": b.anchor(),
            "anchor_frame": b.anchor_frame(),
            "clips": clips, "counts": b.counts(), "plan": b.plan(),
            "reference_limit": pl.MAX_REFERENCES,
            "spend": b.spend(), "prices": self.prices(b),
            "running": runner is not None, "stage": runner.stage if runner else None,
            "stopping": bool(runner and runner.cancelled),
            "stop_reason": None if runner else (stop.get("text") or None),
            "stop_kind": None if runner else (stop.get("kind") or None),
            "folder": str(b.root), "log": log,
        }

    # ---- creating and running

    @staticmethod
    def clean_settings(raw, defaults):
        out = dict(defaults)
        for k, v in (raw or {}).items():
            if k in defaults and str(v).strip() != "":
                out[k] = bool(v) if isinstance(defaults[k], bool) else str(v).strip()
        if "duration" in out and not str(out["duration"]).isdigit():
            raise ApiError(400, "Duration must be a whole number of seconds.")
        return out

    def create(self, body):
        name = str(body.get("name") or "").strip()
        clips = body.get("clips") or []
        if not pl.BATCH_NAME_RE.match(name):
            raise ApiError(400, "Batch name can only use letters, numbers, _ and -.")
        if not clips:
            raise ApiError(400, "Paste the prompts first: every clip needs an image prompt and a motion prompt.")
        cleaned = []
        for c in clips:
            ref = c.get("ref")
            if ref is not None and not isinstance(ref, dict):
                raise ApiError(400, "Bad reference.")
            cleaned.append({"image": str(c.get("image") or "").strip(),
                            "motion": str(c.get("motion") or "").strip(), "ref": ref})
        settings = self.clean_settings(body.get("settings"), pl.DEFAULT_SETTINGS)
        image_settings = self.clean_settings(body.get("image_settings"), pl.DEFAULT_IMAGE_SETTINGS)
        references = [r for r in (body.get("references") or []) if isinstance(r, dict)]
        with self.lock:
            try:
                self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
                b = pl.Batch.create(self.cfg.output_dir, name, cleaned, settings, image_settings, references)
            except FileExistsError as e:
                raise ApiError(409, str(e)) from None
            except (ValueError, OSError) as e:
                raise ApiError(400, str(e)) from None
            self.batches[name] = b
        self.log(b, f"Created batch with {len(cleaned)} clips.")
        return self.detail(b)

    def run(self, name, body):
        b = self.get_batch(name)
        stage = str(body.get("stage") or "")
        if stage not in STAGES:
            raise ApiError(400, "Unknown stage.")
        names = [c["name"] for c in b.clips()]
        redo = [str(n) for n in body.get("redo") or []]
        if any(n not in names for n in redo):
            raise ApiError(400, "Unknown clip.")
        with self.lock:
            if name in self.runners:
                raise ApiError(409, "This batch is already working.")
            if self._drawing(name, b):
                raise ApiError(409, "The anchor is still being drawn. Wait for it, then run.")
        plan = b.plan(redo_frames=redo if stage == "frames" else (),
                      redo_videos=redo if stage == "videos" else ())
        make, check = plan[f"{stage}_make"], plan[f"{stage}_check"]
        if redo:
            make, check = [n for n in make if n in redo], []
            self.log(b, f"Redoing {stage[:-1]} for {', '.join(redo)}.")
        if not make and not check:
            raise ApiError(400, "Nothing to do for that step.")
        self.start(b, stage, make, check)
        return self.detail(b)

    def start(self, b, stage, make, check):
        if not self.cfg.api_key:
            raise ApiError(400, "Add your kie.ai API key in Settings first.")
        runner = None

        def emit(kind, **data):
            if kind == "log":
                self.log(b, data["text"], data.get("level", "info"))
            elif kind == "finished":
                c = b.counts()
                reason = None
                if data.get("error"):
                    self.log(b, f"Stopped early. {c['frames']} frames, {c['videos']} videos.", "error")
                    reason = {"kind": "error", "text": data["error"]}
                elif data["cancelled"]:
                    # anything already sent was paid for: say so, and say that collecting is free
                    left = len(b.plan()[f"{stage}_check"])
                    tail = (f" {plural(left, stage[:-1])} kie.ai is still working on — collecting "
                            "them costs nothing." if left else "")
                    reason = {"kind": "stopped", "text": f"You stopped this run.{tail}"}
                    self.log(b, "Stopped. Anything already sent keeps running on kie.ai; check again "
                                "later to collect it (no extra credits).", "warn")
                else:
                    self.log(b, f"Finished {stage}: {c['frames']} frames, {c['videos']} videos, "
                                f"{c['failed']} failed.")
                with self.lock:
                    self.stop_reasons[b.name] = reason
                    if self.runners.get(b.name) is runner:
                        del self.runners[b.name]

        runner = pl.Runner(b, self.cfg.api_key, stage, make, check, emit,
                           frame_credits=self.frame_price(b))
        with self.lock:
            if b.name in self.runners:
                raise ApiError(409, "This batch is already working.")
            self.runners[b.name] = runner
            self.stop_reasons.pop(b.name, None)
        runner.start()

    def stop(self, name):
        with self.lock:
            runner = self.runners.get(name)
        if runner:
            runner.cancel()
            self.log(runner.batch, "Stopping after the current step…", "warn")
        return self.detail(self.get_batch(name))

    def resume_checks(self, limit=6):
        """Collect work that was already paid for.

        Closing the app (or stopping a run) leaves clips marked "sent": kie.ai keeps
        rendering them and the files are still waiting. Asking for their status and
        downloading them costs nothing, so it is done on its own at the next launch.
        """
        if not self.cfg.api_key or not self.cfg.auto_collect:
            return []
        started = []
        for s in self.summaries():
            if len(started) >= limit:
                break
            if s["running"]:
                continue
            try:
                b = self.get_batch(s["name"])
            except ApiError:
                continue
            plan = b.plan()
            stage = "frames" if plan["frames_check"] else "videos" if plan["videos_check"] else None
            if not stage:
                continue
            waiting = plan[f"{stage}_check"]
            try:
                self.start(b, stage, [], waiting)
            except ApiError:
                continue
            self.log(b, f"Picking up {plural(len(waiting), stage[:-1])} kie.ai was already working on "
                        "when the app last closed. Nothing new is sent, so this is free.")
            started.append(s["name"])
        return started

    # ---- disk space

    def storage(self):
        """How much room every batch takes, live and archived, biggest first."""
        out = self.cfg.output_dir
        rows = []
        for base, archived in ((out, False), (out / ARCHIVE_DIR, True)):
            try:
                folders = sorted(d for d in base.iterdir() if d.is_dir() and (d / "job.json").is_file())
            except OSError:
                continue
            for d in folders:
                if not archived and d.name.startswith((".", "_")):
                    continue
                try:
                    job = pl.read_json(d / "job.json", {}) or {}
                except (OSError, ValueError):
                    job = {}
                rows.append({"name": d.name, "created": job.get("created", ""),
                             "clips": len(job.get("clips") or []),
                             "videos": len(list((d / "videos").glob("*.mp4"))) if (d / "videos").is_dir() else 0,
                             "bytes": folder_bytes(d), "archived": archived,
                             "running": d.name in self.active_runs()})
        rows.sort(key=lambda r: r["bytes"], reverse=True)
        return {"folder": str(out), "archive_folder": str(out / ARCHIVE_DIR),
                "total": sum(r["bytes"] for r in rows),
                "live": sum(r["bytes"] for r in rows if not r["archived"]),
                "batches": rows}

    def archive(self, names, restore=False):
        """Move whole batch folders in or out of the _archive folder.

        Nothing is deleted: an archived batch keeps every frame and video, it just
        stops filling the list. The app never prunes anything on its own.
        """
        moved, failed = [], []
        with self.lock:
            store = self.cfg.output_dir / ARCHIVE_DIR
            for raw in names if isinstance(names, list) else []:
                name = str(raw)
                src = (store / name) if restore else (self.cfg.output_dir / name)
                dst = (self.cfg.output_dir / name) if restore else (store / name)
                if not pl.BATCH_NAME_RE.match(name):
                    failed.append({"name": name, "why": "That isn't a batch name."})
                elif name in self.active_runs():
                    failed.append({"name": name, "why": "It is running. Stop it first."})
                elif name in self.anchors and self.anchors[name].is_alive():
                    failed.append({"name": name, "why": "A reference is still being drawn."})
                elif not (src / "job.json").is_file():
                    failed.append({"name": name, "why": "It isn't there."})
                elif dst.exists():
                    failed.append({"name": name, "why": "Something with that name is already there."})
                else:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        pl.replace_path(src, dst)      # same folder tree, so this is a rename
                    except OSError as e:
                        failed.append({"name": name, "why": e.strerror or str(e)})
                        continue
                    for store_ in (self.batches, self.logs, self.stop_reasons):
                        store_.pop(name, None)
                    moved.append(name)
        return {"moved": moved, "failed": failed, "restored": bool(restore), "storage": self.storage()}

    # ---- edits from the page

    def edit_clip(self, name, body):
        b = self.get_batch(name)
        clip_name = str(body.get("clip") or "")
        clip = b.clip(clip_name)
        if not clip:
            raise ApiError(404, "Unknown clip.")
        with self.lock:
            if name in self.runners:
                raise ApiError(409, "Wait for this batch to finish before editing it.")
        for field in ("image", "motion"):
            if field in body:
                clip[field] = str(body[field] or "").strip()
        replaced = ""
        if "ref" in body:
            ref = body["ref"]
            old = clip.get("ref") or {}
            new = dict(ref) if isinstance(ref, dict) else None
            if new is not None and new.get("kind") != "needed" and (old.get("asked") or old.get("kind") == "needed"):
                new.setdefault("asked", True)      # the skill asked for a picture here
                new.setdefault("note", old.get("note", ""))
            if old.get("file") and old["file"] != (new or {}).get("file"):
                replaced = old["file"]
            clip["ref"] = new
        b.save_job()
        if replaced:
            self._forget_file(b, replaced)      # the picture it was using, if nothing else wants it
            self.log(b, f"{clip_name}: reference changed.")
        return self.detail(b)

    def attach(self, name, body):
        """a picture the page sent: a reference for one clip (or the batch), or a frame itself"""
        b = self.get_batch(name)
        kind = str(body.get("kind") or "reference")
        clip_name = str(body.get("clip") or "")
        with self.lock:
            if name in self.runners:
                raise ApiError(409, "Wait for this batch to finish before adding pictures.")
        if kind == "anchor":
            old = b.anchor().get("file")
            saved = save_upload(b.refs_dir, body.get("filename"), body.get("data"),
                                stem=f"anchor-{int(time.time())}")
            if old and old != saved and not self._in_use(b, old):
                (b.refs_dir / old).unlink(missing_ok=True)
            b.set_anchor(file=saved, status="ready", error="", locked=False, source="dropped",
                         version=int(time.time()))
            self.log(b, "Picture staged as a reference.")
            return self.detail(b)
        if kind == "frame":
            clip = b.clip(clip_name)
            if not clip:
                raise ApiError(404, "Unknown clip.")
            for old in b.frames_dir.glob(f"{clip_name}.*"):
                old.unlink(missing_ok=True)
            saved = save_upload(b.frames_dir, body.get("filename"), body.get("data"), stem=clip_name)
            b.update(clip_name, "frame", status="done", file=saved, source="dropped", error=None,
                     stage=None, task_id=None, failed_by=None, finished_at=time.time())
            self.log(b, f"{clip_name}: frame replaced with a picture you dropped in.")
        elif kind == "reference":
            # one picture, however many clips want it: uploaded once, stored once
            targets = [str(c) for c in (body.get("clips") or []) if str(c)] or ([clip_name] if clip_name else [])
            existing = str(body.get("file") or "")      # reuse a picture already in refs/
            if existing:
                if not (b.refs_dir / existing).is_file() or Path(existing).name != existing:
                    raise ApiError(404, "That picture isn't in this batch any more.")
                saved = existing
            else:
                saved = save_upload(b.refs_dir, body.get("filename"), body.get("data"))
            for target in targets:
                if not b.clip(target):
                    raise ApiError(404, f"Unknown clip {target}.")
            replaced = []
            for target in targets:
                clip = b.clip(target)
                old = clip.get("ref") or {}
                clip["ref"] = {"kind": "file", "file": saved}
                if old.get("kind") == "needed":
                    # remember that the skill asked for one, so taking this picture off
                    # again puts the clip back to waiting with its note intact
                    clip["ref"]["asked"] = True
                    clip["ref"]["note"] = old.get("note", "")
                elif old.get("asked"):
                    clip["ref"]["asked"] = True
                    clip["ref"]["note"] = old.get("note", "")
                if old.get("file") and old["file"] != saved:
                    replaced.append(old["file"])
            if targets:
                self.log(b, f"Reference picture added to {plural(len(targets), 'clip')}: "
                            f"{', '.join(targets)}.")
            else:
                refs = b.job.setdefault("references", [])
                if not any(r.get("file") == saved for r in refs):
                    refs.append({"kind": "file", "file": saved})
                self.log(b, "Style reference added for every frame in the batch.")
            b.save_job()
            for file in replaced:
                self._forget_file(b, file)      # nothing points at the old one any more
        else:
            raise ApiError(400, "Unknown kind.")
        return self.detail(b)


# ------------------------------------------------------------------ HTTP

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


def safe_child(base: Path, rel):
    base = base.resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        raise ApiError(404, "Not found")
    return p


class Handler(BaseHTTPRequestHandler):
    server_version = f"KlingStudio/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    @property
    def core(self) -> Core:
        return self.server.core

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        parsed = urlparse(self.path)
        self.query = parse_qs(parsed.query)
        path = parsed.path
        self.body = b""
        try:
            # read the whole body first: a body left unread would corrupt the next request
            # on this keep-alive connection (a 403, or a route that ignores its body)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if self.headers.get("Transfer-Encoding") or length < 0 or length > MAX_BODY:
                self.close_connection = True
                raise ApiError(413 if length > MAX_BODY else 400, "Bad request body")
            if length:
                self.body = self.rfile.read(length)
            if self.headers.get("Host", "") not in self.server.allowed_hosts:
                raise ApiError(403, "Forbidden")
            if method == "GET" and path in ("/", "/index.html"):
                return self.send_index()
            if not path.startswith("/api/"):
                raise ApiError(404, "Not found")
            token = self.headers.get("X-PK-Token") or (self.query.get("t") or [""])[0]
            if not secrets.compare_digest(token.encode(), self.server.token.encode()):
                raise ApiError(403, "Forbidden")
            for m, rx, fn in ROUTES:
                match = rx.match(path) if m == method else None
                if match:
                    result = fn(self, *[unquote(g) for g in match.groups()])
                    if result is not None:
                        self.send_json(result)
                    return
            raise ApiError(404, "Not found")
        except ApiError as e:
            self.send_json({"error": e.message}, e.status)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception as e:
            write_error_log(f"{method} {path}\n{traceback.format_exc()}")
            try:
                self.send_json({"error": f"Unexpected error: {e}"}, 500)
            except OSError:
                pass

    # ---- responses

    def read_body(self):
        raw = self.body
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "Invalid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "Invalid JSON")
        return data

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_index(self):
        html = UI_FILE.read_text(encoding="utf-8")
        html = html.replace("__PK_TOKEN__", self.server.token).replace("__PK_VERSION__", APP_VERSION)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; worker-src blob:; "
                         "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, cache="private, max-age=600"):
        try:  # open before any header is sent, so a file that just vanished is a clean 404
            f = open(path, "rb")
        except OSError:
            raise ApiError(404, "Not found") from None
        with f:
            size = os.fstat(f.fileno()).st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end, status = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache)
            # download managers hook anything that looks like a file to save; a .webp
            # or an .mp4 gets taken over and the page is left with a broken picture
            self.send_header("Content-Disposition", "inline")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


# ---- routes

@route("GET", "/api/ping")
@route("POST", "/api/ping")
def api_ping(h):
    h.core.last_ping = time.time()
    return {"ok": True, "version": APP_VERSION}


@route("POST", "/api/bye")
def api_bye(h):
    h.core.bye_at = time.time()
    return {"ok": True}


UI_ASSETS = {"logo.png", "mark.png"}      # the brand art the page is allowed to ask for


@route("GET", r"/api/ui/([A-Za-z0-9_.-]{1,40})")
def api_ui_asset(h, name):
    if name not in UI_ASSETS:
        raise ApiError(404, "Not found")
    h.send_file(BASE_DIR / "ui" / name, cache="private, max-age=86400")


@route("GET", "/api/bootstrap")
def api_bootstrap(h):
    return {"version": APP_VERSION, "app_name": APP_NAME, "config": h.core.cfg.public(),
            "defaults": pl.DEFAULT_SETTINGS, "image_defaults": pl.DEFAULT_IMAGE_SETTINGS,
            "prices": pl.PRICES,
            "batches": h.core.summaries(), "update": h.core.update_state()}


@route("GET", "/api/update")
def api_update(h):
    return h.core.update_state()


@route("POST", "/api/update/check")
def api_update_check(h):
    h.read_body()
    return h.core.check_update()


@route("POST", "/api/update/install")
def api_update_install(h):
    h.read_body()
    return h.core.install_update()


@route("POST", "/api/update/page")
def api_update_page(h):
    h.read_body()
    page = h.core.update_state().get("page") or updater.RELEASES_PAGE
    if not page.startswith("https://"):
        raise ApiError(400, "That release page isn't a valid https address.")
    try:
        webbrowser.open(page)
    except Exception as e:                                     # noqa: BLE001
        raise ApiError(500, f"Couldn't open the page: {e}") from None
    return {"opened": page}


@route("POST", "/api/config")
def api_config(h):
    body = h.read_body()
    core, cfg = h.core, h.core.cfg
    new_dir = None
    if "output_dir" in body:  # validate everything before changing anything
        out = str(body["output_dir"]).strip()
        if not out:
            raise ApiError(400, "Choose an output folder.")
        new = Path(os.path.expandvars(out)).expanduser()
        if not new.is_absolute():
            raise ApiError(400, "Use a full folder path, for example D:\\Videos\\Kling Studio.")
        if new != cfg.output_dir:
            if core.active_runs():
                raise ApiError(409, "Change the output folder after the running batches finish.")
            try:
                new.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ApiError(400, f"Can't use that folder: {e.strerror or e}") from None
            new_dir = new
    if "frame_credits" in body:
        value = str(body["frame_credits"]).strip()
        if value in ("", "0"):
            cfg.frame_credits = None            # empty means "use kie.ai's published prices"
        elif value.isdigit():
            cfg.frame_credits = int(value)
        else:
            raise ApiError(400, "Credits per frame must be a whole number.")
    if "update_source" in body:
        src = str(body["update_source"]).strip() or updater.DEFAULT_SOURCE
        if not src.lower().startswith("https://"):
            raise ApiError(400, "The update feed must be an https address ending in latest.json.")
        cfg.update_source = src
        with core.lock:
            core.update.update(state="idle", version="", notes=[], url="", error="", progress=0)
    if "auto_update_check" in body:
        cfg.auto_update_check = bool(body["auto_update_check"])
    if "auto_collect" in body:
        cfg.auto_collect = bool(body["auto_collect"])
    if "tutorial_done" in body:
        cfg.tutorial_done = bool(body["tutorial_done"])
    if "api_key" in body:
        cfg.api_key = str(body["api_key"]).strip()
    if new_dir is not None:
        cfg.output_dir = new_dir
        with core.lock:
            core.batches.clear()
            core.logs.clear()
    try:
        cfg.save()
    except OSError as e:
        raise ApiError(500, f"Couldn't save settings: {e}") from None
    return {"config": cfg.public()}


@route("POST", "/api/pick-folder")
def api_pick_folder(h):
    body = h.read_body()
    return pick_folder(body.get("initial") or h.core.cfg.output_dir)


@route("POST", "/api/parse")
def api_parse(h):
    return paste.parse_master(str(h.read_body().get("text") or ""))


@route("GET", "/api/batches")
def api_batches(h):
    return h.core.summaries()


@route("POST", "/api/batches")
def api_create(h):
    return h.core.create(h.read_body())


@route("GET", f"/api/batches/{NAME}")
def api_batch(h, name):
    return h.core.detail(h.core.get_batch(name))


@route("POST", f"/api/batches/{NAME}/run")
def api_run(h, name):
    return h.core.run(name, h.read_body())


@route("POST", f"/api/batches/{NAME}/stop")
def api_stop(h, name):
    return h.core.stop(name)


@route("POST", f"/api/batches/{NAME}/clip")
def api_edit_clip(h, name):
    return h.core.edit_clip(name, h.read_body())


@route("POST", f"/api/batches/{NAME}/attach")
def api_attach(h, name):
    return h.core.attach(name, h.read_body())


@route("POST", f"/api/batches/{NAME}/anchor")
def api_anchor(h, name):
    return h.core.anchor(name, h.read_body())


@route("POST", f"/api/batches/{NAME}/references")
def api_batch_references(h, name):
    return h.core.batch_references(name, h.read_body())


@route("POST", f"/api/batches/{NAME}/delete")
def api_delete_batch(h, name):
    h.read_body()
    return h.core.delete_batch(name)


@route("GET", f"/api/batches/{NAME}/master")
def api_master_prompt(h, name):
    b = h.core.get_batch(name)
    notes = []
    if b.anchor_frame():
        notes.append(f"Frame {b.anchor_frame()} is this batch's anchor frame. There is no line for "
                     "that in a master prompt, so set it again after pasting.")
    if any(r.get("file") for r in (b.job.get("references") or [])):
        notes.append("Pictures you added in the app are written as full paths into this batch's folder, "
                     "so they only work while that folder is still there.")
    return {"name": b.name, "text": b.master_prompt(), "notes": notes}


@route("GET", "/api/storage")
def api_storage(h):
    return h.core.storage()


@route("POST", "/api/storage/archive")
def api_storage_archive(h):
    body = h.read_body()
    return h.core.archive(body.get("names") or [], restore=bool(body.get("restore")))


@route("POST", f"/api/batches/{NAME}/open")
def api_open(h, name):
    body = h.read_body()
    b = h.core.get_batch(name)
    target = body.get("target")
    try:
        if target == "folder":
            open_path(b.root)
        elif target in ("video", "reveal", "frame"):
            clip = str(body.get("clip") or "")
            path = b.video_path(clip) if target != "frame" else b.frame_path(clip)
            if not b.clip(clip) or not path.exists():
                raise ApiError(404, "That file isn't there yet.")
            (reveal_path if target == "reveal" else open_path)(path)
        else:
            raise ApiError(400, "Unknown target")
    except OSError as e:  # e.g. nothing is set up to play .mp4 files
        raise ApiError(500, f"Windows couldn't open it: {e.strerror or e}") from None
    return {"ok": True}


@route("GET", f"/api/batches/{NAME}/frame/{CLIP}")
def api_batch_frame(h, name, clip):
    b = h.core.get_batch(name)
    if not b.clip(clip):
        raise ApiError(404, "Not found")
    h.send_file(safe_child(b.frames_dir, b.frame_path(clip).name), cache="no-cache")


@route("GET", f"/api/batches/{NAME}/video/{CLIP}")
def api_batch_video(h, name, clip):
    h.send_file(safe_child(h.core.get_batch(name).videos_dir, f"{clip}.mp4"), cache="no-cache")


@route("GET", f"/api/batches/{NAME}/ref/(.+)")
def api_batch_ref(h, name, rel):
    h.send_file(safe_child(h.core.get_batch(name).refs_dir, rel))


@route("POST", "/api/open-output")
def api_open_output(h):
    try:
        h.core.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        open_path(h.core.cfg.output_dir)
    except OSError as e:
        raise ApiError(500, f"Windows couldn't open the output folder: {e.strerror or e}") from None
    return {"ok": True}


# ------------------------------------------------------------------ server + window

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if not isinstance(err, (ConnectionError, TimeoutError)):  # the window closing drops connections
            write_error_log(f"request from {client_address}\n{traceback.format_exc()}")


def create_server(cfg, port=0):
    server = Server(("127.0.0.1", port), Handler)
    server.core = Core(cfg)
    server.core.shutdown = server.shutdown
    server.token = secrets.token_urlsafe(24)
    p = server.server_address[1]
    server.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
    return server


def find_app_browser():
    env = os.environ
    for base in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")):
        if not base:
            continue
        for rel in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
            if (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    for mac in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        if Path(mac).is_file():
            return mac
    return next((p for p in map(shutil.which, ("msedge", "chrome", "google-chrome", "chromium")) if p), None)


def open_window(url):
    browser = find_app_browser()
    if browser:
        try:
            return subprocess.Popen([
                browser, f"--app={url}", f"--user-data-dir={config_dir() / 'window'}",
                "--window-size=1480,940", "--no-first-run", "--no-default-browser-check",
                "--disable-sync", "--disable-extensions",
            ])
        except OSError:
            pass
    webbrowser.open(url)
    return None


def running_instance_url():
    info_path = config_dir() / "instance.json"
    try:
        info = pl.read_json(info_path, None)
        port, token = int(info["port"]), str(info["token"])
        req = Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-PK-Token": token})
        with urlopen(req, timeout=1.5) as r:
            data = json.loads(r.read().decode())
        # an older copy that's still running must not capture the launch of a newer one
        if data.get("ok") and data.get("version") == APP_VERSION:
            return f"http://127.0.0.1:{port}/"
    except Exception:
        return None
    return None


def collect_watch(core, first_delay=4):
    """Once, shortly after launch: pick up anything kie.ai already finished."""
    time.sleep(first_delay)
    try:
        core.resume_checks()
    except Exception:
        write_error_log(f"resume_checks\n{traceback.format_exc()}")


def update_watch(core, first_delay=3, every=6 * 3600):
    """Ask the manifest at startup, then every few hours. Installs nothing."""
    time.sleep(first_delay)
    while True:
        if core.cfg.auto_update_check:
            try:
                core.check_update()
            except Exception:
                pass
        time.sleep(every)


def monitor(server, proc):
    """shut down once every window is closed and no batch is working"""
    core = server.core
    launched = time.time()
    proc_closed_at = None
    while True:
        time.sleep(2)
        now = time.time()
        if proc is not None and proc_closed_at is None and proc.poll() is not None:
            # a quick exit means the browser handed the window to an existing process
            proc_closed_at = now if now - launched > 10 else -1
        window_gone = (
            (proc_closed_at not in (None, -1) and core.last_ping < proc_closed_at + 1)
            or (core.bye_at > core.last_ping and now - core.bye_at > 5)
            or now - core.last_ping > IDLE_LIMIT
        )
        if window_gone and not core.active_runs():
            server.shutdown()
            return


def main(argv=None):
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--no-window", action="store_true", help="start the server only and print its address")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--pick-folder", help=argparse.SUPPRESS)       # internal: folder picker child process
    ap.add_argument("--pick-folder-out", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.pick_folder_out:
        run_folder_picker(args.pick_folder, args.pick_folder_out)
        return

    if not args.no_window:
        existing = running_instance_url()
        if existing:
            open_window(existing)
            return

    cfg = Config()
    server = create_server(cfg, port=args.port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    info_path = config_dir() / "instance.json"
    if args.no_window:
        print(url, flush=True)
    else:
        try:
            pl.write_json(info_path, {"port": port, "token": server.token, "pid": os.getpid()})
        except OSError:
            pass  # without it, opening the app again just starts a second copy
        proc = open_window(url)
        server.core.window_proc = proc
        threading.Thread(target=monitor, args=(server, proc), daemon=True).start()
        threading.Thread(target=update_watch, args=(server.core,), daemon=True).start()
        threading.Thread(target=collect_watch, args=(server.core,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            if not args.no_window and (pl.read_json(info_path, {}) or {}).get("pid") == os.getpid():
                info_path.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
