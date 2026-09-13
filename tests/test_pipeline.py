"""Offline tests for the v3 engine. Nothing here talks to kie.ai.

Run from the v3 folder:  python -X utf8 -m unittest discover -s tests -v
"""

import json
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline as pl  # noqa: E402


def png_bytes(w=4, h=4, rgb=(200, 80, 40)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeKie:
    """stands in for upload_image / submit_image / submit_video / check_task / download_file"""

    def __init__(self):
        self.uploads, self.images, self.videos, self.checks = [], [], [], []
        self.results = {}          # prompt -> list of check_task results, last one repeats
        self.submit_errors = {}    # prompt -> exception raised once
        self.hold = False          # True: everything stays "rendering", like a real slow job
        self.charges = None        # credits kie.ai reports per task, as it really does

    def upload_image(self, path, key, batch):
        self.uploads.append(path.name)
        return f"https://files.kie/{batch}/{path.name}"

    def _fail_if_asked(self, prompt):
        if prompt in self.submit_errors:
            raise self.submit_errors.pop(prompt)

    def submit_image(self, prompt, image_settings, reference_urls, key):
        self._fail_if_asked(prompt)
        self.images.append({"prompt": prompt, "settings": dict(image_settings), "refs": list(reference_urls)})
        return f"img{len(self.images)}"

    def submit_video(self, image_url, prompt, st, key):
        self._fail_if_asked(prompt)
        self.videos.append({"prompt": prompt, "image_url": image_url, "settings": dict(st)})
        return f"vid{len(self.videos)}"

    def check_task(self, task_id, key, suffixes=pl.VIDEO_SUFFIXES):
        self.checks.append(task_id)
        if self.hold:
            return "rendering", None, {"state": "rendering"}
        if task_id.startswith("img"):
            prompt = self.images[int(task_id[3:]) - 1]["prompt"]
            default = ("done", f"https://cdn.kie/{task_id}.png")
        else:
            prompt = self.videos[int(task_id[3:]) - 1]["prompt"]
            default = ("done", f"https://cdn.kie/{task_id}.mp4")
        seq = self.results.get(prompt) or [default]
        result = seq.pop(0) if len(seq) > 1 else seq[0]
        raw = {"state": result[0]}
        if self.charges is not None and result[0] in ("done", "failed"):
            raw["creditsConsumed"] = self.charges
        return result[0], result[1], raw

    def download_file(self, url, dest, progress=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png_bytes() if url.endswith(".png") else b"mp4:" + url.encode())
        return dest.stat().st_size


class EngineTest(unittest.TestCase):
    CLIPS = [
        {"image": "a diya on a table", "motion": "flame flickers"},
        {"image": "a cup of chai", "motion": "steam rises"},
        {"image": "oil in a palm", "motion": "oil streams", "ref": {"kind": "frame", "index": 1}},
    ]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ks_test_"))
        self.kie = FakeKie()
        self.patches = [mock.patch.object(pl, n, getattr(self.kie, n)) for n in
                        ("upload_image", "submit_image", "submit_video", "check_task", "download_file")]
        self.patches += [mock.patch.object(pl, "POLL_EVERY", 0.01), mock.patch.object(pl, "SUBMIT_GAP", 0)]
        for p in self.patches:
            p.start()
        self.batch = pl.Batch.create(self.tmp / "out", "c150", self.CLIPS,
                                     {"duration": "5"}, {"resolution": "2K"})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_stage(self, stage, redo_frames=(), redo_videos=()):
        plan = self.batch.plan(redo_frames=redo_frames, redo_videos=redo_videos)
        events = []
        pl.Runner(self.batch, "key", stage, plan[f"{stage}_make"], plan[f"{stage}_check"],
                  lambda k, **d: events.append((k, d))).run()
        return plan, events

    # ---- batch shape

    def test_create_writes_clips_and_names(self):
        job = pl.read_json(self.batch.root / "job.json")
        self.assertEqual([c["name"] for c in job["clips"]], ["c150_clip01", "c150_clip02", "c150_clip03"])
        self.assertEqual(job["settings"]["duration"], "5")
        self.assertEqual(job["image_settings"], {"aspect_ratio": "9:16", "resolution": "2K"})
        with self.assertRaises(FileExistsError):
            pl.Batch.create(self.tmp / "out", "c150", self.CLIPS, {}, {})
        with self.assertRaises(ValueError):
            pl.Batch.create(self.tmp / "out", "bad name", self.CLIPS, {}, {})

    # ---- stage 1

    def test_frames_then_videos(self):
        plan, events = self.run_stage("frames")
        self.assertEqual(plan["frames_make"], ["c150_clip01", "c150_clip02", "c150_clip03"])
        self.assertEqual(plan["videos_make"], [])   # nothing to animate before the frames exist
        self.assertEqual(events[-1], ("finished", {"cancelled": False, "error": None}))
        self.assertTrue(all(self.batch.frame_ready(c["name"]) for c in self.batch.clips()))
        self.assertEqual([i["prompt"] for i in self.kie.images],
                         ["a diya on a table", "a cup of chai", "oil in a palm"])
        self.assertEqual(self.kie.images[0]["settings"]["resolution"], "2K")

        # clip 3 asked for frame 1, so that frame was uploaded as its reference
        self.assertEqual(self.kie.images[2]["refs"], ["https://files.kie/c150/c150_clip01.png"])
        self.assertEqual(self.kie.images[0]["refs"], [])

        plan, events = self.run_stage("videos")
        self.assertEqual(plan["videos_make"], ["c150_clip01", "c150_clip02", "c150_clip03"])
        self.assertTrue(all(self.batch.video_ready(c["name"]) for c in self.batch.clips()))
        self.assertEqual(self.kie.videos[0]["image_url"], "https://files.kie/c150/c150_clip01.png")
        self.assertEqual(self.kie.videos[0]["prompt"], "flame flickers")
        self.assertEqual(self.batch.counts(), {"total": 3, "frames": 3, "videos": 3,
                                               "failed": 0, "active": 0, "blocked": 0})

    def test_frame_referencing_another_clip_runs_after_it(self):
        order = self.batch.plan()["frames_make"]
        runner = pl.Runner(self.batch, "key", "frames", order, [], lambda *a, **k: None)
        self.assertEqual(runner._order(["c150_clip03", "c150_clip01"]), ["c150_clip01", "c150_clip03"])

    def test_missing_reference_blocks_instead_of_guessing(self):
        b = pl.Batch.create(self.tmp / "out", "c151", [{"image": "a face", "motion": "smiles",
                                                        "ref": {"kind": "needed", "note": "the avatar"}}], {}, {})
        plan = b.plan()
        self.assertEqual((plan["blocked"], plan["frames_make"]), (["c151_clip01"], []))
        self.assertEqual(b.counts()["blocked"], 1)

        ref = b.refs_dir / "avatar.png"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(png_bytes())
        b.job["clips"][0]["ref"] = {"kind": "file", "file": "avatar.png"}
        b.save_job()
        self.assertEqual(b.plan()["frames_make"], ["c151_clip01"])
        pl.Runner(b, "key", "frames", ["c151_clip01"], [], lambda *a, **k: None).run()
        self.assertEqual(self.kie.images[-1]["refs"], ["https://files.kie/c151/avatar.png"])

    def test_clip_without_an_image_prompt_waits_for_a_dropped_picture(self):
        b = pl.Batch.create(self.tmp / "out", "c152", [{"image": "", "motion": "pans left"}], {}, {})
        self.assertEqual(b.plan()["needs_frame"], ["c152_clip01"])
        dropped = b.frames_dir / "c152_clip01.jpg"
        dropped.parent.mkdir(parents=True, exist_ok=True)
        dropped.write_bytes(png_bytes())
        b.update("c152_clip01", "frame", status="done", file="c152_clip01.jpg", source="dropped")
        self.assertTrue(b.frame_ready("c152_clip01"))
        self.assertEqual(b.plan()["videos_make"], ["c152_clip01"])

    # ---- failures

    def test_kie_failure_keeps_the_clip_for_regeneration(self):
        self.kie.results["a cup of chai"] = [("failed", "content policy")]
        self.run_stage("frames")
        st = self.batch.clip_state("c150_clip02")["frame"]
        self.assertEqual((st["status"], st["failed_by"]), ("failed", "kie"))
        self.assertTrue((self.batch.root / "responses" / "c150_clip02.frames.json").exists())
        plan = self.batch.plan()
        self.assertEqual(plan["frames_failed"], ["c150_clip02"])
        self.assertEqual(plan["frames_make"], [])            # not resent until the user asks
        self.assertEqual(self.batch.plan(redo_frames={"c150_clip02"})["frames_make"], ["c150_clip02"])

    def test_done_without_a_file_link_is_reported(self):
        self.kie.results["a diya on a table"] = [("no_url", None)]
        self.run_stage("frames")
        st = self.batch.clip_state("c150_clip01")["frame"]
        self.assertEqual((st["status"], st["stage"]), ("failed", "check"))
        self.assertIn("no file link", st["error"])

    def test_no_credits_stops_the_run(self):
        self.kie.submit_errors["a cup of chai"] = RuntimeError("createTask 402 insufficient credits")
        _, events = self.run_stage("frames")
        self.assertIn("credits", events[-1][1]["error"])
        self.assertTrue(self.batch.frame_ready("c150_clip01"))
        self.assertEqual(self.batch.clip_state("c150_clip03")["frame"]["status"], "pending")
        self.assertEqual(len(self.kie.images), 1)

    def test_cancel_sends_nothing(self):
        runner = pl.Runner(self.batch, "key", "frames", self.batch.plan()["frames_make"], [], lambda *a, **k: None)
        runner.cancel()
        runner.run()
        self.assertEqual(self.kie.images, [])


class ResultUrlTests(unittest.TestCase):
    def test_picks_the_right_kind_of_file(self):
        data = {"resultJson": json.dumps({"resultUrls": ["https://cdn/x.png"]})}
        self.assertEqual(pl.find_result_url(json.loads(json.dumps(data)), pl.IMAGE_SUFFIXES), "https://cdn/x.png")
        self.assertIsNone(pl.find_result_url(json.loads(json.dumps(data)), pl.VIDEO_SUFFIXES))
        video = {"response": {"videos": [{"url": "https://cdn/y.mp4?token=1"}]}}
        self.assertEqual(pl.find_result_url(video, pl.VIDEO_SUFFIXES), "https://cdn/y.mp4?token=1")

    def test_error_code_is_not_treated_as_rendering(self):
        with mock.patch.object(pl, "_req", return_value={"code": 401, "msg": "Unauthorized"}):
            with self.assertRaisesRegex(RuntimeError, "recordInfo 401"):
                pl.check_task("t1", "key")


if __name__ == "__main__":
    unittest.main()
