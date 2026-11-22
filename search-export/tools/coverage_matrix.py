#!/usr/bin/env python3
"""Every screen and every capability of the tool is documented, shown and mapped.

The population comes from the code, never from a hand list:
  - browser tabs (data-t in web/index.html), query tabs (role="tab"), cards (h3), dialogs (overlay h2), launch mode
  - guided-terminal main-menu entries (abx_menu.py) and guided steps (abx_wizard.STEPS)
  - command-line commands (abstract_search.py dispatch), their subcommands (abx_commands.py) and automate targets
  - MCP tools (abx_mcp.Server().tools), resources and prompts
  - Help topics (abx_core.HELP_TOPICS)

For each item:
  doc   it is described in the shipped docs (GUIDE.md, README.md, docs/*.md)
  shot  a visual surface (browser, terminal, command line) has a screenshot in docs/screenshots that a doc shows
  eg    a command has an example in a code block

The "Everything it does" map in GUIDE.md and README.md is generated from the same inventory (CAPABILITIES below): a
renamed tab, menu entry, command or tool changes the map, and a new one must be placed in it.

    python3 tools/coverage_matrix.py            print the matrix; exit 1 on any gap or a stale map
    python3 tools/coverage_matrix.py --write    regenerate the map blocks in GUIDE.md and README.md, then check
    python3 tools/coverage_matrix.py --json     the matrix as JSON
"""
import ast
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_BEGIN, MAP_END = "<!-- capability-map:begin -->", "<!-- capability-map:end -->"

# Which screenshot shows each visual surface. Reviewed by hand (only a person can say what an image shows); the gate
# checks that every visual item has an entry, that the file exists and that a doc shows it.
SHOTS = {
    "tab:start": "web-16-start.png", "tab:preview": "web-03-preview.png", "tab:validate": "web-04-validate.png",
    "tab:export": "web-05-export-complete.png", "tab:exports": "web-06-exports.png",
    "tab:fields": "web-10-fields.png", "tab:fieldsets": "web-20-fieldset-builder.png",
    "tab:views": "web-22-views-change.png", "tab:tenant": "web-12-tenant.png",
    "tab:apicalls": "web-14-api-calls.png", "tab:help": "web-28-help.png",
    "qtab:builder": "web-02-build.png", "qtab:kql": "web-17-kql.png", "qtab:nl": "web-18-english.png",
    "card:Export all results": "web-05-export-complete.png", "card:Run it on a schedule": "web-26-automate.png",
    "card:Fields of every dataset": "web-10-fields.png", "card:Fieldsets in Abstract": "web-20-fieldset-builder.png",
    "card:New fieldset": "web-20-fieldset-builder.png",
    "card:Save the current search as a view": "web-22-views-change.png",
    "card:Saved views in Abstract": "web-22-views-change.png", "card:Content as code": "web-24-content-as-code.png",
    "card:What this tenant supports": "web-12-tenant.png", "card:Platform endpoints": "web-23-platform.png",
    "card:API calls": "web-14-api-calls.png", "card:Launch mode": "web-29-launch-mode.png",
    "dialog:Connect to your tenant": "web-01-connect.png", "dialog:Use a search from Abstract": "web-08-import-paste.png",
    "dialog:Open the link from the terminal": "web-30-locked.png", "dialog:Review the change": "web-21-review.png",
    "dialog:Review the bundle": "web-25-bundle-review.png", "launch:Launch mode": "web-29-launch-mode.png",
    "menu:guided": "term-01-start.png", "menu:connect": "term-15-connect.png", "menu:tenant": "term-09-tenant.png",
    "menu:build": "term-10-build.png", "menu:english": "term-12-english.png", "menu:kql": "term-13-kql.png",
    "menu:preview": "term-17-preview.png", "menu:validate": "term-05-review.png",
    "menu:export": "term-06-result.png", "menu:exports": "term-18-exports.png", "menu:views": "term-11-views.png",
    "menu:saved": "term-19-saved.png", "menu:fieldsets": "term-14-fieldsets.png", "menu:content": "term-20-content.png",
    "menu:automate": "term-21-automate.png", "menu:fields": "term-22-fields.png", "menu:learn": "term-22-fields.png",
    "menu:datasets": "term-23-datasets.png", "menu:help": "term-24-help.png", "menu:quit": "term-08-menu.png",
    "menu:web": "term-08-menu.png",
    "step:Connect": "term-15-connect.png", "step:Dataset": "term-01-start.png", "step:Filters": "term-02-filters.png",
    "step:Time": "term-03-storage.png", "step:Fields": "term-04-fields.png", "step:Review": "term-05-review.png",
    "step:Export": "term-06-result.png",
    "cli:status": "cli-status.png", "cli:capabilities": "cli-capabilities.png", "cli:datasets": "cli-show-api.png", "cli:views": "cli-views.png",
    "cli:validate": "cli-validate.png", "cli:export": "cli-export.png", "cli:kql": "cli-kql.png",
    "cli:fieldsets": "cli-fieldsets.png", "cli:content": "cli-content.png", "cli:automate": "cli-automate.png",
    "cli:endpoints": "cli-endpoints.png", "cli:nl": "cli-nl.png", "cli:mcp": "cli-mcp.png",
    "cli:preview": "cli-preview.png", "cli:exports": "cli-exports.png", "cli:searches": "cli-searches.png",
    "cli:fields": "cli-fields.png", "cli:--web": "term-08-menu.png", "cli:--menu": "term-08-menu.png",
}

# Reviewed exceptions, one reason each. Nothing is waived silently.
WAIVERS = {}

# The "Everything it does" map: one row per capability, naming the inventory items that provide it. Every tab, query
# tab, menu entry, command and MCP tool must appear in at least one row (MAP_EXEMPT lists the few that are not a
# capability), and every item named here must exist, so the map cannot drift from the product.
CAPABILITIES = [
    ("Start: pick what you want to do", ["tab:start"], ["menu:guided"], [], [], "#step-b2-connect-and-check-the-tenant",
     "docs/USER-GUIDE.md#the-start-screen"),
    ("Connect and check which tenant the key belongs to", ["dialog:Connect to your tenant", "tab:tenant"],
     ["menu:connect", "menu:tenant"], ["cli:status", "cli:capabilities"], ["mcp:status", "mcp:capabilities"],
     "#step-b2-connect-and-check-the-tenant", "docs/USER-GUIDE.md#connect"),
    ("Find datasets and the fields they fill", ["tab:fields"], ["menu:fields", "menu:learn", "menu:datasets"],
     ["cli:datasets", "cli:fields"], ["mcp:datasets", "mcp:fields", "mcp:field_values"],
     "#step-c2-find-your-dataset", "docs/USER-GUIDE.md#fields-of-every-dataset"),
    ("Build a search with filters, every operator", ["qtab:builder"], ["menu:guided", "menu:build"],
     ["cli:preview"], ["mcp:preview"], "#step-b3-pick-the-data", "docs/SEARCH-REFERENCE.md"),
    ("Write KQL, checked against your tenant before it runs", ["qtab:kql"], ["menu:kql"], ["cli:kql"],
     ["mcp:kql_check", "mcp:kql_preview", "mcp:kql_save"], "#other-ways-to-write-the-search",
     "docs/SEARCH-REFERENCE.md#how-a-kql-or-plain-english-search-is-checked-before-it-runs"),
    ("Ask in plain English (Abstract's assistant writes the KQL)", ["qtab:nl"], ["menu:english"], ["cli:nl"],
     ["mcp:nl_to_kql"], "#other-ways-to-write-the-search",
     "docs/SEARCH-REFERENCE.md#how-a-kql-or-plain-english-search-is-checked-before-it-runs"),
    ("Use a search or saved view from Abstract", ["dialog:Use a search from Abstract"], ["menu:views"],
     ["cli:views"], ["mcp:views_list", "mcp:view_import"], "#other-ways-to-write-the-search",
     "docs/USER-GUIDE.md#use-a-search-from-abstract"),
    ("Preview the first events", ["tab:preview"], ["menu:preview"], ["cli:preview"], ["mcp:preview"],
     "#step-b6-preview-validate-export", "docs/USER-GUIDE.md#preview-validate-export"),
    ("Validate: every check an export runs, and a time estimate", ["tab:validate"], ["menu:validate"],
     ["cli:validate"], ["mcp:validate"], "#reading-validate", "docs/VALIDATION.md"),
    ("Export every matching event, recent data and the archive", ["tab:export"], ["menu:export"], ["cli:export"],
     ["mcp:export_start", "mcp:export_status", "mcp:export_cancel"], "#step-b6-preview-validate-export",
     "docs/EXPORTS.md"),
    ("Find, download, resume or delete exports", ["tab:exports"], ["menu:exports"], ["cli:exports"],
     ["mcp:exports_list"], "#step-b7-find-your-file", "docs/USER-GUIDE.md#exports"),
    ("Saved searches on this computer", [], ["menu:saved"], ["cli:searches"], [],
     "#step-c3-preview-validate-and-export", "docs/USER-GUIDE.md#saved-searches"),
    ("Build and change fieldsets in Abstract", ["tab:fieldsets"], ["menu:fieldsets"], ["cli:fieldsets"],
     ["mcp:fieldsets_list", "mcp:fieldset_get", "mcp:fieldset_plan", "mcp:apply_change"], "#fieldsets",
     "docs/FIELDSETS-AND-VIEWS.md"),
    ("Save, change or delete saved views in Abstract", ["tab:views", "dialog:Review the change"], ["menu:views"],
     ["cli:views"], ["mcp:view_plan", "mcp:apply_change"], "#saved-views", "docs/FIELDSETS-AND-VIEWS.md"),
    ("Views and fieldsets as code: back up, compare, copy", ["dialog:Review the bundle"], ["menu:content"],
     ["cli:content"], ["mcp:content_export", "mcp:content_diff", "mcp:content_plan"],
     "#views-and-fieldsets-as-files-bundles", "docs/CONTENT-AS-CODE.md"),
    ("Run a search on a schedule", ["tab:export"], ["menu:automate"], ["cli:automate"],
     ["mcp:automation_recipe"], "#step-c4-run-it-on-a-schedule", "docs/AUTOMATION.md"),
    ("Platform endpoints this tool uses", ["tab:tenant"], [], ["cli:endpoints"], ["mcp:endpoints"],
     "#platform-endpoints", "docs/USER-GUIDE.md#platform-endpoints"),
    ("Every API call, live, with the key masked", ["tab:apicalls"], [], [], [], "#every-api-call-live",
     "docs/USER-GUIDE.md#api-calls-and-logs"),
    ("Help, in the app", ["tab:help"], ["menu:help"], [], [], "#if-something-goes-wrong", "docs/TROUBLESHOOTING.md"),
    ("Open the browser app or the guided terminal", [], ["menu:web"], ["cli:--web", "cli:--menu"], [],
     "#step-b1-open-the-app-every-time", "docs/QUICK-START.md"),
    ("Use it from an AI assistant (MCP)", [], [], ["cli:mcp"], [], "#an-ai-assistant-mcp", "docs/MCP.md"),
    ("Launch mode (an optional theme)", ["launch:Launch mode"], [], [], [], "#launch-mode", "docs/USER-GUIDE.md#launch-mode"),
]
MAP_EXEMPT = {"menu:quit": "leaves the app; not a capability"}


# ── inventory, from the code ────────────────────────────────────────────────────────────────────────────────────
def _read(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
        return fh.read()


def _strings(node):
    """Every string constant under node, in source order (depth first, left to right). ast.walk is breadth first, so
    for pink(bold("Guided search & export")) + dim("  step by step") it would put the hint before the label."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [v for child in ast.iter_child_nodes(node) for v in _strings(child)]


def inventory():
    items = []
    html = _read("web/index.html")
    for tid, label in re.findall(r'data-t="([a-z]+)"[^>]*>([^<]+)<', html):
        items.append({"id": f"tab:{tid}", "kind": "browser tab", "label": label.strip(), "visual": True})
    for qid, label in re.findall(r'role="tab" id="qt-([a-z]+)"[^>]*>([^<]+)<', html):
        items.append({"id": f"qtab:{qid}", "kind": "query tab", "label": label.strip(), "visual": True})
    for label in re.findall(r"<h3[^>]*>([^<]+)</h3>", html):
        items.append({"id": f"card:{label.strip()}", "kind": "browser card", "label": label.strip(), "visual": True})
    for block in re.findall(r'class="overlay".*?<h2[^>]*>([^<]+)</h2>', html, flags=re.S):
        items.append({"id": f"dialog:{block.strip()}", "kind": "dialog", "label": block.strip(), "visual": True})
    if os.path.isfile(os.path.join(HERE, "web", "launch.js")):
        items.append({"id": "launch:Launch mode", "kind": "launch mode", "label": "Launch mode", "visual": True})

    tree = ast.parse(_read("abx_menu.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List) and node.value.elts and \
                isinstance(node.value.elts[0], ast.Tuple) and \
                getattr(node.value.elts[0].elts[0], "value", None) == "guided":
            for t in node.value.elts:
                if not isinstance(t, ast.Tuple):
                    continue
                key = str(getattr(t.elts[0], "value", ""))
                node2 = t.elts[1]
                while isinstance(node2, ast.IfExp):          # a label that changes with state: the idle one
                    node2 = node2.orelse
                label = (_strings(node2) or [key])[0]
                items.append({"id": f"menu:{key}", "kind": "terminal menu", "label": label.strip(), "visual": True})
            break
    for node in ast.walk(ast.parse(_read("abx_wizard.py"))):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "STEPS" for t in node.targets):
            for s in _strings(node.value):
                items.append({"id": f"step:{s}", "kind": "guided step", "label": s, "visual": True})

    cli = set(re.findall(r'argv\[:1\] == \["([a-z-]+)"\]', _read("abstract_search.py")))
    cli |= {"export"}
    cli -= {"--help-engines"}
    for c in sorted(cli):
        items.append({"id": f"cli:{c}", "kind": "command", "label": c, "visual": True, "example": [c]})
    subs = {"kql_cmd": "kql", "fieldsets_cmd": "fieldsets", "views_change_cmd": "views", "content_cmd": "content"}
    for node in ast.walk(ast.parse(_read("abx_commands.py"))):
        if isinstance(node, ast.FunctionDef) and node.name in subs:
            found = set()
            for c in ast.walk(node):
                if isinstance(c, ast.Compare):
                    left = c.left
                    is_sub = getattr(left, "id", "") == "sub" or (isinstance(left, ast.Subscript) and
                                                                   getattr(left.value, "id", "") == "args")
                    if is_sub:
                        for comp in c.comparators:
                            found |= set(_strings(comp))
            for s in sorted(found):
                cmd = subs[node.name]
                items.append({"id": f"cli:{cmd} {s}", "kind": "subcommand", "label": f"{cmd} {s}", "visual": False,
                              "example": [cmd, s]})
    for node in ast.walk(ast.parse(_read("abx_automate.py"))):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TARGETS" for t in node.targets):
            for s in _strings(node.value):
                items.append({"id": f"cli:automate {s}", "kind": "automate target", "label": f"automate {s}",
                              "visual": False, "example": ["automate", s]})

    sys.path.insert(0, HERE)
    import abx_mcp                                             # noqa: E402  the live tool table
    for t in abx_mcp.Server().tools:
        items.append({"id": f"mcp:{t['name']}", "kind": "MCP tool", "label": t["name"], "visual": False})
    src = _read("abx_mcp.py")
    for uri in sorted(set(re.findall(r'"uri": "(abstract://[\w-]+)"', src))):
        items.append({"id": f"mcp-resource:{uri}", "kind": "MCP resource", "label": uri, "visual": False})
    for name in abx_mcp.PROMPTS:
        items.append({"id": f"mcp-prompt:{name}", "kind": "MCP prompt", "label": name, "visual": False})

    import abx_core                                            # noqa: E402
    for rel, title in abx_core.HELP_TOPICS:
        items.append({"id": f"help:{rel}", "kind": "help topic", "label": title, "visual": False, "file": rel})
    return items


# ── the docs ───────────────────────────────────────────────────────────────────────────────────────────────────
def corpus():
    # "docs/X.md" on every system: Windows would give "docs\X.md" and every lookup by name would miss
    files = ["GUIDE.md", "README.md"] + sorted(os.path.relpath(p, HERE).replace(os.sep, "/")
                                               for p in glob.glob(os.path.join(HERE, "docs", "*.md")))
    # The generated map is not documentation of anything: an item must be described in the prose.
    strip = re.compile(re.escape(MAP_BEGIN) + r".*?" + re.escape(MAP_END), flags=re.S)
    return {f: strip.sub("", _read(f)) for f in files if os.path.isfile(os.path.join(HERE, f))}


def _code_lines(text):
    out, inside = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append(line)
    return out


def check(items, docs):
    all_text = "\n".join(docs.values())
    low = all_text.casefold()
    code = [ln for t in docs.values() for ln in _code_lines(t)]
    # Only the guide and the README render images (the Help topics are shown in the app, which ships no screenshots).
    shown = {m for f in ("GUIDE.md", "README.md") for m in re.findall(r"screenshots/([\w.-]+\.png)", docs.get(f, ""))}
    mcp_doc = docs.get("docs/MCP.md", "")
    rows = []
    for it in items:
        k, lbl = it["kind"], it["label"]
        if k in ("browser tab", "query tab"):
            doc = f"**{lbl}**" in all_text
        elif k == "MCP tool":
            doc = f"`{lbl}`" in mcp_doc
        elif k in ("MCP resource", "MCP prompt"):
            doc = lbl in mcp_doc
        elif k == "help topic":
            doc = os.path.getsize(os.path.join(HERE, it["file"])) > 200 and it["file"] in all_text
        elif k in ("command", "subcommand", "automate target"):
            doc = True                                             # judged by its example below
        else:
            doc = lbl.casefold() in low
        eg = None
        if "example" in it:
            words = it["example"]
            if words == ["--web"] or words == ["--menu"]:
                pat = re.compile(r"abstract_search\.py\s+" + re.escape(words[0]) + r"\b")
            else:
                pat = re.compile(r"abstract_search\.py(?:\s+--\S+)*\s+" + r"\s+".join(re.escape(w) for w in words)
                                 + r"(?=\s|$|['\"])")
            eg = any(pat.search(ln) for ln in code)
            if not eg and words == ["fieldsets", "list"]:
                eg = any(re.search(r"abstract_search\.py(?:\s+--\S+)*\s+fieldsets\s*$", ln) for ln in code)
            doc = eg
        shot = None
        if it["visual"]:
            f = SHOTS.get(it["id"])
            shot = bool(f) and os.path.isfile(os.path.join(HERE, "docs", "screenshots", f)) and f in shown
        ok = doc and (shot in (None, True)) and (eg in (None, True))
        waived = it["id"] in WAIVERS
        rows.append(dict(it, doc=doc, shot=shot, eg=eg, ok=ok or waived, waived=waived,
                         shot_file=SHOTS.get(it["id"])))
    return rows


# ── the capability map ─────────────────────────────────────────────────────────────────────────────────────────
def map_problems(items):
    ids = {i["id"]: i for i in items}
    named = set()
    probs = []
    for row in CAPABILITIES:
        for group in row[1:5]:
            for x in group:
                named.add(x)
                if x not in ids:
                    probs.append(f"the capability map names {x}, which the product no longer has")
    for i in items:
        if i["kind"] in ("browser tab", "query tab", "terminal menu", "command", "MCP tool") and \
                i["id"] not in named and i["id"] not in MAP_EXEMPT:
            probs.append(f"{i['id']} is in the product but not in the capability map")
    return probs


def _cell(ids, items, style):
    labels = {i["id"]: i["label"] for i in items}
    out = []
    for x in ids:
        lbl = labels.get(x, x.split(":", 1)[-1])
        if style == "bold":
            out.append(f"**{lbl}**" if not x.startswith("dialog:") and not x.startswith("launch:") else lbl)
        elif style == "code":
            out.append(f"`{lbl}`")
        else:
            out.append(lbl)
    return ", ".join(out) or "–"


def render_map(items, target):
    head = ("| What you can do | Browser app | Guided terminal | Command line | AI assistant (MCP) | Read |\n"
            "|---|---|---|---|---|---|\n")
    rows = []
    for name, web, term, cli, mcp, anchor, doc in CAPABILITIES:
        link = f"[Read]({anchor})" if target == "guide" else f"[Read]({doc})"
        rows.append(f"| {name} | {_cell(web, items, 'bold')} | {_cell(term, items, 'plain')} | "
                    f"{_cell(cli, items, 'code')} | {_cell(mcp, items, 'code')} | {link} |")
    return (f"{MAP_BEGIN}\n<!-- generated by tools/coverage_matrix.py from the code; edit CAPABILITIES there -->\n\n"
            + head + "\n".join(rows) + f"\n\n{MAP_END}")


def map_blocks(items, write=False):
    probs = []
    for rel, target in (("GUIDE.md", "guide"), ("README.md", "readme")):
        p = os.path.join(HERE, rel)
        text = _read(rel)
        want = render_map(items, target)
        m = re.search(re.escape(MAP_BEGIN) + r".*?" + re.escape(MAP_END), text, flags=re.S)
        if not m:
            probs.append(f"{rel} has no capability map block ({MAP_BEGIN} … {MAP_END})")
            continue
        if m.group(0) != want:
            if write:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text[:m.start()] + want + text[m.end():])
            else:
                probs.append(f"{rel}: the capability map is stale; run python3 tools/coverage_matrix.py --write")
    return probs


def guide_anchors():
    out = set()
    for line in _read("GUIDE.md").splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if m:
            s = re.sub(r"[^\w\s-]", "", m.group(1).strip().casefold()).replace(" ", "-")
            out.add("#" + s)
    return out


def link_problems():
    probs = []
    anchors = guide_anchors()
    for row in CAPABILITIES:
        if row[5] not in anchors:
            probs.append(f"capability map: GUIDE.md has no heading for {row[5]}")
        f = row[6].split("#")[0]
        if not os.path.isfile(os.path.join(HERE, f)):
            probs.append(f"capability map: {f} does not exist")
    return probs


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    items = inventory()
    if "--write" in argv:
        map_blocks(items, write=True)
    rows = check(items, corpus())
    probs = map_problems(items) + map_blocks(items) + link_problems()
    for w in WAIVERS:
        if not any(r["id"] == w for r in rows):
            probs.append(f"waiver {w} names nothing the product has; remove it")
    gaps = [r for r in rows if not r["ok"]]
    if "--json" in argv:
        print(json.dumps({"rows": rows, "gaps": [r["id"] for r in gaps], "problems": probs}, indent=1))
    else:
        def mark(v):
            return "-" if v is None else ("yes" if v else "NO")
        w = max(len(r["id"]) for r in rows)
        print(f"{'item'.ljust(w)}  {'kind'.ljust(16)} doc  shot  example  screenshot")
        for r in rows:
            print(f"{r['id'].ljust(w)}  {r['kind'].ljust(16)} {mark(r['doc']).ljust(4)} {mark(r['shot']).ljust(5)} "
                  f"{mark(r['eg']).ljust(8)} {r.get('shot_file') or ''}" + ("  (waived: " + WAIVERS[r['id']] + ")"
                                                                          if r["waived"] else ""))
        kinds = {}
        for r in rows:
            kinds.setdefault(r["kind"], [0, 0])
            kinds[r["kind"]][0] += 1
            kinds[r["kind"]][1] += 1 if r["ok"] else 0
        print("\ncovered: " + ", ".join(f"{k} {v[1]}/{v[0]}" for k, v in kinds.items()))
        print(f"total {len(rows) - len(gaps)}/{len(rows)} items covered, {len(WAIVERS)} waived")
        for p in probs:
            print("PROBLEM: " + p)
        for g in gaps:
            print("GAP: " + g["id"] + " — " + ", ".join(x for x, v in (("doc", g["doc"]), ("screenshot", g["shot"]),
                                                                       ("example", g["eg"])) if v is False))
    return 1 if gaps or probs else 0


if __name__ == "__main__":
    sys.exit(main())
