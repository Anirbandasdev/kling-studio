"""The release script's version rewriting.

3.9.0 shipped with a handbook footer still reading "version 3.8.2". The pattern meant
to catch it had a stray backspace byte in front of the word — invisible in an editor,
invisible in the printed source — so the lookbehind could never match and re.sub
quietly did nothing. A regex that matches nothing raises no error, which is why this
needs a test rather than a careful reading.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import publish  # noqa: E402


HANDBOOK = """<html>
  <p>Download <code>KlingStudio-Setup-3.8.2.exe</code> and run it.</p>
  <p>On a Mac, <code>KlingStudio-3.8.2-mac.dmg</code>.</p>
  <span class="sp">Kling Studio 3.8.2 &middot; release notes</span>
  <span>&nbsp;&middot; version 3.8.2</span>
  <svg><path d="M3.8.2L1.5.5z"/></svg>
</html>
"""


class BumpTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "docs").mkdir()
        (self.tmp / "app.py").write_text('APP_VERSION = "3.8.2"\n', encoding="utf-8")
        (self.tmp / "installer.iss").write_text('#define AppVersion "3.8.2"\n', encoding="utf-8")
        (self.tmp / "docs" / "handbook.html").write_text(HANDBOOK, encoding="utf-8")
        self.real, publish.ROOT = publish.ROOT, self.tmp
        self.addCleanup(lambda: setattr(publish, "ROOT", self.real))

    def book(self):
        return (self.tmp / "docs" / "handbook.html").read_text(encoding="utf-8")

    def test_every_version_string_in_the_handbook_moves(self):
        publish.bump("3.9.0")
        text = self.book()
        for expected in ("KlingStudio-Setup-3.9.0.exe", "KlingStudio-3.9.0-mac.dmg",
                         "Kling Studio 3.9.0", "version 3.9.0"):
            self.assertIn(expected, text)
        # the only 3.8.2 allowed to survive is the SVG path data the next test guards
        left = [line for line in text.splitlines() if "3.8.2" in line and "<path" not in line]
        self.assertEqual(left, [], f"a version string was left behind: {left}")

    def test_svg_coordinates_are_not_mistaken_for_a_version(self):
        # "M3.8.2L1.5.5z" is real path data; a bare \d+\.\d+\.\d+ would eat it and
        # scribble the version number through every drawing in the document.
        publish.bump("3.9.0")
        self.assertIn('d="M3.8.2L1.5.5z"', self.book())

    def test_bump_reports_the_docs_it_rewrote_so_they_get_committed(self):
        touched = publish.bump("3.9.0")
        self.assertEqual(touched, ["docs/handbook.html"])

    def test_a_handbook_already_on_the_new_version_is_left_alone(self):
        publish.bump("3.9.0")
        self.assertEqual(publish.bump("3.9.0"), [])

    def test_the_app_and_installer_versions_move_too(self):
        publish.bump("3.9.0")
        self.assertIn('APP_VERSION = "3.9.0"', (self.tmp / "app.py").read_text(encoding="utf-8"))
        self.assertIn('#define AppVersion "3.9.0"',
                      (self.tmp / "installer.iss").read_text(encoding="utf-8"))


class SourceTest(unittest.TestCase):
    def test_no_control_characters_hide_in_the_release_script(self):
        """A heredoc turning \\b into a backspace is how the footer bug got in."""
        raw = (ROOT / "tools" / "publish.py").read_bytes()
        stray = sorted({b for b in raw if b < 32 and b not in (9, 10, 13)})
        self.assertEqual(stray, [], f"control bytes in publish.py: {stray}")


if __name__ == "__main__":
    unittest.main()
