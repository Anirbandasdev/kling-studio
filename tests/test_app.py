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

    def test_frame_credits_default_to_twelve_and_can_be_changed(self):
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertEqual(boot["config"]["frame_credits"], app.DEFAULT_FRAME_CREDITS)
        self.assertEqual(app.DEFAULT_FRAME_CREDITS, 12)
        _, r = self.call("POST", "/api/config", {"frame_credits": "14"})
        self.assertEqual(r["config"]["frame_credits"], 14)
        _, r = self.call("POST", "/api/config", {"frame_credits": ""})     # empty goes back to 12
        self.assertEqual(r["config"]["frame_credits"], 12)
        self.assertEqual(app.Config().frame_credits, 12)

    def test_an_older_config_with_no_price_still_gets_one(self):
        pl.write_json(app.config_dir() / "config.json", {"api_key": "k", "frame_credits": None})
        self.assertEqual(app.Config().frame_credits, 12)

    def test_tutorial_is_remembered_once_it_is_seen(self):
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertIs(boot["config"]["tutorial_done"], False)
        status, r = self.call("POST", "/api/config", {"tutorial_done": True})
        self.assertEqual((status, r["config"]["tutorial_done"]), (200, True))
        self.assertIs(pl.read_json(self.tmp / "home" / "config.json")["tutorial_done"], True)
        _, boot = self.call("GET", "/api/bootstrap")     # and it survives a reload
        self.assertIs(boot["config"]["tutorial_done"], True)

    def test_brand_art_is_served_from_a_short_allowlist(self):
        status, body = self.call("GET", "/api/ui/logo.png")
        self.assertEqual((status, body[1:4]), (200, b"PNG"))
        self.assertEqual(self.call("GET", "/api/ui/mark.png")[0], 200)
        self.assertEqual(self.call("GET", "/api/ui/index.html")[0], 404)     # not on the list
        self.assertEqual(self.call("GET", "/api/ui/..%2Fapp.py")[0], 404)
        self.assertEqual(self.call("GET", "/api/ui/logo.png", token=False)[0], 403)

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


if __name__ == "__main__":
    unittest.main()
