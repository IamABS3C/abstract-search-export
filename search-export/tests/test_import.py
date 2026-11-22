"""Using a search from Abstract (saved view or pasted search-box text): converted to dataset + filters, never sent as
KQL; plus CSV columns that match the chosen fields, and the kit storing no tenant data."""
import csv
import json
import os
import unittest

import abx_kql as K
from support import C, E, EngineCase, KEY, T, spread

S, END = T(hours=3), T(hours=1)
TYPES = {"user_name": "String", "action": "String", "data_stream.dataset": "String", "type": "String",
         "message": "String", "dest_port": "Float64", "source_ipv4": "Ipv4", "is_admin": "Boolean"}
ftype = TYPES.get


def ids_in(path):
    with open(path, encoding="utf-8") as fh:
        return {json.loads(line)["id"] for line in fh}


class Converter(unittest.TestCase):
    """Pure conversion. The query shapes follow real saved views; every value is invented."""

    def conv(self, q):
        return K.convert(q, lambda f: TYPES.get(f, "String"))

    def test_saved_view_shape(self):
        # The shape of a real saved view (dataset in bracket quotes, two == filters, a sort), with invented values.
        r = self.conv("events | where ['data_stream.dataset'] == \"crm - audit events\" and "
                      "user_name == \"a.person@example.com\" and action == \"ContentTransfer\" | "
                      "sort by ['@timestamp'] desc")
        self.assertEqual(r["dataset"], "crm - audit events")
        self.assertEqual([(f["field"], f["op"], f["value"], f["case_sensitive"]) for f in r["filters"]],
                         [("user_name", "IS_IN_LIST", "a.person@example.com", True),
                          ("action", "IS_IN_LIST", "ContentTransfer", True)])
        self.assertTrue(any("sort" in n for n in r["notes"]))

    def test_case_rules_mirror_kql(self):
        r = self.conv('events | where action =~ "x" and user_name in~ ("a", "b") and type != "y" and type !~ "z"')
        self.assertEqual([f["case_sensitive"] for f in r["filters"]], [False, False, True, False])

    def test_time_bounds_set_the_range(self):
        r = self.conv("events | where ['@timestamp'] > datetime(2026-09-01T12:00:00Z) and action == \"x\" and "
                      "['@timestamp'] <= datetime(\"2026-09-02 00:00\")")
        self.assertEqual(r["time"], {"start": "2026-09-01T12:00:00Z", "end": "2026-09-02T00:00:00Z"})

    def test_types_drive_the_filter(self):
        r = self.conv('events | where dest_port in (22, 3389) and ipv4_is_in_range(source_ipv4, "10.0.0.0/8") '
                      'and is_admin == true and dest_port > 1024 and isnotempty(user_name) | project user_name, action')
        self.assertEqual([(f["field"], f["type"], f["op"]) for f in r["filters"]],
                         [("dest_port", "Float64", "IS_IN_LIST"), ("source_ipv4", "Ipv4", "IS_IN_SUBNET"),
                          ("is_admin", "Boolean", "IS_TRUE"), ("dest_port", "Float64", "IS_GREATER_THAN"),
                          ("user_name", "String", "IS_NOT_EMPTY")])
        self.assertEqual(r["fields"], ["user_name", "action"])

    def test_refusals_name_the_clause(self):
        for q, word in [('events | where strlen(action) > 3', "strlen"),
                        ('events | where not(@timestamp > datetime(2026-01-01))', "not"),
                        ('events | where action == "x" | take 10', "take"),
                        ('events | summarize count() by action', "summarize"),
                        ('events | where action > 5', "number"),
                        ('events | where source_ipv4 == "nope"', "IPv4"),
                        ('events | where user_name startswith "  ad"', "space")]:
            with self.assertRaises(K.ConvertError, msg=q) as cm:
                self.conv(q)
            self.assertIn(word, str(cm.exception), q)

    def test_unknown_field_is_refused(self):
        with self.assertRaises(K.ConvertError) as cm:
            K.convert('events | where no_such == "x"', ftype, set(TYPES))
        self.assertIn("no_such", str(cm.exception))

    def test_escapes_and_bracket_quotes(self):
        r = self.conv('events | where ["user_name"] == "a \\"quoted\\" name" and [\'action\'] == \'it\\\'s\'')
        self.assertEqual([f["value"] for f in r["filters"]], ['a "quoted" name', "it's"])

    def test_condition_shape(self):
        cond = K.to_condition([{"field": "action", "type": "String", "op": "IS_IN_LIST", "value": "a,b",
                                "case_sensitive": True}], dataset="fw")
        self.assertEqual(cond["conditions"][0]["values"], ["fw"])
        self.assertEqual(cond["conditions"][1], {"field": "action", "operation": "IS_IN_LIST", "field_type": "String",
                                                 "case_sensitive": True, "values": ["a", "b"]})


class _Tenant(EngineCase):
    def setUp(self):
        super().setUp()
        self.fw = spread(self.tenant, S, END, 60, "fw", action="Login")
        for i, r in enumerate(self.fw):
            if i % 3 == 0:
                r["action"] = "Logout"
            if i % 4 == 0:
                r["action"] = "login"                         # lower case: an exact-case == must not match it
        spread(self.tenant, S, END, 40, "dns", action="Login")

    def connect(self):
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, KEY)
        return s

    def want(self, action="Login", dataset="fw"):
        return {r["id"] for r in self.tenant.rows
                if r["data_stream"]["dataset"] == dataset and r.get("action") == action}


class SessionImport(_Tenant):
    def test_pasted_search_exports_exactly_what_it_says(self):
        s = self.connect()
        text = 'events | where [\'data_stream.dataset\'] == "fw" and action == "Login" | sort by [\'@timestamp\'] desc'
        r = s.import_search(text=text)
        sp = dict(r["spec"], time={"start": E.iso(S), "end": E.iso(END)}, format="ndjson")
        self.assertEqual(sp["dataset"], "fw")
        s.start_export(sp)
        res = s.wait(poll=0.01)["result"]
        self.assertEqual(res["exit_code"], 0, res)
        got = ids_in(os.path.join(res["run_dir"], res["merged_file"]))
        self.assertEqual(got, self.want("Login"))                 # exact case: "login" rows are excluded
        self.assertFalse(any(c[1] == "/v1/dataview/search" and "kql_query" in (c[2] or {}) for c in self.tenant.calls),
                         "KQL was sent to the API")

    def test_saved_view_is_read_live_and_never_stored(self):
        self.tenant.views = [{"id": "v1", "name": "Logins", "kql_query": 'events | where action =~ "login"'},
                             {"id": "v2", "name": "no search", "kql_query": None}]
        s = self.connect()
        vs = s.views()
        self.assertEqual([(v["name"], v["origin"]) for v in vs], [("Logins", "kql"), ("no search", "none")])
        r = s.import_search(view_id="v1")
        self.assertEqual(r["source"], "Logins")
        self.assertFalse(r["spec"]["filters"][0]["case_sensitive"])
        for root, _, files in os.walk(self.home):
            for f in files:
                with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                    self.assertNotIn("Logins", fh.read(), os.path.join(root, f))

    def test_platform_validator_refusal_is_mirrored(self):
        self.tenant.kql_violations = [{"field": "message", "kind": "text_field_operator",
                                       "message": "'!=' is refused on a free-text field"}]
        s = self.connect()
        with self.assertRaises(E.UserError) as cm:
            s.import_search(text='events | where message != "x"')
        self.assertIn("rejects this query", str(cm.exception))

    def test_unconvertible_search_is_refused_by_name(self):
        s = self.connect()
        with self.assertRaises(E.UserError) as cm:
            s.import_search(text='events | where strlen(user_name) > 3')
        self.assertIn("strlen", str(cm.exception))


class CliImport(_Tenant):
    def test_from_file_and_from_view(self):
        path = os.path.join(self.home, "search.kql")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('events | where data_stream.dataset == "fw" and action == "Logout"\n')
        code, res, run = self.export("--from-file", path, start=S, end=END, name="f")
        self.assertEqual(code, 0, self.log[-6:])
        got = ids_in(os.path.join(run, res["merged_file"]))
        self.assertEqual(got, self.want("Logout"))
        self.tenant.views = [{"id": "vw", "name": "Logouts", "kql_query": 'events | where data_stream.dataset == "fw" and action == "Logout"'}]
        code, res, run = self.export("--from-view", "logouts", start=S, end=END, name="v")
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], len(self.want("Logout")))

    def test_from_view_cannot_be_mixed_with_a_dataset(self):
        self.tenant.views = [{"id": "vw", "name": "x", "kql_query": 'events | where action == "Login"'}]
        code, res, _ = self.export("--from-view", "x", "--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 2)
        self.assertIn("already hold the whole search", res["error"])


class CsvColumns(_Tenant):
    def test_csv_header_is_exactly_the_chosen_fields(self):
        code, res, run = self.export("--dataset", "fw", "--fields", "action,user_name", start=S, end=END, fmt="csv")
        self.assertEqual(code, 0, self.log[-6:])
        with open(os.path.join(run, res["merged_file"]), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["@timestamp", "id", "action", "user_name"])
        self.assertEqual(len(rows) - 1, len([r for r in self.tenant.rows if r["data_stream"]["dataset"] == "fw"]))

    def test_dataset_column_only_when_chosen(self):
        code, res, run = self.export("--dataset", "fw", "--fields", "user_name,data_stream.dataset,action",
                                     start=S, end=END, fmt="csv", name="c2")
        with open(os.path.join(run, res["merged_file"]), encoding="utf-8-sig", newline="") as fh:
            self.assertEqual(next(csv.reader(fh)), ["@timestamp", "id", "user_name", "data_stream.dataset", "action"])


class ExtFields(_Tenant):
    """ext.* fields are not in the tenant's field catalog, but they are learned one by one and export by name."""
    def setUp(self):
        super().setUp()
        self.tenant.enum = self.tenant.enum + ["ext"]
        for i, r in enumerate(self.fw):
            if i % 2 == 0:
                r["ext"] = {"vendor": {"rule_id": "r-%d" % i, "verdict": "allow"}}

    def test_learn_fields_lists_each_ext_field(self):
        p = self.connect().dataset_fields("fw", refresh=True)
        names = {f["name"] for f in p["fields"]}
        self.assertIn("ext.vendor.rule_id", names)
        self.assertIn("ext.vendor.verdict", names)
        self.assertNotIn("ext", names)

    def test_ext_fields_export_as_csv_columns(self):
        code, res, run = self.export("--dataset", "fw", "--fields", "ext.vendor.rule_id,action",
                                     start=S, end=END, fmt="csv", name="ext1")
        self.assertEqual(code, 0, res)
        with open(os.path.join(run, res["merged_file"]), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["@timestamp", "id", "ext.vendor.rule_id", "action"])
        self.assertEqual(sum(1 for r in rows[1:] if r[2].startswith("r-")), sum(1 for r in self.fw if r.get("ext")))

    def test_every_field_csv_has_every_acs_and_ext_column(self):
        code, res, run = self.export("--dataset", "fw", "--every-field", start=S, end=END, fmt="csv", name="ext2")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["merged_file"], "export.csv")
        with open(os.path.join(run, res["merged_file"]), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), len(self.fw))
        head = list(rows[0].keys())
        self.assertEqual(head[:2], ["@timestamp", "id"])
        for col in ("action", "data_stream.dataset", "ext.vendor.rule_id", "ext.vendor.verdict"):
            self.assertIn(col, head)
        self.assertLess(head.index("action"), head.index("ext.vendor.rule_id"))      # ACS first, then ext
        self.assertEqual(sum(1 for r in rows if r["ext.vendor.rule_id"]), sum(1 for r in self.fw if r.get("ext")))
        self.assertFalse([f for f in os.listdir(run) if f.startswith("part-")])         # parts removed after merge


class NoTenantDataStored(_Tenant):
    def test_field_profiles_stay_in_memory(self):
        s = self.connect()
        p = s.dataset_fields("fw", refresh=True)
        self.assertTrue(p["sampled"])
        self.assertTrue(s.dataset_fields("fw")["cached"])
        written = [os.path.join(r, f) for r, _, fs in os.walk(self.home) for f in fs]
        self.assertFalse([w for w in written if "field-profiles" in w], written)
        s.disconnect()
        s2 = self.connect()
        self.assertTrue(s2.dataset_fields("fw").get("none"))

    def test_no_settings_are_stored_and_the_default_is_production(self):
        self.connect()
        self.assertFalse(os.path.exists(os.path.join(self.home, "settings.json")))
        env = os.environ.pop("ABSTRACT_BASE_URL", None)
        try:
            self.assertEqual(C.load_settings(), {"base_url": "https://api.abstractsecurity.app"})
            os.environ["ABSTRACT_BASE_URL"] = "https://api.test.example/"
            self.assertEqual(C.load_settings()["base_url"], "https://api.test.example")
        finally:
            os.environ.pop("ABSTRACT_BASE_URL", None)
            if env is not None:
                os.environ["ABSTRACT_BASE_URL"] = env



class TypedWhere(_Tenant):
    def test_where_on_a_number_field_is_typed(self):
        for r in self.tenant.rows:
            r["dest_port"] = 3389 if r["id"].endswith(("1", "2")) else 443
        code, res, run = self.export("--dataset", "fw", "--where", "dest_port=3389", start=S, end=END, name="n")
        self.assertEqual(code, 0, self.log[-6:])
        want = {r["id"] for r in self.tenant.rows if r["data_stream"]["dataset"] == "fw" and r["dest_port"] == 3389}
        self.assertEqual(res["rows_written"], len(want))

    def test_where_rejects_a_non_number_for_a_number_field(self):
        code, res, _ = self.export("--dataset", "fw", "--where", "dest_port=ssh", start=S, end=END, name="m")
        self.assertEqual(code, 2)
        self.assertIn("is a number field", res["error"])


if __name__ == "__main__":
    unittest.main()
