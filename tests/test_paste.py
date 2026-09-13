"""Tests for the master prompt parser. Offline, no API calls.

Run from the v3 folder:  python -X utf8 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paste  # noqa: E402


MASTER = """batch: c150
settings: 9:16 · 5s · pro · sound off · 2K

references:
G:\\vishal\\EIS\\avatars\\meera.png
needed: the same kitchen as the script

1.
image: wide shot of a diya burning on a dark wooden table, warm rim light
motion: the flame flickers and dances, wisps of smoke sway

2.
image: close up of a steaming cup of chai on the same table
motion: steam rises and curls slowly
ref: frame 1

3.
image: golden oil streaming into an open palm
motion: oil streams down, droplets splash
ref: needed: the bottle from the brand shots
"""


class MasterBlockTests(unittest.TestCase):
    def test_full_block(self):
        r = paste.parse_master(MASTER)
        self.assertEqual(r["name"], "c150")
        self.assertEqual(r["settings"], {"aspect_ratio": "9:16", "duration": "5", "mode": "pro",
                                         "sound": False, "resolution": "2K"})
        self.assertEqual(r["references"], [
            {"kind": "path", "path": "G:\\vishal\\EIS\\avatars\\meera.png"},
            {"kind": "needed", "note": "the same kitchen as the script"}])
        self.assertEqual(len(r["clips"]), 3)
        self.assertEqual(r["clips"][0]["image"],
                         "wide shot of a diya burning on a dark wooden table, warm rim light")
        self.assertEqual(r["clips"][0]["motion"], "the flame flickers and dances, wisps of smoke sway")
        self.assertIsNone(r["clips"][0]["ref"])
        self.assertEqual(r["clips"][1]["ref"], {"kind": "frame", "index": 1})
        self.assertEqual(r["clips"][2]["ref"], {"kind": "needed", "note": "the bottle from the brand shots"})
        self.assertEqual((r["warnings"], r["ignored"]), ([], []))

    def test_two_list_style(self):
        r = paste.parse_master("batch: c151\n\nimage prompts:\n1. a lamp\n2. a cup\n\n"
                               "motion prompts:\n1. flame flickers\n2. steam rises")
        self.assertEqual([c["image"] for c in r["clips"]], ["a lamp", "a cup"])
        self.assertEqual([c["motion"] for c in r["clips"]], ["flame flickers", "steam rises"])
        self.assertEqual(r["warnings"], [])

    def test_wrapped_lines_and_bullets_and_bare_numbers(self):
        r = paste.parse_master("batch: c152\nClip 1\nimage: a lamp on a table\n"
                               "lit from the left\nmotion: the flame\nflickers gently")
        self.assertEqual(r["clips"], [{"image": "a lamp on a table lit from the left",
                                       "motion": "the flame flickers gently", "ref": None}])

    def test_counts_and_missing_prompts_are_warnings_not_guesses(self):
        r = paste.parse_master("batch: c153\nimage prompts:\n1. a\n2. b\n3. c\nmotion prompts:\n1. x\n2. y")
        self.assertIn("3 image prompts but 2 motion prompts", r["warnings"][0])
        self.assertIn("No motion prompt for clip 3", " ".join(r["warnings"]))
        self.assertEqual(len(r["clips"]), 3)

    def test_v2_style_motion_only_block_still_parses(self):
        r = paste.parse_master("batch: c154\nprompts:\n1. locked camera, flame flickers\n2. steam rises")
        self.assertEqual([c["motion"] for c in r["clips"]], ["locked camera, flame flickers", "steam rises"])
        self.assertIn("No image prompt for clip 1, 2", " ".join(r["warnings"]))

    def test_reference_forms(self):
        self.assertEqual(paste.parse_ref("frame 2"), {"kind": "frame", "index": 2})
        self.assertEqual(paste.parse_ref("use clip 3"), {"kind": "frame", "index": 3})
        self.assertEqual(paste.parse_ref("D:/shots/av.jpg"), {"kind": "path", "path": "D:/shots/av.jpg"})
        self.assertEqual(paste.parse_ref("needed"), {"kind": "needed", "note": ""})
        self.assertEqual(paste.parse_ref("the same woman"), {"kind": "needed", "note": "the same woman"})

    def test_unknown_lines_are_reported_not_dropped(self):
        r = paste.parse_master("batch: c155\nrender these tonight please!\n1.\nimage: a lamp\nmotion: flickers")
        self.assertEqual(r["ignored"], ["render these tonight please!"])
        self.assertEqual(len(r["clips"]), 1)

    def test_missing_name_is_a_warning(self):
        r = paste.parse_master("1.\nimage: a lamp\nmotion: flickers")
        self.assertIsNone(r["name"])
        self.assertIn("No batch name found", " ".join(r["warnings"]))


class StrayLines(unittest.TestCase):
    def test_prose_after_a_blank_line_is_reported_not_glued_on(self):
        r = paste.parse_master("""batch: fmt1

image prompts:
1. a red door
2. a blue door

motion prompts:
1. it opens
2. it closes

a note to myself that is not a prompt""")
        self.assertEqual([c["motion"] for c in r["clips"]], ["it opens", "it closes"])
        self.assertEqual(r["ignored"], ["a note to myself that is not a prompt"])

    def test_a_prompt_wrapped_onto_the_next_line_still_joins(self):
        r = paste.parse_master("""batch: w1

image prompts:
1. a very long prompt that
   carries on to the next line
2. second

motion prompts:
1. it moves
2. it stops""")
        self.assertEqual(r["clips"][0]["image"], "a very long prompt that carries on to the next line")
        self.assertEqual(r["ignored"], [])


if __name__ == "__main__":
    unittest.main()
