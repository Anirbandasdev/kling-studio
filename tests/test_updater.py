"""Offline tests for the update path: manifests, hashes, the swap script, the API.

Nothing here touches the network, GitHub or a real exe. Run from the v3 folder:
    python -X utf8 -m unittest discover -s tests -v
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater  # noqa: E402


def manifest(version="3.1.0", **over):
    m = {"version": version, "notes": ["Faster retries"],
         "page": "https://github.com/x/y/releases/tag/v3.1.0",
         "windows": {"url": "https://github.com/x/y/releases/download/v3.1.0/KlingStudio-3.1.0.exe",
                     "sha256": "a" * 64, "size": 1234}}
    m.update(over)
    return m


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("3.1.0", "3.0.0"))
        self.assertTrue(updater.is_newer("3.0.1", "3.0"))
        self.assertTrue(updater.is_newer("10.0.0", "9.9.9"))
        self.assertFalse(updater.is_newer("3.0.0", "3.0.0"))
        self.assertFalse(updater.is_newer("2.9.9", "3.0.0"))
        self.assertFalse(updater.is_newer("nightly", "3.0.0"))

    def test_bad_versions_are_rejected(self):
        for bad in ("", "v3.1", "3.1.0-beta", "latest", "3.1.0.0.1"):
            with self.assertRaises(updater.UpdateError):
                updater.parse_version(bad)


class ManifestTests(unittest.TestCase):
    def test_a_newer_build_is_offered(self):
        info = updater.validate(manifest(), "3.0.0")
        self.assertEqual((info["version"], info["newer"], info["size"]), ("3.1.0", True, 1234))
        self.assertEqual(info["notes"], ["Faster retries"])
        self.assertTrue(info["url"].startswith("https://"))

    def test_same_or_older_is_not_an_error(self):
        self.assertFalse(updater.validate(manifest("3.0.0"), "3.0.0")["newer"])
        self.assertFalse(updater.validate(manifest("2.9.0"), "3.0.0")["newer"])

    def test_plain_http_and_bad_hashes_are_refused(self):
        bad_url = manifest(windows={"url": "http://example.com/x.exe", "sha256": "a" * 64, "size": 10})
        with self.assertRaises(updater.UpdateError):
            updater.validate(bad_url, "3.0.0")
        for sha in ("", "zz", "A" * 63):
            with self.assertRaises(updater.UpdateError):
                updater.validate(manifest(windows={"url": "https://e.com/x.exe", "sha256": sha, "size": 1}), "3.0.0")
        with self.assertRaises(updater.UpdateError):
            updater.validate(manifest(page="http://example.com"), "3.0.0")
        with self.assertRaises(updater.UpdateError):
            updater.validate({"notes": ["no version"]}, "3.0.0")

    def test_an_https_source_will_not_read_a_local_file(self):
        with self.assertRaises(updater.UpdateError):
            updater.read_source("ftp://example.com/latest.json")
        with self.assertRaises(updater.UpdateError):
            updater.read_source("")

    def test_a_folder_source_reads_the_file_next_to_it(self):
        tmp = Path(tempfile.mkdtemp(prefix="ks_up_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "latest.json").write_text(json.dumps(manifest(windows={
            "url": str(tmp / "KlingStudio-3.1.0.exe"), "sha256": "b" * 64, "size": 5})), encoding="utf-8")
        info = updater.validate(updater.read_source(tmp / "latest.json"), "3.0.0", tmp / "latest.json")
        self.assertTrue(info["newer"])
        self.assertTrue(info["url"].endswith("KlingStudio-3.1.0.exe"))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ks_dl_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stage = mock.patch.object(updater, "staging_dir", lambda: self.tmp)
        self.stage.start()
        self.addCleanup(self.stage.stop)

    def fake_download(self, payload):
        def download(url, dest, progress=None):
            dest.write_bytes(payload)
            if progress:
                progress(len(payload), len(payload), 1)
            return len(payload)
        return download

    def test_a_good_download_is_kept(self):
        body = b"pretend exe" * 10
        info = {"version": "3.1.0", "url": "https://e.com/x.exe",
                "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
        path = updater.download_update(info, download=self.fake_download(body))
        self.assertEqual(path.read_bytes(), body)

    def test_a_tampered_download_is_deleted(self):
        info = {"version": "3.1.0", "url": "https://e.com/x.exe", "sha256": "c" * 64, "size": 0}
        with self.assertRaises(updater.UpdateError):
            updater.download_update(info, download=self.fake_download(b"not the build"))
        self.assertEqual(list(self.tmp.glob("*.exe")), [])

    def test_a_wrong_size_is_refused_before_hashing(self):
        body = b"short"
        info = {"version": "3.1.0", "url": "https://e.com/x.exe",
                "sha256": hashlib.sha256(body).hexdigest(), "size": 999}
        with self.assertRaises(updater.UpdateError):
            updater.download_update(info, download=self.fake_download(body))

    def test_the_swap_script_waits_for_this_process_then_restores_on_failure(self):
        new, target = self.tmp / "new.exe", self.tmp / "Kling Studio.exe"
        script = updater.write_swap_script(new, target, 4242)
        text = script.read_text(encoding="utf-8")
        self.assertIn("PID eq 4242", text)
        self.assertIn(f'move /y "{target}" "{target.with_suffix(".bak.exe")}"', text)
        self.assertIn(f'move /y "{new}" "{target}"', text)
        # the new build didn't land: put the old one back rather than leave nothing
        self.assertIn(f'if not exist "{target}" goto restore', text)
        self.assertIn(f'move /y "{target.with_suffix(".bak.exe")}" "{target}"', text)
        self.assertIn(f'start "" "{target}"', text)

    def test_the_swap_script_keeps_a_console_to_run_in(self):
        """DETACHED_PROCESS leaves the script with no console at all, and the first
        piped command (tasklist | findstr) then hangs forever: no swap, no restart."""
        self.assertEqual(updater.SWAP_FLAGS & 0x00000008, 0, "DETACHED_PROCESS must not be set")
        self.assertTrue(updater.SWAP_FLAGS & 0x08000000, "the console has to stay hidden")

    @unittest.skipUnless(sys.platform == "win32", "the swap script is cmd")
    def test_the_swap_really_swaps_once_the_app_has_gone(self):
        import subprocess
        import time
        staging = self.tmp / "staging"
        staging.mkdir()
        target, new = self.tmp / "Kling Studio.exe", staging / "Kling Studio 9.9.9.exe"
        target.write_bytes(b"OLDBUILD")
        new.write_bytes(b"NEWBUILD")
        # a process to stand in for the running app, and a target it is "holding"
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.5)"])
        try:
            with mock.patch.object(updater, "staging_dir", lambda: staging), \
                 mock.patch.object(updater, "can_self_install", lambda: True), \
                 mock.patch.object(updater, "SWAP_SCRIPT", updater.SWAP_SCRIPT.replace(
                     'start "" "{target}"', 'rem started')):        # don't launch anything
                script = updater.apply_update(new, target=target, pid=holder.pid)
            time.sleep(1.0)
            self.assertEqual(target.read_bytes(), b"OLDBUILD", "must wait for the app to exit")
            holder.wait(timeout=10)

            def landed():
                try:                       # mid-swap the file is briefly not there at all
                    return target.read_bytes() == b"NEWBUILD"
                except OSError:
                    return False

            end = time.time() + 45      # the script polls, then retries the rename a few times
            while time.time() < end and not landed():
                time.sleep(0.25)
        finally:
            holder.kill()
            holder.wait(timeout=5)
        self.assertEqual(target.read_bytes(), b"NEWBUILD", "the new build never went in")
        self.assertEqual(updater.rollback_path(target).read_bytes(), b"OLDBUILD")
        self.assertFalse(script.exists(), "the script should tidy itself away")
        self.assertIn("swapped in the new build",
                      (staging / "update.log").read_text(encoding="utf-8", errors="replace"))

    def test_source_installs_refuse_to_swap_themselves(self):
        with mock.patch.object(updater, "can_self_install", lambda: False):
            with self.assertRaises(updater.UpdateError):
                updater.apply_update(self.tmp / "new.exe", self.tmp / "old.exe", 1)


if __name__ == "__main__":
    unittest.main()
