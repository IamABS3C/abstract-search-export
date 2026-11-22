"""The coverage gate (tools/coverage_matrix.py) catches what it is for: an undocumented screen, a missing screenshot,
a command with no example, a product item missing from the capability map, and a stale map. It runs on the source
tree only (the guide, the screenshots and tools/ are not in the shipped kit)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(HERE, "tools", "coverage_matrix.py")
SOURCE = os.path.isfile(TOOL) and os.path.isfile(os.path.join(HERE, "GUIDE.md"))


@unittest.skipUnless(SOURCE, "the coverage gate runs on the source tree, not on the shipped kit")
class CoverageGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        os.environ.setdefault("ABX_OUT_ROOT", tempfile.mkdtemp(prefix="abx-test-cov-"))   # never the real home
        sys.path.insert(0, os.path.join(HERE, "tools"))
        import coverage_matrix
        cls.M = coverage_matrix
        cls.items = coverage_matrix.inventory()
        cls.docs = coverage_matrix.corpus()

    def gaps(self, docs=None, items=None):
        return {r["id"] for r in self.M.check(items or self.items, docs or self.docs) if not r["ok"]}

    def test_the_real_tree_has_no_gap(self):
        self.assertEqual(self.gaps(), set())
        self.assertEqual(self.M.map_problems(self.items), [])
        self.assertEqual(self.M.link_problems(), [])

    def test_an_undocumented_tab_is_caught(self):
        docs = {k: v.replace("**Export history**", "Export history") for k, v in self.docs.items()}
        self.assertIn("tab:exports", self.gaps(docs))

    def test_a_screenshot_nobody_shows_is_caught(self):
        docs = dict(self.docs)
        for f in ("GUIDE.md", "README.md"):
            docs[f] = docs[f].replace("web-28-help.png", "web-28-gone.png")
        self.assertIn("tab:help", self.gaps(docs))

    def test_a_command_with_no_example_is_caught(self):
        docs = {k: v.replace("abstract_search.py endpoints", "abstract_search.py  ") for k, v in self.docs.items()}
        self.assertIn("cli:endpoints", self.gaps(docs))

    def test_a_new_product_item_must_be_documented_and_mapped(self):
        planted = {"id": "tab:planted", "kind": "browser tab", "label": "Planted", "visual": True}
        self.assertIn("tab:planted", self.gaps(items=self.items + [planted]))
        self.assertTrue(any("tab:planted" in p for p in self.M.map_problems(self.items + [planted])))

    def test_a_renamed_label_makes_the_map_stale(self):
        renamed = [dict(i, label="Exports and downloads") if i["id"] == "tab:exports" else i for i in self.items]
        self.assertNotEqual(self.M.render_map(renamed, "guide"), self.M.render_map(self.items, "guide"))
        self.assertEqual(self.M.map_blocks(self.items), [])


if __name__ == "__main__":
    unittest.main()
