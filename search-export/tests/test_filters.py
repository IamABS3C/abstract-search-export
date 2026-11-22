"""Every filter operator: what it matches, how it is proven, long text values, and the archive."""
import datetime as dt
import json
import unittest

from support import C, E, EngineCase, FIXED_NOW, T, spread, ids_of

import abx_ops as O

S, END = T(hours=3), T(hours=1)
LONG_URL = "https://example.com/aura?message=" + "x" * 300          # longer than the platform indexes


def cond(*leaves, groups=()):
    return {"operator": "and", "conditions": list(leaves), "groups": list(groups)}


def leaf(field, op, value=None, ftype="String", cs=False, **kw):
    out = {"field": field, "operation": op, "field_type": ftype, "case_sensitive": cs}
    if isinstance(value, list):
        out["values"] = value
    else:
        out["value"] = "" if value is None else value
    out.update(kw)
    return out


class _Filters(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.types.update({"url": "String", "path": "String", "user_name": "String", "tags": "List(String)",
                                  "dur": "Float64", "src": "Ipv4"})
        self.tenant.enum = self.tenant.enum + ["url", "path", "tags", "dur", "src"]
        self.fw = spread(self.tenant, S, END, 60, "fw")
        for i, r in enumerate(self.fw):
            r["user_name"] = ["alice@corp.com", "bob@corp.com.clouduser", "carol@partner.io", None][i % 4]
            r["url"] = (LONG_URL if i % 10 == 0 else None if i % 7 == 0 else "/" if i % 13 == 0 else
                        ["/services/data/v62", "/aura/x", "/home"][i % 3])
            r["tags"] = [["identity:employee", "prod"], ["prod"], []][i % 3]
            r["path"] = ["/", "/home", None, "-", "/aura/x"][i % 5]           # punctuation-only values, none long
            r["dur"] = float(i)
            r["src"] = ["10.1.2.3", "52.1.1.1", "8.8.8.8"][i % 3]
        spread(self.tenant, S, END, 20, "dns")

    def run_cond(self, c, name="r", fmt="ndjson"):
        code, res, run = self.export("--dataset", "fw", "--condition-json", json.dumps(c), start=S, end=END,
                                     name=name, fmt=fmt)
        return code, res, run

    def truth(self, c):
        return {r["id"] for r in self.fw if O.condition_matches(r, c, strict=True)}

    def got(self, run):
        ids = ids_of(self.merged_rows(run))
        self.assertEqual(len(ids), len(set(ids)))
        return set(ids)


class Operators(_Filters):
    def test_every_text_operator_matches_exactly(self):
        cases = [leaf("user_name", "CONTAINS", "corp.com"), leaf("user_name", "DOES_NOT_CONTAIN", "clouduser"),
                 leaf("user_name", "STARTS_WITH", "ALICE"), leaf("user_name", "DOES_NOT_START_WITH", "alice"),
                 leaf("user_name", "ENDS_WITH", ".io"), leaf("user_name", "DOES_NOT_END_WITH", ".io"),
                 leaf("user_name", "CONTAINS_WORD", "partner"), leaf("user_name", "DOES_NOT_CONTAIN_WORD", "corp"),
                 leaf("user_name", "MATCHES_REGEX", "^(alice|carol)@"), leaf("user_name", "DOES_NOT_MATCH_REGEX", "corp"),
                 leaf("user_name", "CONTAINS", "ALICE", cs=True), leaf("user_name", "IS_NOT_IN_LIST", ["alice@corp.com"]),
                 leaf("dur", "IS_GREATER_THAN_OR_EQUAL_TO", 30, "Float64"), leaf("dur", "IS_LESS_THAN_OR_EQUAL_TO", 10, "Float64"),
                 leaf("dur", "IS_IN_RANGE", "", "Float64", **{"from": "5", "to": "15"}),
                 leaf("dur", "IS_OUTSIDE_RANGE", "", "Float64", **{"from": "5", "to": "15"}),
                 leaf("src", "IS_NOT_IN_SUBNET", "10.0.0.0/8", "Ipv4"), leaf("src", "IS_NOT_RFC1918", None, "Ipv4"),
                 leaf("tags", "CONTAINS_ANY", ["identity:employee"], "List(String)"),
                 leaf("tags", "CONTAINS_ALL", ["identity:employee", "prod"], "List(String)"),
                 leaf("tags", "CONTAINS", "ident", "List(String)"), leaf("tags", "DOES_NOT_CONTAIN", "ident", "List(String)")]
        for i, lf in enumerate(cases):
            c = cond(lf)
            code, res, run = self.run_cond(c, name=f"op{i}")
            self.assertEqual(code, 0, (lf, self.log[-4:]))
            self.assertEqual(self.got(run), self.truth(c), lf)

    def test_not_operators_match_events_that_have_a_value(self):
        c = cond(leaf("user_name", "DOES_NOT_CONTAIN", "clouduser"))
        code, res, run = self.run_cond(c)
        self.assertEqual(code, 0, self.log[-4:])
        got = self.got(run)
        self.assertFalse({r["id"] for r in self.fw if r.get("user_name") is None} & got)

    def test_or_has_no_value_includes_the_empty_ones(self):
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", filters=[
            {"field": "user_name", "type": "String", "op": "DOES_NOT_CONTAIN", "value": "clouduser", "or_empty": True}]))
        c = C.build_condition(sp)
        self.assertEqual(c["groups"][0]["operator"], "or")
        code, res, run = self.run_cond(c)
        self.assertEqual(code, 0, self.log[-4:])
        want = {r["id"] for r in self.fw if r.get("user_name") is None or "clouduser" not in r["user_name"]}
        self.assertEqual(self.got(run), want)


class LongValues(_Filters):
    def test_long_values_are_matched_by_the_tool(self):
        c = cond(leaf("url", "CONTAINS", "aura?message"))
        code, res, run = self.run_cond(c)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(self.got(run), self.truth(c))
        self.assertTrue(self.got(run), "the long values must be found")
        man = self.manifest(run)
        self.assertIn("url", man["local_check"]["fields"])
        self.assertTrue(sum(x.get("checked_out", 0) for x in man["slices"]) > 0)

    def test_not_filter_on_a_field_with_long_values_is_exact(self):
        c = cond(leaf("url", "DOES_NOT_CONTAIN", "/services"))
        code, res, run = self.run_cond(c)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(self.got(run), self.truth(c))

    def test_without_the_check_long_values_are_missed(self):
        c = cond(leaf("url", "CONTAINS", "aura?message"))
        code, res, run = self.export("--dataset", "fw", "--condition-json", json.dumps(c), "--no-long-check",
                                     start=S, end=END, name="nolong")
        self.assertEqual(code, 0, self.log[-4:])
        self.assertFalse(self.got(run), "the platform alone never sees a long value")

    def test_validate_says_the_tool_filters_long_values_itself(self):
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, "test-key-0123456789abcdef")
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", format="ndjson",
                                   time={"start": E.iso(S), "end": E.iso(END)},
                                   filters=[{"field": "url", "type": "String", "op": "CONTAINS", "value": "aura?message"}]))
        v = s.validate(sp)
        self.assertTrue(v["ready"], v["checks"])
        self.assertTrue(v["plan"]["upper_bound"])
        lc = [c for c in v["checks"] if c["name"] == "Long text values"]
        self.assertTrue(lc and "url" in lc[0]["detail"], v["checks"])
        self.assertGreaterEqual(v["plan"]["total"], len(self.truth(C.build_condition(sp))))

    def test_a_field_with_no_long_values_is_filtered_by_the_server(self):
        c = cond(leaf("user_name", "CONTAINS", "corp.com"))
        code, res, run = self.run_cond(c)
        self.assertEqual(code, 0, self.log[-4:])
        self.assertFalse((self.manifest(run).get("local_check") or {}).get("fields"))
        self.assertEqual(self.got(run), self.truth(c))


class FieldTypes(_Filters):
    def test_a_list_field_is_recognised(self):
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, "test-key-0123456789abcdef")
        self.assertEqual(s.field_type("tags"), "List(String)")
        self.assertEqual(s.field_type("dur"), "Float64")


class Punctuation(_Filters):
    def test_a_punctuation_only_value_is_exact_with_every_operator(self):
        for i, c in enumerate((cond(leaf("path", "IS_EMPTY")), cond(leaf("path", "IS_NOT_EMPTY")),
                               cond(leaf("path", "DOES_NOT_CONTAIN", "/services")), cond(leaf("path", "DOES_NOT_CONTAIN", "aura")),
                               cond(leaf("path", "DOES_NOT_START_WITH", "x")), cond(leaf("path", "DOES_NOT_MATCH_REGEX", "x")),
                               cond(leaf("path", "IS_IN_LIST", ["/"], cs=True)), cond(leaf("path", "IS_IN_LIST", ["/"])),
                               cond(leaf("path", "CONTAINS", "/")))):
            code, res, run = self.run_cond(c, name=f"p{i}")
            self.assertEqual(code, 0, (c, self.log[-4:]))
            self.assertEqual(self.got(run), self.truth(c), c)
        self.assertTrue(O.leaf_matches({"u": "/"}, leaf("u", "IS_EMPTY")))
        self.assertFalse(O.leaf_matches({"u": "/"}, leaf("u", "DOES_NOT_CONTAIN", "x")))
        self.assertTrue(O.leaf_matches({"u": "/"}, leaf("u", "CONTAINS", "/")))


class Proofs(_Filters):
    def test_a_not_filter_the_platform_ignores_is_caught(self):
        self.tenant.ignore_filter = {"user_name"}
        c = cond(leaf("user_name", "DOES_NOT_CONTAIN", "clouduser"))
        code, res, run = self.run_cond(c)
        self.assertEqual(code, 2, self.log[-4:])
        self.assertIn("match both", str(res.get("error")))

    def test_a_not_filter_is_proven_exact(self):
        c = cond(leaf("user_name", "DOES_NOT_CONTAIN", "clouduser"))
        code, res, run = self.run_cond(c)
        ctl = [x for x in self.manifest(run)["preflight"]["controls"] if x.get("op") == "DOES_NOT_CONTAIN"]
        self.assertEqual([x["result"] for x in ctl], ["PASS"])


class Archive(_Filters):
    def test_not_filters_sent_to_the_archive_require_a_value(self):
        self.tenant.hot_days = 4
        self.tenant.hot_since = FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=2)
        old = spread(self.tenant, T(days=3), T(days=2, hours=12), 40, "fw")
        for i, r in enumerate(old):
            r["user_name"] = [None, "dave@corp.com", "eve@x.io.clouduser"][i % 3]
        c = cond(leaf("user_name", "DOES_NOT_CONTAIN", "clouduser"))
        code, res, run = self.export("--dataset", "fw", "--condition-json", json.dumps(c), start=T(days=3), end=END,
                                     name="arch")
        self.assertEqual(code, 0, self.log[-6:])
        sent = [j["condition"] for j in self.tenant.jobs.values()]
        self.assertTrue(sent)
        for sc in sent:
            ops = [(x["field"], x["operation"]) for x in E.all_leaves(sc)]
            self.assertIn(("user_name", "IS_NOT_EMPTY"), ops)
        want = {r["id"] for r in self.fw + old if O.condition_matches(r, c, strict=True)}
        self.assertEqual(self.got(run), want)


class FilterRows(unittest.TestCase):
    def test_range_and_regex_problems(self):
        p = C.filter_problem({"field": "dur", "type": "Float64", "op": "IS_IN_RANGE", "value": "5"})
        self.assertIn("two numbers", p)
        p = C.filter_problem({"field": "dur", "type": "Float64", "op": "IS_IN_RANGE", "value": "9,5"})
        self.assertIn("larger", p)
        p = C.filter_problem({"field": "u", "type": "String", "op": "MATCHES_REGEX", "value": "(unclosed"})
        self.assertIn("not a valid regex", p)
        self.assertIsNone(C.filter_problem({"field": "u", "type": "String", "op": "CONTAINS", "value": "x"}))

    def test_range_leaf_carries_from_and_to(self):
        c = C.filter_leaf({"field": "dur", "type": "Float64", "op": "IS_IN_RANGE", "value": "5, 15"})
        self.assertEqual((c["from"], c["to"]), ("5", "15"))

    def test_every_operator_has_a_label_and_value_kind(self):
        sample = {"String": ("abc", "a"), "List(String)": (["abc"], "abc"), "Ipv4": ("10.1.2.3", "10.0.0.0/8"),
                  "Float64": (1.0, "1"), "Boolean": (True, "")}
        for t, ops in C.OPERATORS.items():
            row_value, v = sample[t]
            for op, label, kind in ops:
                self.assertTrue(label)
                self.assertIn(kind, (True, False, None, "range"))
                lf = {"field": "f", "operation": op, "field_type": t, "value": v, "values": [v], "from": "0", "to": "2"}
                self.assertIsNotNone(O.leaf_matches({"f": row_value}, lf), (t, op))


if __name__ == "__main__":
    unittest.main()


import contextlib
import io
import os

import abx_kql as K
import abstract_search as A
from support import KEY

TYPES = {"user_name": "String", "action": "String", "url": "String", "tags": "List(String)", "dur": "Float64",
         "src": "Ipv4", "data_stream.dataset": "String", "@timestamp": "Date"}


class Converter(unittest.TestCase):
    def conv(self, q):
        return K.convert(q, lambda f: TYPES.get(f, "String"), set(TYPES))

    def test_every_kql_text_operator(self):
        cases = {'user_name contains "corp"': ("CONTAINS", False), 'user_name contains_cs "Corp"': ("CONTAINS", True),
                 'user_name !contains "corp"': ("DOES_NOT_CONTAIN", False), 'user_name !contains_cs "C"': ("DOES_NOT_CONTAIN", True),
                 'user_name has "corp"': ("CONTAINS_WORD", False), 'user_name !has "corp"': ("DOES_NOT_CONTAIN_WORD", False),
                 'user_name startswith "a"': ("STARTS_WITH", False), 'user_name !startswith "a"': ("DOES_NOT_START_WITH", False),
                 'user_name endswith ".io"': ("ENDS_WITH", False), 'user_name !endswith_cs ".IO"': ("DOES_NOT_END_WITH", True),
                 'user_name matches regex "^a.*"': ("MATCHES_REGEX", False), 'user_name =~ "A@B"': ("IS_IN_LIST", False),
                 'user_name !in ("a", "b")': ("IS_NOT_IN_LIST", True), 'dur >= 5': ("IS_GREATER_THAN_OR_EQUAL_TO", False),
                 'dur <= 5': ("IS_LESS_THAN_OR_EQUAL_TO", False), 'dur between (1 .. 10)': ("IS_IN_RANGE", False),
                 'dur !between (1 .. 10)': ("IS_OUTSIDE_RANGE", False), 'dur != 3': ("IS_NOT_IN_LIST", False),
                 'src != "10.1.1.1"': ("IS_NOT_IN_LIST", False), 'tags contains "ident"': ("CONTAINS", False),
                 'tags == "prod"': ("CONTAINS_ANY", True)}
        for q, (op, cs) in cases.items():
            f = self.conv("events | where " + q)["filters"]
            self.assertEqual(len(f), 1, q)
            self.assertEqual(f[0]["op"], op, q)
            self.assertEqual(bool(f[0].get("case_sensitive")), cs, q)
            self.assertIsNone(C.filter_problem(f[0]), q)

    def test_hasprefix_and_hassuffix_need_no_anchor_inside_a_group(self):
        g = self.conv('events | where user_name hasprefix "car"')["any_groups"]
        self.assertEqual([(f["op"], f["value"]) for f in g[0]], [("STARTS_WITH", "car"), ("MATCHES_REGEX", "[^A-Za-z0-9]car")])
        f = self.conv('events | where user_name !hassuffix "io"')["filters"]
        self.assertEqual([(x["op"], x["value"]) for x in f], [("DOES_NOT_END_WITH", "io"), ("DOES_NOT_MATCH_REGEX", "io[^A-Za-z0-9]")])

    def test_or_becomes_an_any_group(self):
        r = self.conv('events | where data_stream.dataset == "fw" and (user_name contains "a" or action == "x")')
        self.assertEqual(r["dataset"], "fw")
        self.assertEqual([[x["op"] for x in g] for g in r["any_groups"]], [["CONTAINS", "IS_IN_LIST"]])
        r = self.conv('events | where user_name has_any ("a", "b")')
        self.assertEqual([[x["op"] for x in g] for g in r["any_groups"]], [["CONTAINS_WORD", "CONTAINS_WORD"]])
        r = self.conv('events | where user_name has_all ("a", "b")')
        self.assertEqual([x["op"] for x in r["filters"]], ["CONTAINS_WORD", "CONTAINS_WORD"])

    def test_the_customer_search_converts_and_says_what_not_means(self):
        r = self.conv('events | where [\'data_stream.dataset\'] == "CRM - Audit Events" and action == '
                      '"ContentTransfer" and user_name !contains "example.com" | sort by [\'@timestamp\'] desc')
        self.assertEqual(r["dataset"], "CRM - Audit Events")
        self.assertEqual([(f["field"], f["op"]) for f in r["filters"]],
                         [("action", "IS_IN_LIST"), ("user_name", "DOES_NOT_CONTAIN")])
        self.assertTrue(any("or has no value" in n for n in r["notes"]))

    def test_command_line_values_need_no_quotes(self):
        conv = lambda w: K.convert("events | where " + w, lambda f: TYPES.get(f, "String"), set(TYPES), bare=True)
        cases = {"url endswith .clouduser": ("ENDS_WITH", ".clouduser"), "url contains aura?message": ("CONTAINS", "aura?message"),
                 "url !contains /services": ("DOES_NOT_CONTAIN", "/services"), "url matches regex ^a-\\d+": ("MATCHES_REGEX", "^a-\\d+"),
                 'user_name == "a == b"': ("IS_IN_LIST", "a == b"), "src == 10.1.2.3": ("IS_IN_LIST", "10.1.2.3"),
                 "dur >= 5": ("IS_GREATER_THAN_OR_EQUAL_TO", "5"), 'user_name in (a@x.com, "b c")': ("IS_IN_LIST", "a@x.com,b c")}
        for w, (op, value) in cases.items():
            f = conv(w)["filters"][0]
            self.assertEqual((f["op"], f["value"]), (op, value), w)
        with self.assertRaises(K.ConvertError):          # a pasted search still needs quotes, as in Abstract
            self.conv("events | where url endswith .clouduser")

    def test_not_flips_each_operator(self):
        r = self.conv('events | where not(user_name contains "corp" or isempty(url))')
        self.assertEqual([(f["op"], f["field"]) for f in r["filters"]],
                         [("DOES_NOT_CONTAIN", "user_name"), ("IS_NOT_EMPTY", "url")])

    def test_too_many_alternatives_is_refused(self):
        q = "events | where " + " or ".join(f'(user_name == "{i}" and url == "{i}")' for i in range(7))
        with self.assertRaises(K.ConvertError) as cm:
            self.conv(q)
        self.assertIn("too many groups", str(cm.exception))


class ConvertedSearches(_Filters):
    def conv(self, q):
        return K.convert(q, lambda f: TYPES.get(f, "String"), set(TYPES))

    def test_nested_and_or_converts_exactly(self):
        q = 'events | where (user_name == "alice@corp.com" and url == "/home") or user_name =~ "CAROL@partner.io"'
        r = self.conv(q)
        self.assertEqual(len(r["any_groups"]), 2)
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", filters=r["filters"], any_groups=r["any_groups"]))
        c = C.build_condition(sp)
        want = {x["id"] for x in self.fw if (x.get("user_name") == "alice@corp.com" and x.get("url") == "/home")
                or (x.get("user_name") or "").lower() == "carol@partner.io"}
        self.assertEqual(self.truth(c), want)
        code, res, run = self.run_cond(c)
        self.assertEqual(code, 0, self.log[-4:])
        self.assertEqual(self.got(run), want)


class CommandLineConditions(_Filters):
    def setUp(self):
        super().setUp()
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example",
                           "ABX_OUT_ROOT": self.home})
        self._home = C.home_dir
        C.home_dir = lambda: self.home

    def tearDown(self):
        C.home_dir = self._home
        for k in ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL"):
            os.environ.pop(k, None)
        super().tearDown()

    def run_cli(self, *argv):
        os.environ["ABSTRACT_API_KEY"] = KEY
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = A.main(list(argv))
        return code, out.getvalue()

    def test_preview_and_export_take_a_condition(self):
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        want = {r["id"] for r in self.fw if r.get("user_name") and "clouduser" not in r["user_name"]}
        code, out = self.run_cli("preview", "--dataset", "fw", "--where", 'user_name !contains "clouduser"', "--json", *win)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["total"], len(want))
        code, out = self.run_cli("export", "--dataset", "fw", "--where", 'user_name !contains "clouduser"',
                                 "--out", os.path.join(self.home, "cli1"), "--format", "ndjson", "--merge", "--quiet", *win)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.got(os.path.join(self.home, "cli1")), want)

    def test_old_and_new_where_mix(self):
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        code, out = self.run_cli("export", "--dataset", "fw", "--where", "user_name=alice@corp.com,carol@partner.io",
                                 "--where", 'user_name endswith ".io"', "--out", os.path.join(self.home, "cli2"),
                                 "--format", "ndjson", "--merge", "--quiet", *win)
        self.assertEqual(code, 0, out)
        want = {r["id"] for r in self.fw if r.get("user_name") == "carol@partner.io"}
        self.assertEqual(self.got(os.path.join(self.home, "cli2")), want)

    def test_a_bad_condition_is_refused_by_name(self):
        code, out = self.run_cli("preview", "--dataset", "fw", "--where", 'user_name !contains', "--json",
                                 "--last", "1h")
        self.assertEqual(code, 2)

    def test_unquoted_values_work_on_every_shell(self):
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        want = {r["id"] for r in self.fw if r.get("user_name") and "clouduser" not in r["user_name"]
                and r.get("src") != "8.8.8.8"}
        code, out = self.run_cli("preview", "--dataset", "fw", "--where", "user_name !contains clouduser",
                                 "--where", "src != 8.8.8.8", "--json", *win)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["total"], len(want))

    def test_match_any_over_conditions_is_exact(self):
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        args = ["--dataset", "fw", "--where", 'user_name endswith ".io" and url == "/home"',
                "--where", "user_name=alice@corp.com", "--match", "any"]
        want = {r["id"] for r in self.fw if ((r.get("user_name") or "").endswith(".io") and r.get("url") == "/home")
                or (r.get("user_name") or "").lower() == "alice@corp.com"}
        code, out = self.run_cli("preview", *args, "--json", *win)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["total"], len(want))
        code, out = self.run_cli("export", *args, "--out", os.path.join(self.home, "cli3"), "--format", "ndjson",
                                 "--merge", "--quiet", *win)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.got(os.path.join(self.home, "cli3")), want)


class Groups(unittest.TestCase):
    def test_match_any_keeps_the_groups_as_well(self):
        sp = C.normalize_spec(dict(C.empty_spec(), match="any", filters=[
            {"field": "a", "type": "String", "op": "IS_IN_LIST", "value": "1"},
            {"field": "b", "type": "String", "op": "IS_IN_LIST", "value": "2"}],
            any_groups=[[{"field": "c", "type": "String", "op": "IS_IN_LIST", "value": "3"},
                         {"field": "d", "type": "String", "op": "IS_IN_LIST", "value": "4"}]]))
        c = C.build_condition(sp)
        row = lambda **kw: kw
        self.assertTrue(O.condition_matches(row(a="1", c="3"), c))
        self.assertFalse(O.condition_matches(row(a="1"), c), "the group still applies")
        self.assertFalse(O.condition_matches(row(c="3"), c), "one of the filters must match")

    def test_either_of_is_exact(self):
        f = lambda n: {"field": n, "type": "String", "op": "IS_IN_LIST", "value": "1"}
        filters, groups = C.either_of([([f("a"), f("b")], []), ([f("c")], [])])
        self.assertEqual(filters, [])
        self.assertEqual(groups, [[f("a"), f("c")], [f("b"), f("c")]])
