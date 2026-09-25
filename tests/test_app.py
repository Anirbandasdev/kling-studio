"""Offline tests for the v3 local server (fake kie.ai, no credits, no windows).

Run from the v3 folder:  python -X utf8 -m unittest discover -s tests -v
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import paste  # noqa: E402
import pipeline as pl  # noqa: E402
from test_pipeline import FakeKie, png_bytes  # noqa: E402

CLIPS = [{"image": "a diya on a table", "motion": "flame flickers"},
         {"image": "a cup of chai", "motion": "steam rises"}]


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ks_app_"))
        self.env = mock.patch.dict(os.environ, {"KLING_STUDIO_HOME": str(self.tmp / "home")})
        self.env.start()
        self.kie = FakeKie()
        self.patches = [mock.patch.object(pl, n, getattr(self.kie, n)) for n in
                        ("upload_image", "submit_image", "submit_video", "check_task", "download_file")]
        self.patches += [mock.patch.object(pl, "POLL_EVERY", 0.01), mock.patch.object(pl, "SUBMIT_GAP", 0)]
        for p in self.patches:
            p.start()

        cfg = app.Config()
        cfg.api_key = "secret-key-1234abcd"
        cfg.output_dir = self.tmp / "out"
        self.server = app.create_server(cfg)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.patches:
            p.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, body=None, token=True, headers=None):
        h = {"X-PK-Token": self.server.token} if token else {}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        try:
            with urlopen(Request(self.base + path, data=data, headers=h, method=method), timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def make(self, name="c150", clips=None):
        return self.call("POST", "/api/batches", {"name": name, "clips": clips or CLIPS,
                                                  "settings": {"duration": "5"},
                                                  "image_settings": {"resolution": "2K"}})

    def run_stage(self, name, stage, redo=()):
        status, d = self.call("POST", f"/api/batches/{name}/run", {"stage": stage, "redo": list(redo)})
        self.assertEqual(status, 200, d)
        end = time.time() + 15
        while time.time() < end:
            _, d = self.call("GET", f"/api/batches/{name}")
            if not d["running"]:
                return d
            time.sleep(0.05)
        self.fail("stage never finished")

    # ---- security

    def test_api_needs_token_and_local_host(self):
        self.assertEqual(self.call("GET", "/api/batches", token=False)[0], 403)
        self.assertEqual(self.call("GET", "/api/batches")[0], 200)
        self.assertEqual(self.call("GET", "/api/batches", headers={"Host": "evil.example"})[0], 403)

    def test_index_carries_the_token_but_never_the_key(self):
        status, html = self.call("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), html)
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertEqual((boot["app_name"], boot["config"]["key_hint"]), ("Kling Studio", "abcd"))
        self.assertNotIn("secret-key", json.dumps(boot))

    def test_files_cannot_escape_the_batch_folder(self):
        self.make()
        self.assertEqual(self.call("GET", "/api/batches/c150/ref/..%2F..%2Fjob.json")[0], 404)
        self.assertEqual(self.call("GET", "/api/batches/c150/frame/c150_clip01")[0], 404)  # not made yet

    def test_unread_bodies_do_not_break_the_connection(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            for path, headers in (("/api/bye", {"X-PK-Token": self.server.token}), ("/api/config", {})):
                conn.request("POST", path, body='{"padding": "xxxxxxxxxxxx"}',
                             headers={**headers, "Content-Type": "application/json"})
                conn.getresponse().read()
                conn.request("GET", "/api/batches", headers={"X-PK-Token": self.server.token})
                r = conn.getresponse()
                self.assertEqual((r.status, json.loads(r.read())), (200, []))
        finally:
            conn.close()

    # ---- the two stages

    def test_parse_then_create_then_both_stages(self):
        _, parsed = self.call("POST", "/api/parse", {"text": "batch: c150\n1.\nimage: a diya\nmotion: flickers"})
        self.assertEqual((parsed["name"], len(parsed["clips"])), ("c150", 1))

        status, d = self.make()
        self.assertEqual(status, 200, d)
        self.assertEqual(d["plan"]["frames_make"], ["c150_clip01", "c150_clip02"])
        self.assertEqual(d["plan"]["videos_make"], [])
        self.assertFalse(d["running"])

        d = self.run_stage("c150", "frames")
        self.assertEqual(d["counts"]["frames"], 2)
        self.assertEqual([c["frame"]["ready"] for c in d["clips"]], [True, True])
        self.assertEqual(d["plan"]["videos_make"], ["c150_clip01", "c150_clip02"])
        self.assertEqual(self.call("GET", "/api/batches/c150/frame/c150_clip01")[1][:4], b"\x89PNG")

        d = self.run_stage("c150", "videos")
        self.assertEqual(d["counts"]["videos"], 2)
        status, part = self.call("GET", "/api/batches/c150/video/c150_clip01", headers={"Range": "bytes=0-3"})
        self.assertEqual((status, part), (206, b"mp4:"))
        self.assertEqual(self.kie.videos[0]["prompt"], "flame flickers")

    def test_run_validation(self):
        self.make()
        self.assertEqual(self.call("POST", "/api/batches/c150/run", {"stage": "nope"})[0], 400)
        self.assertEqual(self.call("POST", "/api/batches/c150/run", {"stage": "videos"})[0], 400)  # no frames yet
        self.assertEqual(self.call("POST", "/api/batches/c150/run",
                                   {"stage": "frames", "redo": ["nope"]})[0], 400)
        self.assertEqual(self.make()[0], 409)
        self.assertEqual(self.call("POST", "/api/batches", {"name": "c151", "clips": []})[0], 400)

    def test_no_key_blocks_the_run(self):
        self.make()
        self.server.core.cfg.api_key = ""
        status, err = self.call("POST", "/api/batches/c150/run", {"stage": "frames"})
        self.assertEqual(status, 400)
        self.assertIn("API key", err["error"])

    def test_regenerate_one_frame_with_an_edited_prompt(self):
        self.make()
        self.run_stage("c150", "frames")
        status, d = self.call("POST", "/api/batches/c150/clip", {"clip": "c150_clip02", "image": "a glass of chai"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["clips"][1]["image"], "a glass of chai")
        d = self.run_stage("c150", "frames", redo=["c150_clip02"])
        self.assertEqual(self.kie.images[-1]["prompt"], "a glass of chai")
        self.assertEqual(len(self.kie.images), 3)      # only the one clip was redone
        self.assertEqual(d["counts"]["frames"], 2)

    # ---- pictures from the user

    def test_dropped_picture_becomes_the_frame(self):
        self.make()
        body = {"kind": "frame", "clip": "c150_clip01", "filename": "shot.PNG",
                "data": "data:image/png;base64," + base64.b64encode(png_bytes()).decode()}
        status, d = self.call("POST", "/api/batches/c150/attach", body)
        self.assertEqual(status, 200, d)
        self.assertEqual((d["clips"][0]["frame"]["ready"], d["clips"][0]["frame"]["source"]), (True, "dropped"))
        self.assertEqual(d["plan"]["frames_make"], ["c150_clip02"])
        self.assertEqual(d["plan"]["videos_make"], ["c150_clip01"])
        self.assertTrue((self.tmp / "out" / "c150" / "frames" / "c150_clip01.png").exists())

    def test_reference_popup_flow(self):
        clips = [{"image": "the avatar smiling", "motion": "she blinks",
                  "ref": {"kind": "needed", "note": "the avatar"}}]
        _, d = self.make("c152", clips)
        self.assertEqual((d["plan"]["blocked"], d["clips"][0]["needs_reference"]), (["c152_clip01"], True))
        self.assertEqual(d["clips"][0]["reference_note"], "the avatar")
        self.assertEqual(self.call("POST", "/api/batches/c152/run", {"stage": "frames"})[0], 400)

        body = {"kind": "reference", "clip": "c152_clip01", "filename": "avatar.png",
                "data": base64.b64encode(png_bytes()).decode()}
        _, d = self.call("POST", "/api/batches/c152/attach", body)
        self.assertEqual((d["clips"][0]["needs_reference"], d["plan"]["frames_make"]), (False, ["c152_clip01"]))
        self.run_stage("c152", "frames")
        self.assertEqual(self.kie.images[-1]["refs"], ["https://files.kie/c152/avatar.png"])

    def test_one_upload_can_cover_every_clip_that_needs_it(self):
        clips = [{"image": f"the avatar, shot {i}", "motion": "she blinks",
                  "ref": {"kind": "needed", "note": "the avatar"}} for i in range(1, 5)]
        _, d = self.make("c160", clips)
        self.assertEqual(len(d["plan"]["blocked"]), 4)

        body = {"kind": "reference", "clips": [c["name"] for c in d["clips"]],
                "filename": "avatar.png", "data": base64.b64encode(png_bytes()).decode()}
        _, d = self.call("POST", "/api/batches/c160/attach", body)
        self.assertEqual(d["plan"]["blocked"], [])
        self.assertEqual(len(d["plan"]["frames_make"]), 4)
        files = {c["ref"]["file"] for c in d["clips"]}
        self.assertEqual(len(files), 1)                                    # stored once
        self.assertEqual(len(list((self.tmp / "out" / "c160" / "refs").iterdir())), 1)

        self.run_stage("c160", "frames")
        self.assertEqual([len(i["refs"]) for i in self.kie.images], [1, 1, 1, 1])

    def test_a_picture_already_here_is_reused_without_uploading_again(self):
        clips = [{"image": "a", "motion": "a", "ref": {"kind": "needed", "note": "x"}},
                 {"image": "b", "motion": "b", "ref": {"kind": "needed", "note": "x"}}]
        _, d = self.make("c161", clips)
        first = {"kind": "reference", "clips": ["c161_clip01"], "filename": "look.png",
                 "data": base64.b64encode(png_bytes()).decode()}
        _, d = self.call("POST", "/api/batches/c161/attach", first)
        saved = d["clips"][0]["ref"]["file"]

        status, d = self.call("POST", "/api/batches/c161/attach",
                              {"kind": "reference", "clips": ["c161_clip02"], "file": saved})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["clips"][1]["ref"]["file"], saved)
        self.assertEqual(len(list((self.tmp / "out" / "c161" / "refs").iterdir())), 1)

        status, err = self.call("POST", "/api/batches/c161/attach",
                                {"kind": "reference", "clips": ["c161_clip02"], "file": "nope.png"})
        self.assertEqual(status, 404, err)

    def test_a_style_reference_goes_on_every_frame(self):
        self.make("c162")
        body = {"kind": "reference", "filename": "style.png",
                "data": base64.b64encode(png_bytes()).decode()}
        _, d = self.call("POST", "/api/batches/c162/attach", body)          # no clip: whole batch
        self.assertEqual(len(d["references"]), 1)
        self.run_stage("c162", "frames")
        self.assertEqual([len(i["refs"]) for i in self.kie.images], [1, 1])

        _, d = self.call("POST", "/api/batches/c162/references",
                         {"remove": d["references"][0]["file"]})
        self.assertEqual(d["references"], [])

    def test_an_anchor_frame_is_set_and_cleared_on_the_batch(self):
        clips = [{"image": f"shot {i}", "motion": "moves"} for i in range(1, 5)]
        self.make("c163", clips)
        _, d = self.call("POST", "/api/batches/c163/references", {"anchor": 2})
        self.assertEqual(d["anchor_frame"], 2)
        self.assertEqual([c["ref"] for c in d["clips"]], [None, None, None, None])
        self.assertEqual(d["plan"]["frames_make"], [c["name"] for c in d["clips"]])

        self.assertEqual(self.call("POST", "/api/batches/c163/references", {"anchor": 9})[0], 400)
        _, d = self.call("POST", "/api/batches/c163/references", {"anchor": None})
        self.assertEqual(d["anchor_frame"], 0)

    def test_the_anchor_frame_goes_with_every_clip_including_ones_with_their_own(self):
        clips = [{"image": f"shot {i}", "motion": "moves"} for i in range(1, 4)]
        self.make("c165", clips)
        self.call("POST", "/api/batches/c165/attach",
                  {"kind": "reference", "clips": ["c165_clip03"], "filename": "own.png",
                   "data": base64.b64encode(png_bytes()).decode()})
        _, d = self.call("POST", "/api/batches/c165/references", {"anchor": 1})
        self.assertEqual(d["anchor_frame"], 1)
        self.assertIsNone(d["clips"][1]["ref"])            # clip refs are left alone entirely
        self.assertEqual(d["clips"][2]["ref"]["kind"], "file")

        self.run_stage("c165", "frames")
        by_prompt = {i["prompt"]: len(i["refs"]) for i in self.kie.images}
        self.assertEqual(by_prompt["shot 1"], 0)           # the anchor itself
        self.assertEqual(by_prompt["shot 2"], 1)           # the anchor frame
        self.assertEqual(by_prompt["shot 3"], 2)           # the anchor frame + its own picture
        self.assertEqual(self.kie.images[0]["prompt"], "shot 1")   # drawn first, so it can be used

    def test_an_anchor_leaves_a_clip_that_has_its_own_picture_alone(self):
        clips = [{"image": "a", "motion": "a"},
                 {"image": "b", "motion": "b", "ref": {"kind": "needed", "note": "x"}}]
        _, d = self.make("c164", clips)
        self.call("POST", "/api/batches/c164/attach",
                  {"kind": "reference", "clips": ["c164_clip02"], "filename": "own.png",
                   "data": base64.b64encode(png_bytes()).decode()})
        _, d = self.call("POST", "/api/batches/c164/references", {"anchor": 1})
        self.assertEqual(d["clips"][1]["ref"]["kind"], "file")              # its own picture is untouched
        self.assertEqual(d["anchor_frame"], 1)                              # and the anchor applies too

    # ---- the anchor

    def wait_idle(self, name, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            _, d = self.call("GET", f"/api/batches/{name}")
            if not d["running"]:
                return d
            time.sleep(0.05)
        self.fail(f"{name} never stopped working")

    def wait_anchor(self, name, want="ready"):
        end = time.time() + 15
        while time.time() < end:
            _, d = self.call("GET", f"/api/batches/{name}")
            if d["anchor"].get("status") == want:
                return d
            time.sleep(0.05)
        self.fail(f"anchor never reached {want}")

    def test_a_drawn_reference_is_reviewed_then_kept(self):
        self.make("c170")
        _, d = self.call("POST", "/api/batches/c170/anchor",
                         {"action": "prompt", "prompt": "portrait of the avatar, plain background"})
        self.assertEqual(d["anchor"]["prompt"], "portrait of the avatar, plain background")

        _, d = self.call("POST", "/api/batches/c170/anchor", {"action": "generate"})
        self.assertEqual(d["anchor"]["status"], "working")
        d = self.wait_anchor("c170")
        self.assertTrue(d["anchor"]["file"].startswith("anchor-"))
        self.assertEqual(d["references"], [])                           # drawn, not kept yet
        self.assertEqual(self.kie.images[-1]["prompt"], "portrait of the avatar, plain background")

        self.run_stage("c170", "frames")                                # not kept: not used
        self.assertEqual([len(i["refs"]) for i in self.kie.images[1:]], [0, 0])

        _, d = self.call("POST", "/api/batches/c170/anchor", {"action": "use"})
        self.assertEqual(len(d["references"]), 1)
        self.assertEqual(d["anchor"]["prompt"], "portrait of the avatar, plain background")
        self.kie.images.clear()
        self.run_stage("c170", "frames", redo=["c170_clip01", "c170_clip02"])
        self.assertEqual([len(i["refs"]) for i in self.kie.images], [1, 1])

    def test_a_finished_draw_does_not_hold_the_batch_up(self):
        """the worker thread outlives the picture by a moment; that must not block a run"""
        self.make("c171")
        self.call("POST", "/api/batches/c171/anchor", {"action": "generate", "prompt": "the avatar"})
        d = self.wait_anchor("c171")
        self.assertEqual(d["anchor"]["status"], "ready")
        self.assertEqual(self.call("POST", "/api/batches/c171/run", {"stage": "frames"})[0], 200)
        self.wait_idle("c171")

    def test_a_draw_that_never_finished_does_not_spin_forever(self):
        self.make("c172")
        b = self.server.core.get_batch("c172")
        b.set_anchor(prompt="the avatar", status="working")     # as if the app was closed mid-draw
        self.server.core.batches.pop("c172")                    # and then opened again
        _, d = self.call("GET", "/api/batches/c172")
        self.assertEqual(d["anchor"]["status"], "failed")
        self.assertIn("Draw it again", d["anchor"]["error"])
        self.assertEqual(self.call("POST", "/api/batches/c172/run", {"stage": "frames"})[0], 200)
        self.wait_idle("c172")

    def test_the_anchor_rides_along_with_a_clip_that_has_its_own_reference(self):
        clips = [{"image": "her holding the bottle", "motion": "she turns it",
                  "ref": {"kind": "needed", "note": "the bottle"}},
                 {"image": "her smiling", "motion": "she nods"}]
        self.make("c173", clips)
        pic = base64.b64encode(png_bytes()).decode()
        self.call("POST", "/api/batches/c173/attach",
                  {"kind": "anchor", "filename": "face.png", "data": pic})
        self.call("POST", "/api/batches/c173/anchor", {"action": "use"})
        self.call("POST", "/api/batches/c173/attach",
                  {"kind": "reference", "filename": "style.png", "data": pic})          # whole batch
        self.call("POST", "/api/batches/c173/attach",
                  {"kind": "reference", "clips": ["c173_clip01"], "filename": "bottle.png", "data": pic})

        self.run_stage("c173", "frames")
        # clip 1: anchor + style + its own picture. clip 2: anchor + style only
        self.assertEqual([len(i["refs"]) for i in self.kie.images], [3, 2])

    def test_drawing_needs_a_prompt_and_keeping_needs_a_picture(self):
        self.make("c171")
        status, err = self.call("POST", "/api/batches/c171/anchor", {"action": "generate"})
        self.assertEqual(status, 400)
        self.assertIn("what the anchor should show", err["error"])
        status, err = self.call("POST", "/api/batches/c171/anchor", {"action": "use"})
        self.assertEqual(status, 400)
        self.assertIn("first", err["error"])

    def test_your_own_picture_can_be_kept_as_a_reference(self):
        self.make("c172")
        _, d = self.call("POST", "/api/batches/c172/attach",
                         {"kind": "anchor", "filename": "me.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        self.assertEqual(d["anchor"]["source"], "dropped")
        _, d = self.call("POST", "/api/batches/c172/anchor", {"action": "use"})
        self.assertEqual(len(d["references"]), 1)
        self.run_stage("c172", "frames")
        self.assertEqual([len(i["refs"]) for i in self.kie.images], [1, 1])

        _, d = self.call("POST", "/api/batches/c172/references", {"remove": d["references"][0]["file"]})
        self.assertEqual(d["references"], [])
        self.assertEqual(list((self.tmp / "out" / "c172" / "refs").glob("anchor-*")), [])

    def test_a_draft_can_be_thrown_away_without_touching_kept_pictures(self):
        self.make("c174")
        pic = base64.b64encode(png_bytes()).decode()
        self.call("POST", "/api/batches/c174/attach", {"kind": "anchor", "filename": "a.png", "data": pic})
        _, d = self.call("POST", "/api/batches/c174/anchor", {"action": "use"})
        kept = d["references"][0]["file"]
        self.call("POST", "/api/batches/c174/attach", {"kind": "anchor", "filename": "b.png", "data": pic})
        _, d = self.call("POST", "/api/batches/c174/anchor", {"action": "discard"})
        self.assertEqual([r["file"] for r in d["references"]], [kept])
        self.assertTrue((self.tmp / "out" / "c174" / "refs" / kept).is_file())

    def test_a_picture_a_clip_still_uses_is_not_deleted_with_the_batch_copy(self):
        clips = [{"image": "a", "motion": "a"}, {"image": "b", "motion": "b"}]
        _, d = self.make("c180", clips)
        pic = base64.b64encode(png_bytes()).decode()
        _, d = self.call("POST", "/api/batches/c180/attach",
                         {"kind": "reference", "clips": ["c180_clip01"], "filename": "shared.png", "data": pic})
        shared = d["clips"][0]["ref"]["file"]
        _, d = self.call("POST", "/api/batches/c180/attach",
                         {"kind": "reference", "file": shared})            # same picture, batch-wide
        self.assertEqual(len(d["references"]), 1)

        _, d = self.call("POST", "/api/batches/c180/references", {"remove": shared})
        self.assertEqual(d["references"], [])
        self.assertTrue((self.tmp / "out" / "c180" / "refs" / shared).is_file())
        self.assertEqual(d["clips"][0]["ref"]["file"], shared)             # the clip still has it
        self.run_stage("c180", "frames")
        self.assertEqual([len(i["refs"]) for i in self.kie.images], [1, 0])

    def test_a_draft_never_counts_as_kept_even_on_an_older_batch(self):
        self.make("c181")
        b = self.server.core.get_batch("c181")
        b.job["anchor"] = {"locked": True}                                 # written by an older version
        b.save_job()
        self.call("POST", "/api/batches/c181/attach",
                  {"kind": "anchor", "filename": "draft.png",
                   "data": base64.b64encode(png_bytes()).decode()})
        self.assertIsNone(b.anchor_file())                                 # staged, not kept
        self.run_stage("c181", "frames")
        self.assertEqual([len(i["refs"]) for i in self.kie.images], [0, 0])

    def test_rejects_a_file_that_is_not_a_picture(self):
        self.make()
        status, err = self.call("POST", "/api/batches/c150/attach",
                                {"kind": "frame", "clip": "c150_clip01", "filename": "notes.txt",
                                 "data": base64.b64encode(b"hello").decode()})
        self.assertEqual(status, 400)
        self.assertIn(".png", err["error"])

    # ---- settings

    def test_settings_keep_the_key_private_and_validate_the_folder(self):
        status, err = self.call("POST", "/api/config", {"api_key": "new-key-99990000", "output_dir": "relative"})
        self.assertEqual((status, "full folder path" in err["error"]), (400, True))
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertEqual(boot["config"]["key_hint"], "abcd")     # the rejected save changed nothing

        status, r = self.call("POST", "/api/config", {"api_key": "new-key-99990000",
                                                      "output_dir": str(self.tmp / "o2"), "frame_credits": "30"})
        self.assertEqual((status, r["config"]["key_hint"], r["config"]["frame_credits"]), (200, "0000", 30))
        saved = pl.read_json(self.tmp / "home" / "config.json")
        self.assertEqual(saved["frame_credits"], 30)

    def test_the_published_prices_reach_the_page(self):
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertEqual(boot["prices"]["frames"], {"1K": 8, "2K": 12, "4K": 18})
        self.assertEqual(boot["prices"]["video_per_second"]["pro"], {"off": 18, "on": 27})
        self.assertEqual(boot["prices"]["video_per_second"]["std"], {"off": 14, "on": 20})
        self.assertEqual(boot["prices"]["credit_usd"], 0.005)
        self.assertIsNone(boot["config"]["frame_credits"])        # nothing overridden

    def test_a_frame_costs_what_its_resolution_costs(self):
        for resolution, price in (("1K", 8), ("2K", 12), ("4K", 18)):
            name = f"c21{resolution}".replace("K", "k")
            self.make(name, [{"image": "a", "motion": "a"}])
            self.call("POST", f"/api/batches/{name}/clip",
                      {"clip": f"{name}_clip01", "image": "a"})
            b = self.server.core.get_batch(name)
            b.job["image_settings"] = {"aspect_ratio": "9:16", "resolution": resolution}
            b.save_job()
            _, d = self.call("GET", f"/api/batches/{name}")
            self.assertEqual(d["prices"]["frame"], price, resolution)
            d = self.run_stage(name, "frames")
            self.assertEqual(d["spend"]["credits"], price, resolution)

    def test_a_video_costs_by_the_second_by_mode_and_by_sound(self):
        cases = [({"mode": "pro", "duration": "5", "sound": False}, 90),
                 ({"mode": "pro", "duration": "5", "sound": True}, 135),
                 ({"mode": "pro", "duration": "10", "sound": False}, 180),
                 ({"mode": "std", "duration": "5", "sound": False}, 70),
                 ({"mode": "std", "duration": "5", "sound": True}, 100),
                 ({"mode": "4k", "duration": "5", "sound": False}, 335)]
        for settings, price in cases:
            self.assertEqual(pl.credits_per_video(settings), price, settings)
        self.assertIsNone(pl.credits_per_video({"mode": "turbo", "duration": "5"}))

        self.make("c220", [{"image": "a", "motion": "moves"}])
        b = self.server.core.get_batch("c220")
        b.job["settings"] = {**pl.DEFAULT_SETTINGS, "mode": "pro", "duration": "5", "sound": True}
        b.job["image_settings"] = {"aspect_ratio": "9:16", "resolution": "1K"}
        b.save_job()
        self.run_stage("c220", "frames")
        d = self.run_stage("c220", "videos")
        self.assertEqual(d["prices"], {"frame": 8, "video": 135, "video_per_second": 27,
                                       "override": False, "learned": False})
        self.assertEqual(d["spend"]["credits"], 8 + 135)

    def test_a_new_batch_is_1k_unless_you_say_otherwise(self):
        """1K is kie.ai's own default and the cheapest frame there is"""
        self.assertEqual(pl.DEFAULT_IMAGE_SETTINGS["resolution"], "1K")
        self.assertEqual(pl.credits_per_frame({}), 8)              # nothing said: the 1K default
        self.assertEqual(pl.credits_per_frame(None), 8)
        self.assertEqual(pl.credits_per_frame({"resolution": "banana"}), 12)   # unknown: the middle

        status, d = self.call("POST", "/api/batches", {"name": "c222", "clips": [{"image": "a", "motion": "a"}]})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["image_settings"]["resolution"], "1K")
        self.assertEqual(d["prices"]["frame"], 8)
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertEqual(boot["image_defaults"]["resolution"], "1K")
        d = self.run_stage("c222", "frames")
        self.assertEqual(self.kie.images[-1]["settings"]["resolution"], "1K")
        self.assertEqual(d["spend"]["credits"], 8)

    def test_what_kie_ai_says_it_charged_beats_the_estimate(self):
        """recordInfo carries creditsConsumed: the ledger is corrected to it"""
        self.kie.charges = 12                       # kie.ai bills 12 whatever we guessed
        self.make("c223", [{"image": "a", "motion": "moves"}])
        b = self.server.core.get_batch("c223")
        b.job["image_settings"] = {"aspect_ratio": "9:16", "resolution": "4K"}   # we would quote 18
        b.save_job()
        _, d = self.call("GET", "/api/batches/c223")
        self.assertEqual(d["prices"]["frame"], 18)

        d = self.run_stage("c223", "frames")
        self.assertEqual(d["spend"]["credits"], 12, "the ledger should hold kie.ai's figure")
        self.assertEqual(d["spend"]["settled"], 1)
        self.assertTrue(any("charged 12 credits" in l["text"] and "not the 18" in l["text"]
                            for l in d["log"]), [l["text"] for l in d["log"]])

        self.kie.charges = 90
        d = self.run_stage("c223", "videos")
        self.assertEqual(d["spend"]["credits"], 12 + 90)
        self.assertEqual(d["spend"]["settled"], 2)

    def test_without_a_figure_from_kie_ai_the_estimate_stands(self):
        self.kie.charges = None                     # older tasks don't carry creditsConsumed
        self.make("c224", [{"image": "a", "motion": "a"}])
        d = self.run_stage("c224", "frames")
        self.assertEqual((d["spend"]["credits"], d["spend"]["settled"]), (12, 0))

    def test_a_refunded_failure_settles_to_nothing(self):
        self.kie.charges = 0                        # kie.ai charged nothing for a failed job
        self.make("c225", [{"image": "fail please", "motion": "a"}])
        self.kie.results["fail please"] = [("failed", "moderation said no")]
        d = self.run_stage("c225", "frames")
        self.assertEqual(d["counts"]["failed"], 1)
        self.assertEqual(d["spend"]["credits"], 0)
        self.assertEqual(d["spend"]["settled"], 1)

    def test_the_estimate_follows_what_kie_ai_last_charged(self):
        """kie.ai's own price list has been wrong before: the account's history wins"""
        self.kie.charges = 12
        self.make("c226", [{"image": "a", "motion": "a"}, {"image": "b", "motion": "b"}])
        b = self.server.core.get_batch("c226")
        b.job["image_settings"] = {"aspect_ratio": "9:16", "resolution": "4K"}
        b.save_job()
        _, d = self.call("GET", "/api/batches/c226")
        self.assertEqual((d["prices"]["frame"], d["prices"]["learned"]), (18, False))   # published

        self.run_stage("c226", "frames")
        _, d = self.call("GET", "/api/batches/c226")
        self.assertEqual((d["prices"]["frame"], d["prices"]["learned"]), (12, True))    # charged

        # switch resolution and the old charge no longer applies
        b.job["image_settings"] = {"aspect_ratio": "9:16", "resolution": "2K"}
        b.save_job()
        _, d = self.call("GET", "/api/batches/c226")
        self.assertEqual((d["prices"]["frame"], d["prices"]["learned"]), (12, False))

    def test_a_refund_is_not_mistaken_for_a_price(self):
        self.kie.charges = 0
        self.make("c227", [{"image": "fail please", "motion": "a"}])
        self.kie.results["fail please"] = [("failed", "no")]
        self.run_stage("c227", "frames")
        _, d = self.call("GET", "/api/batches/c227")
        self.assertEqual((d["prices"]["frame"], d["prices"]["learned"]), (12, False))

    def test_a_price_you_set_yourself_wins(self):
        self.make("c221", [{"image": "a", "motion": "a"}])
        _, r = self.call("POST", "/api/config", {"frame_credits": "14"})
        self.assertEqual(r["config"]["frame_credits"], 14)
        _, d = self.call("GET", "/api/batches/c221")
        self.assertEqual((d["prices"]["frame"], d["prices"]["override"]), (14, True))
        _, r = self.call("POST", "/api/config", {"frame_credits": ""})    # empty hands it back
        self.assertIsNone(r["config"]["frame_credits"])
        self.assertIsNone(app.Config().frame_credits)
        _, d = self.call("GET", "/api/batches/c221")
        self.assertEqual((d["prices"]["frame"], d["prices"]["override"]), (12, False))   # 2K

    def test_the_old_flat_price_is_not_mistaken_for_a_choice(self):
        """every config written before the prices were known holds 12"""
        pl.write_json(app.config_dir() / "config.json", {"api_key": "k", "frame_credits": 12})
        self.assertIsNone(app.Config().frame_credits)
        pl.write_json(app.config_dir() / "config.json", {"api_key": "k", "frame_credits": None})
        self.assertIsNone(app.Config().frame_credits)
        pl.write_json(app.config_dir() / "config.json", {"api_key": "k", "frame_credits": 30})
        self.assertEqual(app.Config().frame_credits, 30)          # a real choice is kept

    def test_tutorial_is_remembered_once_it_is_seen(self):
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertIs(boot["config"]["tutorial_done"], False)
        status, r = self.call("POST", "/api/config", {"tutorial_done": True})
        self.assertEqual((status, r["config"]["tutorial_done"]), (200, True))
        self.assertIs(pl.read_json(self.tmp / "home" / "config.json")["tutorial_done"], True)
        _, boot = self.call("GET", "/api/bootstrap")     # and it survives a reload
        self.assertIs(boot["config"]["tutorial_done"], True)

    def test_pictures_are_served_as_something_to_show_not_to_download(self):
        """A download manager grabs anything that looks like a file to save — a .webp
        or an .mp4 especially — and the page is left with a broken picture."""
        self.make("c201")
        _, d = self.call("POST", "/api/batches/c201/attach",
                         {"kind": "reference", "filename": "look.webp",
                          "data": base64.b64encode(png_bytes()).decode()})
        file = d["references"][0]["file"]
        for path in (f"/api/batches/c201/ref/{file}", "/api/ui/logo.png"):
            with urlopen(Request(self.base + path, headers={"X-PK-Token": self.server.token})) as r:
                self.assertEqual(r.headers.get("Content-Disposition"), "inline", path)
        self.run_stage("c201", "frames")
        with urlopen(Request(self.base + "/api/batches/c201/frame/c201_clip01",
                             headers={"X-PK-Token": self.server.token})) as r:
            self.assertEqual(r.headers.get("Content-Disposition"), "inline")
            self.assertEqual(r.headers.get("Content-Type"), "image/png")

    def test_brand_art_is_served_from_a_short_allowlist(self):
        status, body = self.call("GET", "/api/ui/logo.png")
        self.assertEqual((status, body[1:4]), (200, b"PNG"))
        self.assertEqual(self.call("GET", "/api/ui/mark.png")[0], 200)
        self.assertEqual(self.call("GET", "/api/ui/index.html")[0], 404)     # not on the list
        self.assertEqual(self.call("GET", "/api/ui/..%2Fapp.py")[0], 404)
        self.assertEqual(self.call("GET", "/api/ui/logo.png", token=False)[0], 403)

    # ---- what a stop leaves behind

    def test_stopping_says_so_and_says_the_rest_is_free_to_collect(self):
        clips = [{"image": f"shot {i}", "motion": "moves"} for i in range(1, 4)]
        self.make("c180", clips)
        self.kie.hold = True                        # nothing ever finishes on its own
        try:
            self.assertEqual(self.call("POST", "/api/batches/c180/run", {"stage": "frames"})[0], 200)
            end = time.time() + 10          # wait for all three to be sent, or the re-run
            while time.time() < end and len(self.kie.images) < len(clips):   # would send the rest
                time.sleep(0.05)
            self.call("POST", "/api/batches/c180/stop")
            end = time.time() + 10
            while time.time() < end:
                _, d = self.call("GET", "/api/batches/c180")
                if not d["running"]:
                    break
                time.sleep(0.05)
        finally:
            self.kie.hold = False
        self.assertFalse(d["running"])
        self.assertEqual(d["stop_kind"], "stopped")
        self.assertIn("You stopped this run.", d["stop_reason"])
        self.assertIn("collecting", d["stop_reason"])
        self.assertTrue(d["plan"]["frames_check"], "sent frames should be waiting to be collected")

        # and collecting them afterwards costs nothing more
        before = len(self.kie.images)
        d = self.run_stage("c180", "frames")
        self.assertEqual(len(self.kie.images), before)
        self.assertEqual(d["counts"]["frames"], len(self.kie.images))

    def test_a_run_that_finishes_leaves_no_stop_line(self):
        self.make("c181")
        d = self.run_stage("c181", "frames")
        self.assertEqual((d["stop_reason"], d["stop_kind"]), (None, None))

    # ---- what was already paid for

    def test_clips_left_rendering_are_collected_at_the_next_launch(self):
        self.make("c182")
        self.kie.hold = True
        try:
            self.call("POST", "/api/batches/c182/run", {"stage": "frames"})
            end = time.time() + 10
            while time.time() < end and len(self.kie.images) < 2:
                time.sleep(0.05)
            self.call("POST", "/api/batches/c182/stop")
            end = time.time() + 10
            while time.time() < end and self.server.core.active_runs():
                time.sleep(0.05)
        finally:
            self.kie.hold = False
        sent = len(self.kie.images)
        self.assertEqual(self.server.core.resume_checks(), ["c182"])
        end = time.time() + 10
        while time.time() < end and self.server.core.active_runs():
            time.sleep(0.05)
        _, d = self.call("GET", "/api/batches/c182")
        self.assertEqual(d["counts"]["frames"], 2)
        self.assertEqual(len(self.kie.images), sent, "collecting must not send anything new")

    def test_collecting_at_launch_can_be_switched_off(self):
        self.make("c183")
        self.server.core.cfg.auto_collect = False
        self.assertEqual(self.server.core.resume_checks(), [])
        self.server.core.cfg.auto_collect = True
        self.assertEqual(self.server.core.resume_checks(), [])      # nothing was ever sent
        _, cfg = self.call("POST", "/api/config", {"auto_collect": False})
        self.assertFalse(cfg["config"]["auto_collect"])
        self.assertFalse(self.server.core.cfg.auto_collect)

    # ---- what a batch has cost

    def test_a_batch_adds_up_what_it_has_sent(self):
        self.make("c184")
        _, d = self.call("GET", "/api/batches/c184")
        self.assertEqual(d["spend"], {"credits": 0, "calls": 0, "frames": 0, "videos": 0,
                                      "references": 0, "unpriced": 0, "settled": 0})
        d = self.run_stage("c184", "frames")
        self.assertEqual((d["spend"]["frames"], d["spend"]["credits"]), (2, 24))
        d = self.run_stage("c184", "videos")
        self.assertEqual((d["spend"]["frames"], d["spend"]["videos"]), (2, 2))
        self.assertEqual(d["spend"]["credits"], 24 + 2 * 90)        # 12 a frame, 90 a 5s Pro video
        self.assertEqual(d["spend"]["unpriced"], 0)

    def test_a_drawn_reference_is_counted_too(self):
        self.make("c185")
        self.call("POST", "/api/batches/c185/anchor", {"action": "generate", "prompt": "the woman"})
        end = time.time() + 10
        while time.time() < end:
            _, d = self.call("GET", "/api/batches/c185")
            if d["anchor"].get("status") != "working":
                break
            time.sleep(0.05)
        self.assertEqual(d["anchor"]["status"], "ready")
        self.assertEqual((d["spend"]["references"], d["spend"]["credits"]), (1, 12))

    def test_a_picture_can_be_taken_back_off_a_clip(self):
        self.make("c240")
        body = {"kind": "frame", "clip": "c240_clip01", "filename": "mine.png",
                "data": base64.b64encode(png_bytes()).decode()}
        _, d = self.call("POST", "/api/batches/c240/attach", body)
        self.assertTrue(d["clips"][0]["frame"]["ready"])
        self.assertEqual(d["clips"][0]["frame"]["source"], "dropped")
        frames = self.tmp / "out" / "c240" / "frames"
        self.assertTrue(list(frames.glob("c240_clip01.*")))

        _, d = self.call("POST", "/api/batches/c240/clip", {"clip": "c240_clip01", "frame": None})
        self.assertFalse(d["clips"][0]["frame"]["ready"])
        self.assertEqual(d["clips"][0]["frame"]["status"], "pending")
        self.assertEqual(list(frames.glob("c240_clip01.*")), [], "the file goes too")
        self.assertIn("c240_clip01", d["plan"]["frames_make"], "it can be drawn again")
        self.assertTrue(any("picture removed" in l["text"] for l in d["log"]))

        status, err = self.call("POST", "/api/batches/c240/clip",
                                {"clip": "c240_clip01", "frame": None})
        self.assertEqual(status, 400, err)         # nothing left to remove

    def test_removing_a_frame_keeps_the_video_it_made(self):
        """the clip was paid for separately and the file is still good"""
        self.make("c241")
        self.run_stage("c241", "frames")
        d = self.run_stage("c241", "videos")
        self.assertTrue(d["clips"][0]["video"]["ready"])
        _, d = self.call("POST", "/api/batches/c241/clip", {"clip": "c241_clip01", "frame": None})
        self.assertFalse(d["clips"][0]["frame"]["ready"])
        self.assertTrue(d["clips"][0]["video"]["ready"])
        self.assertTrue((self.tmp / "out" / "c241" / "videos" / "c241_clip01.mp4").is_file())

    # ---- clip length and end frames

    def test_a_clip_runs_at_its_own_length(self):
        clips = [{"image": "a", "motion": "a", "duration": 7},
                 {"image": "b", "motion": "b", "duration": 3},
                 {"image": "c", "motion": "c"}]                      # falls back to the batch
        _, d = self.call("POST", "/api/batches", {"name": "c230", "clips": clips,
                                                  "settings": {"duration": "5", "mode": "pro"}})
        self.assertEqual([c["seconds"] for c in d["clips"]], [7, 3, 5])
        self.assertEqual([c["video_credits"] for c in d["clips"]], [126, 54, 90])   # 18/s

        self.run_stage("c230", "frames")
        d = self.run_stage("c230", "videos")
        self.assertEqual([v["seconds"] for v in self.kie.videos], [7, 3, 5])
        self.assertEqual([v["settings"]["duration"] for v in self.kie.videos], ["5", "5", "5"])
        self.assertEqual(d["spend"]["credits"], 3 * 8 + 126 + 54 + 90)   # 1K frames, then the clips

    def test_a_length_outside_what_kie_takes_is_refused(self):
        for bad in (2, 16, "soon"):
            status, err = self.call("POST", "/api/batches",
                                    {"name": "c231", "clips": [{"image": "a", "motion": "a",
                                                                "duration": bad}]})
            self.assertEqual(status, 400, bad)
            self.assertTrue("seconds" in err["error"] or "3" in err["error"], err)

    def test_a_clip_ends_on_the_next_clips_frame(self):
        clips = [{"image": "a", "motion": "a", "end": "next"},
                 {"image": "b", "motion": "b", "end": 3},
                 {"image": "c", "motion": "c"}]                      # the last one holds its own
        _, d = self.call("POST", "/api/batches", {"name": "c232", "clips": clips})
        names = [c["name"] for c in d["clips"]]
        self.assertEqual([c["ends_on"] for c in d["clips"]], [names[1], names[2], None])

        self.run_stage("c232", "frames")
        self.run_stage("c232", "videos")
        sent = [v["image_urls"] for v in self.kie.videos]
        self.assertEqual(len(sent[0]), 2, "a chained clip sends first and last frames")
        self.assertTrue(sent[0][0].endswith("c232_clip01.png"))
        self.assertTrue(sent[0][1].endswith("c232_clip02.png"))
        self.assertTrue(sent[1][1].endswith("c232_clip03.png"))
        self.assertEqual(len(sent[2]), 1, "the last clip has no successor to end on")

    def test_a_clip_waits_for_the_frame_it_ends_on(self):
        clips = [{"image": "a", "motion": "a", "end": 2}, {"image": "b", "motion": "b"}]
        _, d = self.call("POST", "/api/batches", {"name": "c233", "clips": clips})
        names = [c["name"] for c in d["clips"]]
        self.run_stage("c233", "frames", redo=[names[0]])            # only clip 1 has a frame
        _, d = self.call("GET", "/api/batches/c233")
        self.assertEqual(d["plan"]["videos_make"], [names[0]])       # clip 2 has no frame yet

        d = self.run_stage("c233", "videos")
        self.assertEqual(d["clips"][0]["video"]["status"], "failed")
        self.assertIn("isn't ready", d["clips"][0]["video"]["error"])
        self.assertEqual(self.kie.videos, [], "nothing was sent, so nothing was charged")

    def test_the_anchor_prompt_travels_with_the_batch(self):
        _, d = self.call("POST", "/api/batches", {
            "name": "c234", "clips": [{"image": "a", "motion": "a"}],
            "anchor": "a weathered fisherman, sixties, salt-white beard"})
        self.assertEqual(d["anchor"]["prompt"], "a weathered fisherman, sixties, salt-white beard")
        lines = ["batch: c235", "anchor: the same fisherman", "",
                 "1.", "image: a net", "motion: it sways", "duration: 6", "end: next"]
        _, parsed = self.call("POST", "/api/parse", {"text": chr(10).join(lines)})
        self.assertEqual(parsed["anchor"], "the same fisherman")
        self.assertEqual(parsed["clips"][0]["duration"], 6)
        self.assertEqual(parsed["clips"][0]["end"], "next")

    def test_veo_is_a_different_model_priced_a_different_way(self):
        """kling cannot lip-sync, so a talking-head ad has to go to veo — which bills
        per clip by tier rather than per second"""
        self.assertEqual(pl.video_model({}), "kling-3.0/video")
        self.assertEqual(pl.video_model({"model": "veo"}), "veo3_fast")            # Fast by default
        self.assertEqual(pl.video_model({"model": "veo", "veo_tier": "quality"}), "veo-3-1")
        self.assertEqual(pl.video_model({"model": "veo", "veo_tier": "lite"}), "veo3_lite")

        # length changes a kling clip's price; a veo clip's price is its tier and resolution
        self.assertEqual(pl.credits_per_video({"model": "kling", "mode": "pro"}, 10), 180)
        self.assertEqual(pl.credits_per_video({"model": "veo"}, 8), 65)            # fast 1080p
        self.assertEqual(pl.credits_per_video({"model": "veo"}, 4), 65)            # length is free
        self.assertEqual(pl.credits_per_video({"model": "veo", "veo_resolution": "720p"}), 60)
        self.assertEqual(pl.credits_per_video({"model": "veo", "veo_tier": "quality"}), 255)
        self.assertEqual(pl.credits_per_video({"model": "veo", "veo_tier": "lite",
                                               "veo_resolution": "4k"}), 150)

        # veo renders 4, 6 or 8 seconds and nothing else
        self.assertEqual([pl.veo_seconds(n) for n in (3, 5, 7, 10)], [4, 4, 6, 8])

        _, d = self.call("POST", "/api/batches", {
            "name": "c236", "clips": [{"image": "a", "motion": "she says hello"}],
            "settings": {"model": "veo", "duration": "5"}})
        self.assertEqual(d["settings"]["model"], "veo")
        self.assertEqual((d["settings"]["veo_tier"], d["settings"]["veo_resolution"]),
                         ("fast", "1080p"))
        self.assertEqual(d["prices"]["video"], 65)
        self.assertIsNone(d["prices"]["video_per_second"], "veo has no per-second price")
        self.assertEqual(d["clips"][0]["seconds"], 4, "5s is not a length veo renders")
        self.run_stage("c236", "frames")
        self.run_stage("c236", "videos")
        self.assertEqual(self.kie.videos[-1]["settings"]["model"], "veo")

    # ---- kie.ai's picture limit

    def test_the_page_is_told_how_many_pictures_each_frame_carries(self):
        clips = [{"image": "a", "motion": "a"}, {"image": "b", "motion": "b"}]
        _, d = self.make("c186", clips)
        self.assertEqual(d["reference_limit"], pl.MAX_REFERENCES)
        self.assertEqual([c["references_planned"] for c in d["clips"]], [0, 0])

        _, d = self.call("POST", "/api/batches/c186/attach",
                         {"kind": "reference", "filename": "style.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        _, d = self.call("POST", "/api/batches/c186/references", {"anchor": 1})
        self.assertEqual([c["references_planned"] for c in d["clips"]], [1, 2])
        self.assertEqual(d["clips"][1]["reference_labels"], ["frame 1", "a style reference"])

    # ---- seeing and changing what draws a frame

    def test_every_picture_a_frame_uses_is_named_for_the_page(self):
        clips = [{"image": "a", "motion": "a"},
                 {"image": "b", "motion": "b", "ref": {"kind": "needed", "note": "the woman"}}]
        self.make("c195", clips)
        self.call("POST", "/api/batches/c195/attach",
                  {"kind": "reference", "filename": "style.png",
                   "data": base64.b64encode(png_bytes()).decode()})
        _, d = self.call("POST", "/api/batches/c195/attach",
                         {"kind": "reference", "clips": ["c195_clip02"], "filename": "woman.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        _, d = self.call("POST", "/api/batches/c195/references", {"anchor": 1})

        first, second = d["clips"][0]["reference_items"], d["clips"][1]["reference_items"]
        self.assertEqual([i["kind"] for i in first], ["style"])
        self.assertEqual([i["kind"] for i in second], ["anchor", "style", "own"])
        anchor, style, own = second
        self.assertEqual((anchor["clip"], anchor["waiting"]), ("c195_clip01", True))   # not drawn yet
        self.assertEqual((style["file"], style["clip"]), ("style.png", ""))
        self.assertEqual((own["file"], own["label"]), ("woman.png", "its own picture"))
        self.assertEqual([i["path"] for i in second], ["", "", ""])   # nothing outside the batch

    def test_a_clips_picture_can_be_swapped_and_the_old_one_is_binned(self):
        clips = [{"image": "a", "motion": "a", "ref": {"kind": "needed", "note": "the woman"}}]
        self.make("c196", clips)
        refs = self.tmp / "out" / "c196" / "refs"
        self.call("POST", "/api/batches/c196/attach",
                  {"kind": "reference", "clips": ["c196_clip01"], "filename": "first.png",
                   "data": base64.b64encode(png_bytes()).decode()})
        _, d = self.call("POST", "/api/batches/c196/attach",
                         {"kind": "reference", "clips": ["c196_clip01"], "filename": "second.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        self.assertEqual(d["clips"][0]["ref"]["file"], "second.png")
        self.assertEqual(sorted(p.name for p in refs.iterdir()), ["second.png"])

    def test_a_picture_two_clips_share_survives_one_of_them_swapping(self):
        clips = [{"image": f"shot {i}", "motion": "moves",
                  "ref": {"kind": "needed", "note": "her"}} for i in (1, 2)]
        _, d = self.make("c197", clips)
        names = [c["name"] for c in d["clips"]]
        self.call("POST", "/api/batches/c197/attach",
                  {"kind": "reference", "clips": names, "filename": "her.png",
                   "data": base64.b64encode(png_bytes()).decode()})
        _, d = self.call("POST", "/api/batches/c197/attach",
                         {"kind": "reference", "clips": [names[0]], "filename": "new.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        self.assertEqual([c["ref"]["file"] for c in d["clips"]], ["new.png", "her.png"])
        self.assertTrue((self.tmp / "out" / "c197" / "refs" / "her.png").is_file())

    def test_taking_a_picture_off_puts_the_clip_back_to_waiting(self):
        clips = [{"image": "a", "motion": "a", "ref": {"kind": "needed", "note": "the woman"}}]
        self.make("c198", clips)
        _, d = self.call("POST", "/api/batches/c198/attach",
                         {"kind": "reference", "clips": ["c198_clip01"], "filename": "her.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        self.assertEqual((d["clips"][0]["needs_reference"], d["clips"][0]["ref"]["asked"]), (False, True))
        self.assertEqual(d["clips"][0]["ref"]["note"], "the woman")

        _, d = self.call("POST", "/api/batches/c198/clip",
                         {"clip": "c198_clip01", "ref": {"kind": "needed", "note": "the woman"}})
        self.assertEqual((d["clips"][0]["needs_reference"], d["clips"][0]["reference_note"]), (True, "the woman"))
        self.assertEqual(d["plan"]["blocked"], ["c198_clip01"])
        self.assertFalse((self.tmp / "out" / "c198" / "refs" / "her.png").exists())   # nothing wants it now

    def test_a_clip_that_was_never_asked_just_loses_its_picture(self):
        clips = [{"image": "a", "motion": "a"}]
        self.make("c199", clips)
        _, d = self.call("POST", "/api/batches/c199/attach",
                         {"kind": "reference", "clips": ["c199_clip01"], "filename": "look.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        self.assertNotIn("asked", d["clips"][0]["ref"])
        _, d = self.call("POST", "/api/batches/c199/clip", {"clip": "c199_clip01", "ref": None})
        self.assertEqual((d["clips"][0]["ref"], d["clips"][0]["needs_reference"]), (None, False))
        self.assertEqual(d["clips"][0]["reference_items"], [])
        self.assertEqual(d["plan"]["frames_make"], ["c199_clip01"])

    def test_the_note_survives_switching_to_another_clips_frame(self):
        clips = [{"image": "a", "motion": "a"},
                 {"image": "b", "motion": "b", "ref": {"kind": "needed", "note": "the woman"}}]
        self.make("c200", clips)
        _, d = self.call("POST", "/api/batches/c200/clip",
                         {"clip": "c200_clip02", "ref": {"kind": "frame", "index": 1}})
        self.assertEqual(d["clips"][1]["ref"]["note"], "the woman")
        self.assertTrue(d["clips"][1]["ref"]["asked"])
        self.assertEqual(d["clips"][1]["reference_items"][0]["clip"], "c200_clip01")

    def test_past_the_limit_the_run_says_what_it_dropped(self):
        with mock.patch.object(pl, "MAX_REFERENCES", 1):
            clips = [{"image": "a", "motion": "a"}]
            self.make("c187", clips)
            for i in range(2):
                self.call("POST", "/api/batches/c187/attach",
                          {"kind": "reference", "filename": f"style{i}.png",
                           "data": base64.b64encode(png_bytes()).decode()})
            _, d = self.call("GET", "/api/batches/c187")
            self.assertEqual(d["clips"][0]["references_planned"], 2)
            d = self.run_stage("c187", "frames")
            self.assertEqual(len(self.kie.images[-1]["refs"]), 1)
            self.assertEqual(d["clips"][0]["frame"]["references_used"], 1)
            self.assertEqual(d["clips"][0]["frame"]["references_dropped"], 1)
            self.assertTrue(any("will not be sent" in l["text"] for l in d["log"]))

    def test_the_same_picture_is_never_sent_twice_for_one_frame(self):
        clips = [{"image": "a", "motion": "a"}]
        self.make("c188", clips)
        _, d = self.call("POST", "/api/batches/c188/attach",
                         {"kind": "reference", "filename": "look.png",
                          "data": base64.b64encode(png_bytes()).decode()})
        file = d["references"][0]["file"]
        # the same file as the batch's picture and as the clip's own
        _, d = self.call("POST", "/api/batches/c188/attach",
                         {"kind": "reference", "clips": ["c188_clip01"], "file": file})
        self.assertEqual(d["clips"][0]["references_planned"], 1)
        self.run_stage("c188", "frames")
        self.assertEqual(len(self.kie.images[-1]["refs"]), 1)

    # ---- the batch, back out again

    def test_a_batch_can_be_copied_back_out_as_a_master_prompt(self):
        clips = [{"image": "a diya on a table", "motion": "the flame flickers"},
                 {"image": "a cup of coffee", "motion": "steam rises", "ref": {"kind": "frame", "index": 1}}]
        self.make("c189", clips)
        status, d = self.call("GET", "/api/batches/c189/master")
        self.assertEqual(status, 200, d)
        again = paste.parse_master(d["text"])
        self.assertEqual(again["name"], "c189")
        self.assertEqual([c["image"] for c in again["clips"]], [c["image"] for c in clips])
        self.assertEqual([c["motion"] for c in again["clips"]], [c["motion"] for c in clips])
        self.assertEqual(again["clips"][1]["ref"], {"kind": "frame", "index": 1})
        self.assertEqual(again["settings"].get("duration"), "5")
        self.assertEqual(again["warnings"], [])
        self.assertEqual(again["ignored"], [])

    def test_the_copied_prompt_says_what_it_cannot_carry(self):
        self.make("c190")
        self.call("POST", "/api/batches/c190/references", {"anchor": 2})
        _, d = self.call("GET", "/api/batches/c190/master")
        self.assertTrue(any("anchor frame" in n for n in d["notes"]))
        self.assertEqual(self.call("GET", "/api/batches/nope/master")[0], 404)

    # ---- storage

    def test_storage_lists_what_each_batch_is_holding(self):
        self.make("c191")
        self.run_stage("c191", "frames")
        status, s = self.call("GET", "/api/storage")
        self.assertEqual(status, 200, s)
        self.assertEqual([b["name"] for b in s["batches"]], ["c191"])
        row = s["batches"][0]
        self.assertEqual((row["clips"], row["archived"], row["running"]), (2, False, False))
        self.assertGreater(row["bytes"], 0)
        self.assertEqual(s["total"], row["bytes"])

    def test_archiving_hides_a_batch_without_losing_a_file(self):
        self.make("c192")
        self.run_stage("c192", "frames")
        frame = self.tmp / "out" / "c192" / "frames" / "c192_clip01.png"
        self.assertTrue(frame.exists())

        status, r = self.call("POST", "/api/storage/archive", {"names": ["c192"]})
        self.assertEqual((status, r["moved"], r["failed"]), (200, ["c192"], []))
        self.assertFalse(frame.exists())
        self.assertTrue((self.tmp / "out" / "_archive" / "c192" / "frames" / "c192_clip01.png").exists())
        self.assertEqual(self.call("GET", "/api/batches")[1], [])        # gone from the list
        self.assertEqual(self.call("GET", "/api/batches/c192")[0], 404)
        self.assertTrue(r["storage"]["batches"][0]["archived"])

        _, r = self.call("POST", "/api/storage/archive", {"names": ["c192"], "restore": True})
        self.assertEqual(r["moved"], ["c192"])
        self.assertTrue(frame.exists())
        self.assertEqual([b["name"] for b in self.call("GET", "/api/batches")[1]], ["c192"])

    def test_archiving_refuses_the_impossible_instead_of_guessing(self):
        self.make("c193")

        class Busy:
            def is_alive(self):
                return True

        self.server.core.runners["c193"] = Busy()
        try:
            _, r = self.call("POST", "/api/storage/archive", {"names": ["c193"]})
            self.assertEqual(r["moved"], [])
            self.assertIn("running", r["failed"][0]["why"])
        finally:
            self.server.core.runners.pop("c193")
        _, r = self.call("POST", "/api/storage/archive", {"names": ["..", "nope", "c193/x"]})
        self.assertEqual(r["moved"], [])
        self.assertEqual(len(r["failed"]), 3)
        self.assertTrue((self.tmp / "out" / "c193" / "job.json").exists())

    def test_a_name_cannot_be_taken_twice_by_archiving(self):
        self.make("c194")
        self.call("POST", "/api/storage/archive", {"names": ["c194"]})
        self.make("c194")                                    # the name is free again
        _, r = self.call("POST", "/api/storage/archive", {"names": ["c194"]})
        self.assertEqual(r["moved"], [])
        self.assertIn("already there", r["failed"][0]["why"])

    # ---- deleting a batch

    def test_delete_removes_the_folder_and_the_batch(self):
        self.make()
        self.run_stage("c150", "frames")
        root = self.tmp / "out" / "c150"
        self.assertTrue((root / "frames" / "c150_clip01.png").exists())

        status, d = self.call("POST", "/api/batches/c150/delete")
        self.assertEqual((status, d), (200, {"deleted": "c150"}))
        self.assertFalse(root.exists())
        self.assertEqual(self.call("GET", "/api/batches")[1], [])
        self.assertEqual(self.call("GET", "/api/batches/c150")[0], 404)
        self.assertEqual(self.make()[0], 200)          # the name is free again

    def test_delete_refuses_while_a_batch_is_running(self):
        self.make()

        class Busy:
            def is_alive(self):
                return True

        self.server.core.runners["c150"] = Busy()
        try:
            status, err = self.call("POST", "/api/batches/c150/delete")
            self.assertEqual(status, 409)
            self.assertIn("Stop the batch", err["error"])
        finally:
            self.server.core.runners.pop("c150")
        self.assertTrue((self.tmp / "out" / "c150" / "job.json").exists())

    def test_delete_cannot_escape_the_output_folder(self):
        self.make()
        self.assertEqual(self.call("POST", "/api/batches/..%2F..%2Fetc/delete")[0], 404)
        self.assertEqual(self.call("POST", "/api/batches/nope/delete")[0], 404)
        self.assertTrue((self.tmp / "out" / "c150" / "job.json").exists())

    # ---- updates

    def newer_manifest(self, version="9.9.9"):
        return {"version": version, "notes": ["Something better"],
                "page": "https://github.com/x/y/releases/tag/v9.9.9",
                "windows": {"url": "https://github.com/x/y/releases/download/v9.9.9/KlingStudio.exe",
                            "sha256": "a" * 64, "size": 2048}}

    def test_update_check_offers_a_newer_build_and_hides_the_url(self):
        _, state = self.call("GET", "/api/update")
        self.assertEqual((state["state"], state["app_version"]), ("idle", app.APP_VERSION))
        self.assertIn("github.com", state["source"])

        with mock.patch.object(app.updater, "read_source", return_value=self.newer_manifest()):
            _, state = self.call("POST", "/api/update/check")
        self.assertEqual((state["state"], state["version"]), ("ready", "9.9.9"))
        self.assertEqual(state["notes"], ["Something better"])
        self.assertNotIn("url", state)          # the page never needs the download link
        self.assertNotIn("sha256", state)

    def test_update_check_on_the_newest_build_says_current(self):
        with mock.patch.object(app.updater, "read_source", return_value=self.newer_manifest(app.APP_VERSION)):
            _, state = self.call("POST", "/api/update/check")
        self.assertEqual(state["state"], "current")

    def test_a_broken_manifest_is_reported_not_raised(self):
        with mock.patch.object(app.updater, "read_source", return_value={"version": "9.9.9",
                               "windows": {"url": "http://evil.example/x.exe", "sha256": "a" * 64}}):
            _, state = self.call("POST", "/api/update/check")
        self.assertEqual(state["state"], "error")
        self.assertIn("https", state["error"])

    def test_a_release_with_no_build_for_this_computer_still_shows_up(self):
        mac_only = {"version": "9.9.9", "notes": ["Mac only for now"],
                    "mac": {"url": "https://github.com/x/y/releases/download/v9.9.9/mac.zip",
                            "sha256": "b" * 64, "size": 10}}
        with mock.patch.object(app.updater, "read_source", return_value=mac_only):
            _, state = self.call("POST", "/api/update/check")
        self.assertEqual((state["state"], state["version"]), ("ready", "9.9.9"))
        self.assertIs(state["has_download"], False)          # nothing to install here
        status, err = self.call("POST", "/api/update/install")
        self.assertEqual(status, 400)
        self.assertIn("no download for this computer", err["error"])

    def test_install_is_refused_unless_something_is_ready(self):
        self.assertEqual(self.call("POST", "/api/update/install")[0], 409)      # nothing checked yet
        with mock.patch.object(app.updater, "read_source", return_value=self.newer_manifest()):
            self.call("POST", "/api/update/check")
        status, err = self.call("POST", "/api/update/install")                  # tests run from source
        self.assertEqual(status, 400)
        self.assertIn("can't replace itself", err["error"])

    def test_install_waits_for_a_running_batch(self):
        self.make()
        with mock.patch.object(app.updater, "read_source", return_value=self.newer_manifest()):
            self.call("POST", "/api/update/check")

        class Busy:
            def is_alive(self):
                return True

        self.server.core.runners["c150"] = Busy()
        try:
            with mock.patch.object(app.updater, "can_self_install", lambda: True):
                status, err = self.call("POST", "/api/update/install")
            self.assertEqual(status, 409)
            self.assertIn("running batch", err["error"])
        finally:
            self.server.core.runners.pop("c150")

    def test_update_source_must_be_https_or_a_full_path(self):
        for bad in ("http://example.com/latest.json", "not-a-path/latest.json"):
            status, err = self.call("POST", "/api/config", {"update_source": bad})
            self.assertEqual(status, 400, err)
        good = "https://example.com/latest.json"
        _, r = self.call("POST", "/api/config", {"update_source": good, "auto_update_check": False})
        self.assertEqual((r["config"]["update_source"], r["config"]["auto_update_check"]), (good, False))
        self.assertEqual(pl.read_json(self.tmp / "home" / "config.json")["update_source"], good)

    def test_instance_reuse_requires_the_same_version(self):
        pl.write_json(app.config_dir() / "instance.json", {"port": 1234, "token": "t", "pid": 1})

        class Resp:
            def __init__(self, version):
                self.body = json.dumps({"ok": True, "version": version}).encode()

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(app, "urlopen", return_value=Resp(app.APP_VERSION)):
            self.assertEqual(app.running_instance_url(), "http://127.0.0.1:1234/")
        with mock.patch.object(app, "urlopen", return_value=Resp("2.2.0")):
            self.assertIsNone(app.running_instance_url())

    # ---- Kling clip length
    #
    # Kling renders any whole number of seconds from 3 to 15; the page used to offer
    # only 5 and 10, so the lengths in between were reachable per clip but not for a
    # whole batch.

    def new(self, name, **settings):
        return self.call("POST", "/api/batches",
                         {"name": name, "clips": CLIPS, "settings": settings,
                          "image_settings": {"resolution": "1K"}})

    def test_a_length_between_the_old_two_is_accepted(self):
        status, d = self.new("sevens", duration="7")
        self.assertEqual(status, 200, d)
        _, b = self.call("GET", "/api/batches/sevens")
        self.assertEqual(b["settings"]["duration"], "7")

    def test_both_ends_of_the_range_are_accepted(self):
        for seconds in ("3", "15"):
            status, d = self.new(f"edge{seconds}", duration=seconds)
            self.assertEqual(status, 200, d)
            _, b = self.call("GET", f"/api/batches/edge{seconds}")
            self.assertEqual(b["settings"]["duration"], seconds)

    def test_a_length_kling_cannot_render_is_refused_not_quietly_clamped(self):
        status, d = self.new("toolong", duration="20")
        self.assertEqual(status, 400, d)
        self.assertIn("out of range", d["error"])

    def test_veo_snaps_instead_of_being_refused(self):
        # veo renders 4, 6 or 8; 5 is not one of them, but that is not an error
        status, d = self.new("veodur", model="veo", duration="5")
        self.assertEqual(status, 200, d)

    def test_the_quoted_credits_follow_the_length(self):
        self.new("five", duration="5")
        self.new("eleven", duration="11")
        _, a = self.call("GET", "/api/batches/five")
        _, b = self.call("GET", "/api/batches/eleven")
        self.assertEqual(b["clips"][0]["video_credits"],
                         a["clips"][0]["video_credits"] / 5 * 11)

    # ---- changing a batch's settings after it exists
    #
    # The reason to change them usually only shows up once the first frames are back,
    # and there was no way to do it without starting the batch again from the paste.

    def test_settings_can_be_changed_after_the_batch_is_made(self):
        self.make("resettle")
        status, d = self.call("POST", "/api/batches/resettle/settings",
                              {"settings": {"duration": "9", "mode": "std"},
                               "image_settings": {"resolution": "4K"}})
        self.assertEqual(status, 200, d)
        _, b = self.call("GET", "/api/batches/resettle")
        self.assertEqual(b["settings"]["duration"], "9")
        self.assertEqual(b["settings"]["mode"], "std")
        self.assertEqual(b["image_settings"]["resolution"], "4K")

    def test_what_is_left_alone_stays_as_it_was(self):
        self.make("partial")
        _, before = self.call("GET", "/api/batches/partial")
        self.call("POST", "/api/batches/partial/settings", {"settings": {"mode": "std"}})
        _, after = self.call("GET", "/api/batches/partial")
        self.assertEqual(after["settings"]["aspect_ratio"], before["settings"]["aspect_ratio"])
        self.assertEqual(after["image_settings"], before["image_settings"])

    def test_the_new_price_is_quoted_straight_away(self):
        self.make("repriced")                       # make() asks for 2K frames
        _, before = self.call("GET", "/api/batches/repriced")
        self.assertEqual(before["prices"]["frame"], 12)
        self.call("POST", "/api/batches/repriced/settings",
                  {"image_settings": {"resolution": "4K"}})
        _, after = self.call("GET", "/api/batches/repriced")
        self.assertEqual(after["prices"]["frame"], 18)

    def test_a_length_kling_cannot_render_is_refused_here_too(self):
        self.make("badlen")
        status, d = self.call("POST", "/api/batches/badlen/settings", {"settings": {"duration": "40"}})
        self.assertEqual(status, 400, d)
        self.assertIn("out of range", d["error"])

    def test_a_frame_already_drawn_keeps_what_it_was_drawn_with(self):
        self.make("kept")
        self.run_stage("kept", "frames")
        _, before = self.call("GET", "/api/batches/kept")
        drawn = [c["frame"]["file"] for c in before["clips"] if c["frame"].get("file")]
        self.assertTrue(drawn, "no frames were made")
        self.call("POST", "/api/batches/kept/settings", {"image_settings": {"resolution": "4K"}})
        _, after = self.call("GET", "/api/batches/kept")
        self.assertEqual([c["frame"]["file"] for c in after["clips"] if c["frame"].get("file")], drawn)
        self.assertTrue(all(c["frame"]["ready"] for c in after["clips"]))

    def test_the_change_is_written_into_the_log(self):
        self.make("logged")
        self.call("POST", "/api/batches/logged/settings", {"settings": {"mode": "std"}})
        _, b = self.call("GET", "/api/batches/logged")
        self.assertTrue(any("video settings" in e["text"] for e in b["log"]),
                        [e["text"] for e in b["log"]])


if __name__ == "__main__":
    unittest.main()
