"""
Engine for Kling Studio v3. No UI code in here.

Every clip goes through two paid stages on kie.ai:

  1. frame  - nano-banana-2 draws the still from the clip's image prompt
              (optionally with reference images), saved to frames/<clip>.png
  2. video  - kling-3.0 animates that frame with the clip's motion prompt,
              saved to videos/<clip>.mp4

The kie.ai calls keep the shapes from their docs: createTask + recordInfo, with
the result URL found by walking the response rather than assuming a field name.
A frame can also come from a picture the user drops in, which skips stage 1.
"""

import base64
import json
import mimetypes
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

KIE_API = "https://api.kie.ai/api/v1"
CREATE_URL = f"{KIE_API}/jobs/createTask"
DETAIL_URL = f"{KIE_API}/jobs/recordInfo"
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"
VIDEO_MODEL = "kling-3.0/video"
IMAGE_MODEL = "nano-banana-2"

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIXES = (".mp4",)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
POLL_EVERY = 20
POLL_TIMEOUT = 1800
SUBMIT_GAP = 1.5
MAX_CHECK_ERRORS = 5
DOWNLOAD_STALL = 40       # seconds without data before a download is retried
DOWNLOAD_ATTEMPTS = 4
RETRY_DELAY = 2
MAX_REFERENCES = 14       # nano-banana-2 accepts up to 14 image inputs

DEFAULT_SETTINGS = {"aspect_ratio": "9:16", "duration": "5", "mode": "pro", "sound": False}
DEFAULT_IMAGE_SETTINGS = {"aspect_ratio": "9:16", "resolution": "2K"}
BATCH_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


# ------------------------------------------------------------------ kie.ai

def natural_key(s):
    """shot2.png sorts before shot10.png"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def _req(url, method="GET", payload=None, key=None, timeout=120):
    headers = {"Content-Type": "application/json", "User-Agent": "kling-studio/3.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps(payload).encode() if payload is not None else None
    r = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} :: {e.read().decode(errors='replace')[:300]}") from None
    except URLError as e:
        raise RuntimeError(f"network :: {e.reason}") from None


def upload_image(path: Path, key, batch):
    """put a local picture on kie.ai's file store and return its public URL"""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    res = _req(UPLOAD_URL, "POST", {
        "base64Data": f"data:{mime};base64,{b64}",
        "uploadPath": f"images/{batch}",
        "fileName": path.name,
    }, key)
    url = (res.get("data") or {}).get("downloadUrl")
    if not url:
        raise RuntimeError(f"upload failed :: {json.dumps(res)[:300]}")
    return url


def _task_id(res, what):
    if res.get("code") != 200:
        raise RuntimeError(f"{what} {res.get('code')} {res.get('msg')}")
    tid = (res.get("data") or {}).get("taskId")
    if not tid:
        raise RuntimeError(f"no taskId :: {json.dumps(res)[:300]}")
    return tid


def submit_image(prompt, image_settings, reference_urls, key):
    """nano-banana-2 text-to-image, or image-to-image when references are given"""
    res = _req(CREATE_URL, "POST", {
        "model": IMAGE_MODEL,
        "input": {
            "prompt": prompt,
            "image_input": list(reference_urls or [])[:MAX_REFERENCES],
            "aspect_ratio": image_settings.get("aspect_ratio", "9:16"),
            "resolution": image_settings.get("resolution", "2K"),
            "output_format": "png",
        },
    }, key)
    return _task_id(res, "createTask")


def submit_video(image_url, prompt, st, key):
    res = _req(CREATE_URL, "POST", {
        "model": VIDEO_MODEL,
        "input": {
            "prompt": prompt,
            "image_urls": [image_url],
            "duration": str(st.get("duration", "5")),
            "aspect_ratio": st.get("aspect_ratio", "9:16"),
            "mode": st.get("mode", "pro"),
            "sound": bool(st.get("sound", False)),
            "multi_shots": False,
        },
    }, key)
    return _task_id(res, "createTask")


def find_result_url(obj, suffixes):
    """field names vary by model - walk the response instead of assuming"""
    hits = []

    def unwrap(o):
        if isinstance(o, dict):
            for k in list(o):
                if isinstance(o[k], str) and k in ("resultJson", "result", "response"):
                    try:
                        o[k] = json.loads(o[k])
                    except Exception:
                        pass
                unwrap(o[k])
        elif isinstance(o, list):
            for v in o:
                unwrap(v)

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o.startswith("http"):
            hits.append(o)

    unwrap(obj)
    walk(obj)
    for u in hits:
        clean = u.split("?")[0].lower()
        if clean.endswith(tuple(suffixes)) or any(s in clean for s in suffixes):
            return u
    return None


def check_task(task_id, key, suffixes=VIDEO_SUFFIXES):
    """One status check.

    Returns (status, value, raw) where status is "done" (value = result url),
    "failed" (value = message), "no_url" (done but no file found) or "rendering".
    """
    res = _req(f"{DETAIL_URL}?taskId={task_id}", "GET", None, key)
    if res.get("code") not in (None, 200):  # e.g. 401 bad key: an error, not "still rendering"
        raise RuntimeError(f"recordInfo {res.get('code')} {res.get('msg')}")
    d = res.get("data") or {}
    raw = json.loads(json.dumps(d))  # find_result_url decodes resultJson in place
    flag = str(d.get("successFlag", d.get("state", ""))).lower()
    if flag in ("1", "success", "completed", "done"):
        u = find_result_url(d, suffixes)
        return ("done", u, raw) if u else ("no_url", None, raw)
    if flag in ("2", "3", "fail", "failed", "error"):
        return "failed", d.get("errorMessage") or json.dumps(d)[:250], raw
    return "rendering", None, raw


# ------------------------------------------------------------------ files

def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def replace_path(src: Path, dst: Path, attempts=10):
    """src.replace(dst), retrying while Windows antivirus or an indexer holds a new file"""
    for attempt in range(attempts):
        try:
            return src.replace(dst)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.3)


def write_json(path: Path, data):
    """write to a temp file then swap it in, so a crash never leaves half a file"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    replace_path(tmp, path)


def download_file(url, dest: Path, progress=None):
    """Stream url to dest, calling progress(bytes, total, attempt) as data arrives.

    A connection that stalls for DOWNLOAD_STALL seconds or closes early is retried
    from scratch; HTTP errors (expired link, 403, 404) are not.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            r = Request(url, headers={"User-Agent": "kling-studio/3.0"})
            with urlopen(r, timeout=DOWNLOAD_STALL) as resp, open(dest, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0) or None
                done = 0
                if progress:
                    progress(0, total, attempt)
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, attempt)
            if total and done < total:
                raise OSError(f"connection closed after {done} of {total} bytes")
            return done
        except HTTPError:
            raise
        except (URLError, OSError):
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            if progress:
                progress(0, None, attempt + 1)
            time.sleep(RETRY_DELAY)


def credits_per_video(settings):
    """v1's estimate: 18 credits per second in pro mode; None for other modes"""
    if settings.get("mode", "pro") != "pro":
        return None
    try:
        return 18 * int(settings.get("duration", 5))
    except (TypeError, ValueError):
        return None


def default_names(batch, count):
    return [f"{batch}_clip{i:02d}" for i in range(1, count + 1)]


# ------------------------------------------------------------------ batches

class Batch:
    """one batch folder: job.json, state.json, frames/, videos/, refs/, responses/"""

    def __init__(self, root: Path):
        self.root = root
        self.job = read_json(root / "job.json", {})
        self.state = read_json(root / "state.json", {})
        self._lock = threading.Lock()

    # ---- layout

    @property
    def name(self):
        return self.job.get("batch", self.root.name)

    @property
    def frames_dir(self):
        return self.root / "frames"

    @property
    def videos_dir(self):
        return self.root / "videos"

    @property
    def refs_dir(self):
        return self.root / "refs"

    def video_path(self, name):
        return self.videos_dir / f"{name}.mp4"

    def frame_path(self, name):
        """where this clip's still lives; the extension follows whatever was saved"""
        saved = (self.clip_state(name)["frame"] or {}).get("file")
        return self.frames_dir / (saved or f"{name}.png")

    # ---- creation

    @classmethod
    def create(cls, output_dir: Path, name, clips, settings, image_settings, references=()):
        if not BATCH_NAME_RE.match(name or ""):
            raise ValueError("Batch name can only use letters, numbers, _ and -")
        if not clips:
            raise ValueError("A batch needs at least one clip")
        root = output_dir / name
        if root.exists():
            raise FileExistsError(f"A batch named \"{name}\" already exists. Pick another name.")
        staging = output_dir / f".{name}.creating"
        shutil.rmtree(staging, ignore_errors=True)
        names = default_names(name, len(clips))
        (staging / "frames").mkdir(parents=True, exist_ok=True)
        write_json(staging / "job.json", {
            "batch": name,
            "settings": {**DEFAULT_SETTINGS, **(settings or {})},
            "image_settings": {**DEFAULT_IMAGE_SETTINGS, **(image_settings or {})},
            "references": list(references or []),
            "clips": [{"name": n, "image": c.get("image", ""), "motion": c.get("motion", ""),
                       "ref": c.get("ref")} for n, c in zip(names, clips)],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": 3,
        })
        replace_path(staging, root)
        return cls(root)

    def save_job(self):
        write_json(self.root / "job.json", self.job)

    # ---- clips and their state

    def clips(self):
        return list(self.job.get("clips") or [])

    def anchor(self):
        """the character portrait this batch locks onto, if any"""
        a = self.job.get("anchor")
        return dict(a) if isinstance(a, dict) else {}

    def anchor_file(self):
        """the locked anchor's path, or None when there isn't one or it isn't locked"""
        a = self.anchor()
        if not a.get("locked") or not a.get("file"):
            return None
        path = self.refs_dir / a["file"]
        return path if path.is_file() else None

    def set_anchor(self, **kw):
        a = self.anchor()
        a.update(kw)
        self.job["anchor"] = a
        self.save_job()
        return a

    def clip(self, name):
        return next((c for c in self.clips() if c["name"] == name), None)

    def clip_index(self, name):
        return next((i for i, c in enumerate(self.clips()) if c["name"] == name), None)

    def clip_state(self, name):
        st = self.state.get(name) or {}
        return {"frame": dict(st.get("frame") or {"status": "pending"}),
                "video": dict(st.get("video") or {"status": "pending"})}

    def update(self, name, part_name, **kw):
        """part_name is "frame" or "video"; None values clear the key"""
        with self._lock:
            st = self.state.setdefault(name, {})
            part = st.setdefault(part_name, {"status": "pending"})
            for k, v in kw.items():
                if v is None:
                    part.pop(k, None)
                else:
                    part[k] = v
            write_json(self.root / "state.json", self.state)
            return dict(part)

    def snapshot(self):
        """copy of every clip's state, safe to read while a runner is writing"""
        with self._lock:
            return json.loads(json.dumps(self.state))

    def save_response(self, name, stage, raw):
        write_json(self.root / "responses" / f"{name}.{stage}.json", raw)

    # ---- what is finished

    def frame_ready(self, name):
        st = self.clip_state(name)["frame"]
        return st.get("status") == "done" and self.frame_path(name).exists()

    def video_ready(self, name):
        return self.clip_state(name)["video"].get("status") == "done" and self.video_path(name).exists()

    def reference_urls_needed(self, name):
        """what this clip's ref asks for: ("frame", index) / ("file", path) / ("ask", note) / None"""
        clip = self.clip(name) or {}
        ref = clip.get("ref")
        if not ref:
            return None
        if ref.get("kind") == "frame":
            return ("frame", int(ref.get("index", 0)))
        if ref.get("kind") == "path" and Path(ref["path"]).is_file():
            return ("file", ref["path"])
        if ref.get("kind") == "file":  # attached in the app, stored under refs/
            p = self.refs_dir / ref["file"]
            return ("file", str(p)) if p.is_file() else ("ask", ref.get("note", ""))
        return ("ask", ref.get("note", ""))

    def waiting_for_reference(self, name):
        need = self.reference_urls_needed(name)
        return bool(need and need[0] == "ask")

    def plan(self, redo_frames=(), redo_videos=()):
        """what a run would do, per stage.

        frames/videos in "make" cost credits; "check" only asks kie.ai for status.
        "blocked" clips are waiting for a reference image the user must attach.
        """
        out = {"frames_make": [], "frames_check": [], "videos_make": [], "videos_check": [],
               "blocked": [], "needs_frame": [], "frames_failed": [], "videos_failed": []}

        def sort_failure(part, make, check, failed):
            """a model failure needs the user's go-ahead; a timeout or download can just be re-checked"""
            if part.get("failed_by") == "kie":
                out[failed].append(n)
            elif part.get("task_id"):
                out[check].append(n)
            else:
                out[make].append(n)

        for c in self.clips():
            n = c["name"]
            st = self.clip_state(n)
            frame, video = st["frame"], st["video"]

            if n in redo_frames:
                out["frames_make"].append(n)
            elif self.frame_ready(n):
                pass
            elif self.waiting_for_reference(n):
                out["blocked"].append(n)
            elif not c.get("image"):
                out["needs_frame"].append(n)          # no image prompt: drop a picture instead
            elif frame.get("status") == "failed":
                sort_failure(frame, "frames_make", "frames_check", "frames_failed")
            elif frame.get("task_id"):
                out["frames_check"].append(n)
            else:
                out["frames_make"].append(n)

            if not self.frame_ready(n):               # nothing to animate yet
                continue
            if n in redo_videos:
                out["videos_make"].append(n)
            elif self.video_ready(n) or not c.get("motion"):
                pass
            elif video.get("status") == "failed":
                sort_failure(video, "videos_make", "videos_check", "videos_failed")
            elif video.get("task_id"):
                out["videos_check"].append(n)
            else:
                out["videos_make"].append(n)
        return out

    def counts(self):
        c = {"total": 0, "frames": 0, "videos": 0, "failed": 0, "active": 0, "blocked": 0}
        active = ("uploading", "generating", "rendering", "downloading")
        for clip in self.clips():
            n = clip["name"]
            st = self.clip_state(n)
            c["total"] += 1
            if self.frame_ready(n):
                c["frames"] += 1
            if self.video_ready(n):
                c["videos"] += 1
            if st["frame"].get("status") in active or st["video"].get("status") in active:
                c["active"] += 1
            elif st["frame"].get("status") == "failed" or st["video"].get("status") == "failed":
                c["failed"] += 1
            elif self.waiting_for_reference(n):
                c["blocked"] += 1
        return c


def list_batches(output_dir: Path):
    if not output_dir.is_dir():
        return []
    out = []
    for d in output_dir.iterdir():
        if d.is_dir() and not d.name.startswith((".", "_")) and (d / "job.json").exists():
            try:
                out.append(Batch(d))
            except (OSError, ValueError):
                continue
    return out


# ------------------------------------------------------------------ runner

FATAL_MESSAGES = {
    "401": "kie.ai rejected the API key. Check it in Settings.",
    "402": "Not enough kie.ai credits. Top up the kie.ai balance, then start it again.",
}
_FATAL_RE = re.compile(r'(?:^(?:createTask|recordInfo|HTTP) |"code":\s*)(401|402)\b')


class Runner(threading.Thread):
    """Runs one stage of one batch in the background.

    stage "frames": nano-banana-2 draws each still, with references if the clip asks.
    stage "videos": kling-3.0 animates each finished still.

    Reports through emit(kind, **data):
      emit("log", text=..., level="info"|"warn"|"error")
      emit("clip", name=..., stage=..., state={...})
      emit("finished", cancelled=bool, error=str|None)
    """

    def __init__(self, batch: Batch, api_key, stage, make, check, emit):
        super().__init__(daemon=True)
        self.batch = batch
        self.api_key = api_key
        self.stage = stage                              # "frames" or "videos"
        self.part = "frame" if stage == "frames" else "video"   # the half of a clip's state it writes
        self.make = list(make)
        self.check = list(check)
        self.emit = emit
        self._cancel = threading.Event()
        self._uploads = {}   # local path -> kie.ai url, so a reference uploads once per run
        self.error = None
        self.halt = False

    # ---- plumbing

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def _set(self, name, **kw):
        self.emit("clip", name=name, stage=self.stage, state=self.batch.update(name, self.part, **kw))

    def _log(self, text, level="info"):
        self.emit("log", text=text, level=level)

    def _fatal(self, err):
        """a bad key or an empty balance would fail every remaining clip the same way"""
        m = _FATAL_RE.search(str(err))
        if not m:
            return False
        if self.error != FATAL_MESSAGES[m.group(1)]:
            self.error = FATAL_MESSAGES[m.group(1)]
            self._log(self.error, "error")
        self.halt = self.halt or m.group(1) == "401"
        return True

    def _upload(self, path: Path):
        key = str(path)
        if key not in self._uploads:
            self._uploads[key] = upload_image(path, self.api_key, self.batch.name)
        return self._uploads[key]

    def _reference_ready(self, name):
        """a clip that copies another clip's frame can only be sent once that frame exists"""
        need = self.batch.reference_urls_needed(name)
        if not need or need[0] != "frame":
            return True
        clips = self.batch.clips()
        return 0 < need[1] <= len(clips) and self.batch.frame_ready(clips[need[1] - 1]["name"])

    def run(self):
        try:
            pending = self._order(self.make)
            while True:
                ready = [n for n in pending if self._reference_ready(n)]
                waiting = [n for n in pending if n not in ready]
                if not ready and waiting and not self.check:
                    for n in waiting:  # the frame they copy from never arrived
                        self._set(n, status="failed", stage="reference",
                                  error="The frame this clip copies from isn't ready.")
                    break
                self._make_all(ready)
                if self.halt or self.cancelled:
                    break
                self._wait_all()
                # self.error means a bad key or an empty balance: collect what was paid for, send nothing more
                if not waiting or self.halt or self.cancelled or self.error:
                    break
                pending = waiting     # their reference should exist now
        except Exception as e:  # never let the thread die silently
            self.error = f"Run stopped: {e}"
            self._log(self.error, "error")
        finally:
            self.emit("finished", cancelled=self.cancelled, error=self.error)

    # ---- stage 1: send the work

    def _reference_urls(self, name):
        """upload whatever this clip's reference points at; [] when it has none"""
        b = self.batch
        urls = []
        anchor = b.anchor_file()          # the locked character, on every frame in the batch
        if anchor:
            urls.append(self._upload(anchor))
        for ref in b.job.get("references") or []:
            path = Path(ref["path"]) if ref.get("path") else (b.refs_dir / ref["file"] if ref.get("file") else None)
            if path and path.is_file():
                urls.append(self._upload(path))
        need = b.reference_urls_needed(name)
        if need and need[0] == "file":
            urls.append(self._upload(Path(need[1])))
        elif need and need[0] == "frame":
            other = b.clips()[need[1] - 1]["name"] if 0 < need[1] <= len(b.clips()) else None
            if not other or not b.frame_ready(other):
                raise RuntimeError(f"needs frame {need[1]} first, and that frame isn't ready")
            urls.append(self._upload(b.frame_path(other)))
        return urls

    def _order(self, names):
        """clips that reference another clip's frame go last"""
        return sorted(names, key=lambda n: 1 if (self.batch.reference_urls_needed(n) or ("", ))[0] == "frame" else 0)

    def _make_all(self, names):
        b = self.batch
        todo = list(names)
        if todo:
            self._log(f"Sending {len(todo)} {self.stage[:-1]}(s) to kie.ai…")
        for i, name in enumerate(todo):
            if self.cancelled:
                return
            clip = b.clip(name)
            self._set(name, status="uploading", stage=None, error=None, task_id=None, failed_by=None)
            try:
                if self.stage == "frames":
                    urls = self._reference_urls(name)
                    tid = submit_image(clip["image"], b.job.get("image_settings", {}), urls, self.api_key)
                    extra = {"references_used": len(urls)}
                else:
                    urls = [self._upload(b.frame_path(name))]
                    tid = submit_video(urls[0], clip["motion"], b.job.get("settings", {}), self.api_key)
                    extra = {}
            except Exception as e:
                self._set(name, status="failed", stage="submit", error=str(e))
                self._log(f"{name}: {self.stage[:-1]} could not be sent: {e}", "error")
                if self._fatal(e):
                    return
                continue
            else:
                # kept out of the try: once kie.ai has the task (and the credits), a problem
                # saving state must never look like a failure that would send it again
                self.check.append(name)
                self._set(name, status="generating" if self.stage == "frames" else "rendering",
                          task_id=tid, submitted_at=time.time(), **extra)
                self._log(f"{name}: sent ({tid})")
            if i < len(todo) - 1 and self._cancel.wait(SUBMIT_GAP):
                return

    # ---- stage 2: collect the results

    def _wait_all(self):
        b = self.batch
        suffixes = IMAGE_SUFFIXES if self.stage == "frames" else VIDEO_SUFFIXES
        active = list(dict.fromkeys(self.check))
        self.check = []
        if not active or self.cancelled:
            return
        self._log(f"Waiting for {len(active)} {self.stage[:-1]}(s)…")
        started = {n: time.time() for n in active}
        errors = {}
        while active:
            for name in list(active):
                if self.cancelled:
                    return
                st = self.batch.clip_state(name)[self.part]
                try:
                    status, value, raw = check_task(st["task_id"], self.api_key, suffixes)
                    errors.pop(name, None)
                except Exception as e:
                    if self._fatal(e) and self.halt:
                        return  # task ids are kept; checking again works once the key is fixed
                    errors[name] = errors.get(name, 0) + 1
                    if errors[name] >= MAX_CHECK_ERRORS:
                        self._set(name, status="failed", stage="check", error=str(e))
                        self._log(f"{name}: status check keeps failing: {e}", "error")
                        active.remove(name)
                    else:
                        self._log(f"{name}: status check failed, trying again: {e}", "warn")
                    continue

                if status == "done":
                    active.remove(name)
                    self._download(name, value)
                elif status == "failed":
                    active.remove(name)
                    b.save_response(name, self.stage, raw)
                    self._set(name, status="failed", stage="model", failed_by="kie", error=str(value))
                    self._log(f"{name}: kie.ai failed it: {value}", "error")
                elif status == "no_url":
                    active.remove(name)
                    b.save_response(name, self.stage, raw)
                    self._set(name, status="failed", stage="check",
                              error="kie.ai says it is done but sent no file link. Raw response saved to "
                                    f"responses/{name}.{self.stage}.json")
                    self._log(f"{name}: done but no file link", "error")
                else:
                    if time.time() - started[name] > POLL_TIMEOUT:
                        active.remove(name)
                        self._set(name, status="failed", stage="timeout",
                                  error="Still going after 30 minutes. Checking again is free.")
                        self._log(f"{name}: timed out", "warn")
            if active and self._cancel.wait(POLL_EVERY if self.stage == "videos" else max(3, POLL_EVERY // 4)):
                return

    def _download(self, name, url):
        b = self.batch
        self._set(name, status="downloading", url=url)
        if self.stage == "frames":
            ext = Path(url.split("?")[0]).suffix.lower()
            dest = b.frames_dir / f"{name}{ext if ext in IMG_EXT else '.png'}"
        else:
            dest = b.video_path(name)
        part = dest.with_name(dest.name + ".part")
        try:
            size = download_file(url, part)
            replace_path(part, dest)
        except Exception as e:
            part.unlink(missing_ok=True)
            self._set(name, status="failed", stage="download", error=f"Download failed: {e}")
            self._log(f"{name}: download failed: {e}", "error")
            return
        extra = {"file": dest.name, "source": "generated"} if self.stage == "frames" else {}
        self._set(name, status="done", bytes=size, stage=None, error=None, failed_by=None,
                  finished_at=time.time(), **extra)
        self._log(f"{name}: {self.stage[:-1]} ready ({size / 1024:.0f} KB)")
