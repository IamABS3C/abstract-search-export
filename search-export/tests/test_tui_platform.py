"""The guided terminal's plain English, KQL, fieldset and saved-view screens, with the prompts scripted."""
import contextlib
import io
import unittest

from support import C, EngineCase, KEY, T, spread

import abx_tui_platform as TP
import abx_ui as U

S, END = T(hours=3), T(hours=1)


class _Menu:
    def __init__(self, s):
        self.s, self.spec = s, C.empty_spec()


class _Base(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.types.update({"user_name": "String", "action": "String"})
        for i, r in enumerate(spread(self.tenant, S, END, 40, "fw")):
            r["action"] = ["Login", "Logout"][i % 2]
        self.s = C.Session()
        self.s.connect("https://api.fake.example", self.tenant.vid, KEY)
        self.menu = _Menu(self.s)
        self._real = (U.ask, U.confirm, U.select, U.pause, U.page)
        U.pause = lambda *a, **k: None
        U.page = lambda *a, **k: None

    def tearDown(self):
        U.ask, U.confirm, U.select, U.pause, U.page = self._real
        super().tearDown()

    def script(self, asks=(), confirms=(), selects=()):
        a, c, s = iter(asks), iter(confirms), iter(selects)
        U.ask = lambda *x, **k: next(a)
        U.confirm = lambda *x, **k: next(c)
        U.select = lambda *x, **k: next(s)


class Wizard(_Base):
    def test_english_becomes_a_checked_search_only_after_a_yes(self):
        self.tenant.nl_answers["fw logins"] = {"kql": 'events | where data_stream.dataset == "fw" and action == "Login"',
                                               "explanation": "Logins in fw."}
        self.script(asks=["fw logins"], confirms=[True])
        with contextlib.redirect_stdout(io.StringIO()):
            c = TP.check_for_wizard(self.s, "english")
        sp = TP.spec_from_check(c)
        self.assertEqual((sp["dataset"], sp["filters"][0]["field"]), ("fw", "action"))

    def test_a_result_table_query_is_not_offered_as_an_export(self):
        self.script(asks=["events | summarize n = count() by action", ""], confirms=[])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            c = TP.check_for_wizard(self.s, "kql")
        self.assertIsNone(c)
        self.assertIn("reshapes the rows", out.getvalue())


class Fieldsets(_Base):
    def test_build_review_and_create(self):
        self.script(asks=["My columns", "action, ext.role"], selects=["new", "type", "done", None],
                    confirms=[True])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            TP.do_fieldsets(self.menu)
        self.assertIn("POST /v1/streamviewer/field-set", out.getvalue())
        (f,) = self.tenant.fieldsets.values()
        self.assertEqual(f["fields"], ["@timestamp", "action", "ext.role"])

    def test_saying_no_changes_nothing(self):
        self.script(asks=["Nope"], selects=["new", "done", None], confirms=[False])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            TP.do_fieldsets(self.menu)
        self.assertEqual(self.tenant.fieldsets, {})
        self.assertIn("Nothing was changed", out.getvalue())


class Views(_Base):
    def test_save_the_current_search_as_a_view(self):
        self.menu.spec = C.normalize_spec(dict(C.empty_spec(), dataset="fw", time={"last": "24h"}))
        self.script(asks=["fw everything"], selects=["save"], confirms=[True])
        with contextlib.redirect_stdout(io.StringIO()):
            TP.view_actions(self.menu, [])
        self.assertEqual([v["name"] for v in self.tenant.views], ["fw everything"])


class ContentAndSchedule(_Base):
    def test_export_then_apply_a_bundle_with_a_review(self):
        import os
        self.tenant.fieldsets["fsX"] = {"id": "fsX", "name": "Cols", "fields": ["@timestamp", "action"],
                                        "share_level": "private", "updated_at": "2026-11-01T00:00:00Z"}
        d = os.path.join(self.home, "b")
        self.script(asks=[d], selects=["export"])
        with contextlib.redirect_stdout(io.StringIO()):
            TP.do_content(self.menu)
        self.assertTrue(os.path.isfile(os.path.join(d, "fieldsets", "cols.json")))
        self.tenant.fieldsets.clear()
        self.script(asks=[d], selects=["apply"], confirms=[True])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            TP.do_content(self.menu)
        self.assertIn("1 create", out.getvalue())
        self.assertEqual([f["name"] for f in self.tenant.fieldsets.values()], ["Cols"])

    def test_schedule_the_current_search(self):
        import os
        self.menu.spec = C.normalize_spec(dict(C.empty_spec(), name="fw", dataset="fw"))
        d = os.path.join(self.home, "auto")
        self.script(asks=[d], selects=["systemd", "1h"])
        with contextlib.redirect_stdout(io.StringIO()):
            TP.do_automate(self.menu)
        self.assertTrue(os.path.isfile(os.path.join(d, "run-export.sh")))
        self.assertTrue(any(f.endswith(".timer") for f in os.listdir(d)))


class LaunchAtStart(_Base):
    def _run_menu(self, tty):
        import abx_menu
        calls = []
        saved = (U._LAUNCH[0], U.STDOUT_TTY, U.CURSOR, U.COLOR_LEVEL, U.PLAIN, U.NO_COLOR, U.rocket_launch, U.header)
        U._LAUNCH[0] = True                                   # what ABX_LAUNCH=1 sets at import
        U.STDOUT_TTY, U.CURSOR, U.COLOR_LEVEL, U.PLAIN, U.NO_COLOR = tty, True, 3, False, False
        U.rocket_launch = lambda *a, **k: calls.append(1)
        U.header = lambda *a, **k: None

        def stop(*a, **k):
            raise U.EndOfInput()
        U.select = stop
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                abx_menu.Menu(self.s).run()
        finally:
            (U._LAUNCH[0], U.STDOUT_TTY, U.CURSOR, U.COLOR_LEVEL, U.PLAIN, U.NO_COLOR, U.rocket_launch,
             U.header) = saved
        return calls

    def test_abx_launch_plays_once_on_a_terminal_and_never_when_piped(self):
        self.assertEqual(self._run_menu(tty=True), [1])
        self.assertEqual(self._run_menu(tty=False), [])


if __name__ == "__main__":
    unittest.main()
