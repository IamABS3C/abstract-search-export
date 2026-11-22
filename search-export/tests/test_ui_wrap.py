"""The terminal pager wraps by what the eye sees: colour codes take no room, so coloured bars and tables are not
broken early (textwrap counted every escape character)."""
import unittest

import support  # noqa: F401  (puts the kit on the path)

import abx_ui as U

BAR = "\x1b[38;2;1;230;157m" + "█" * 20 + "\x1b[0m"


class WrapVisible(unittest.TestCase):
    def test_a_coloured_line_that_fits_on_screen_is_not_wrapped(self):
        line = "  user_name" + " " * 9 + BAR + "  98.5%"
        self.assertLessEqual(U.vlen(line), 60)
        self.assertGreater(len(line), 60)                       # textwrap would have broken it
        self.assertEqual(U.wrap_visible(line, 60), [line])

    def test_a_long_coloured_line_wraps_at_the_visible_width_and_keeps_the_bar_whole(self):
        line = ("word " * 10) + BAR + (" more" * 10)
        parts = U.wrap_visible(line, 40)
        self.assertTrue(all(U.vlen(p) <= 40 for p in parts), [U.vlen(p) for p in parts])
        self.assertEqual(sum(1 for p in parts if "█" * 20 in p), 1)
        self.assertEqual("".join(p.replace(" ", "") for p in parts).count("█"), 20)

    def test_a_word_longer_than_the_line_is_split(self):
        parts = U.wrap_visible("x" * 95, 40)
        self.assertEqual([U.vlen(p) for p in parts], [40, 40, 15])


if __name__ == "__main__":
    unittest.main()
