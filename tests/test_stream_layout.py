"""Regression guard for the Stream rail layout (gui/style.css).

The rails used to be a horizontal flex strip with `overflow-x: auto`, so a
folder holding 120 files rendered as one endless band behind a scrollbar.
jsdom has no layout engine, so the DOM tests cannot catch that class of bug --
these assertions read the stylesheet directly instead.

They are deliberately structural (grid vs. scroll container, fluid vs. fixed
cards) rather than pixel-based, so normal visual tweaks do not break them.
"""

import os
import re
import unittest

STYLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui", "style.css"
)


def rule_block(css, selector):
    """Return the declaration block for a top-level `selector { ... }` rule."""
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{", css)
    if not match:
        return ""
    start = match.end()
    end = css.find("}", start)
    return css[start:end] if end != -1 else ""


class TestStreamRailLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(STYLE_PATH, encoding="utf-8") as f:
            cls.css = f.read()

    def test_the_rules_under_test_exist(self):
        # Guards the guard: a renamed selector would make every test below
        # pass vacuously against an empty string.
        self.assertTrue(rule_block(self.css, ".stream-rail-track"))
        self.assertTrue(rule_block(self.css, ".stream-card"))

    def test_rail_track_is_a_wrapping_grid(self):
        block = rule_block(self.css, ".stream-rail-track")
        self.assertIn("display: grid", block)
        self.assertIn("repeat(auto-fill", block)

    def test_rail_track_is_not_a_horizontal_scroll_container(self):
        block = rule_block(self.css, ".stream-rail-track")
        self.assertNotIn("overflow-x", block)
        self.assertNotIn("scroll-snap", block)
        self.assertNotIn("display: flex", block)

    def test_the_horizontal_scrollbar_styling_is_gone(self):
        self.assertNotIn(".stream-rail-track::-webkit-scrollbar", self.css)

    def test_cards_are_fluid_so_the_grid_controls_their_width(self):
        block = rule_block(self.css, ".stream-card")
        self.assertNotIn("flex: 0 0 auto", block)
        self.assertIsNone(
            re.search(r"width:\s*\d+px", block),
            "a fixed card width would fight the grid track sizing",
        )

    def test_the_grid_cannot_overflow_a_narrow_container(self):
        block = rule_block(self.css, ".stream-rail-track")
        self.assertIn(
            "min(", block,
            "minmax() needs a min() floor, or a container narrower than one "
            "card overflows horizontally",
        )


if __name__ == "__main__":
    unittest.main()
