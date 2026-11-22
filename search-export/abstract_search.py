#!/usr/bin/env python3
HELP = """
Abstract Search & Export — search, validate and export every event in your Abstract tenant.

Start here
  python3 abstract_search.py              guided search & export in this terminal (m = main menu, q = quit)
  python3 abstract_search.py --menu       open the terminal's main menu first
  python3 abstract_search.py --web        the browser app  [--port N] [--no-browser] [--bind ADDR, containers]

Commands (the same steps as the browser app; add --json for machine-readable output)
  capabilities                            what this tenant supports: KQL, archive, complete events
  datasets                                every dataset, with 24-hour volume
  views                                   every saved view in Abstract, and how it runs here
  searches                                your saved searches (on this computer)
  exports [--delete NAME]                 the exports on this computer (resume one: export --resume FOLDER)
  fields --dataset NAME | --all           the fields a dataset uses, learned live
  preview  <search>                       how many events match, and the first 25
  validate <search>                       every check an export runs, without exporting
  export   <search> [--format F --merge]  export every matching event to this computer

KQL, plain English, fieldsets and saved views
  kql check|preview|save "QUERY"          how a KQL query runs here; run it; save its result table
  nl "WHAT YOU WANT" [--preview]          plain English to KQL with Abstract's assistant (shown first, never run
                                          unless you ask)
  fieldsets [list|show|create|update|delete]   build and keep Abstract fieldsets (column lists)
  views save|update|delete …              save a search as a saved view in Abstract, change or delete one
  endpoints                               the platform endpoints this tool uses and what it saw
  content export|diff|apply DIR          saved views and fieldsets as files: back up, compare, restore, copy
                                          to another tenant (one review for the whole bundle; --yes applies)
  automate TARGET --from-saved NAME       the files that run a search on a schedule: cron, launchd, systemd,
                                          windows-task, github-actions, container-cron  [--every 1d --out DIR]
  mcp [--http PORT]                       let an AI assistant on this computer use this tool over MCP (see
                                          Help → Use the tool from an AI assistant)
  Changes to Abstract show exactly what they will do and stop; add --yes to make them.

<search> is one of
  --dataset NAME [--where "CONDITION" …] [--match all|any]
  --kql "events | where …" | --kql-file FILE
  --from-view "NAME" | --from-file FILE | --from-saved NAME
and a time range: --last 24h | --start … --end …   (UTC)
plus, if you need them: --storage auto|recent   --fields a,b,c | --every-field

A --where CONDITION is written as in Abstract's search, in double quotes, for example
  --where "user_name !contains example.com"     --where "dest_port >= 1024"     --where "action in (Login, Logout)"
  --where "user_name startswith svc- or isempty(user_name)"     --where user_name=alice@example.com,bob@example.com
Every operator Abstract offers works: ==, !=, in, contains, has, startswith, endswith (and ! forms), matches regex,
>, >=, <, <=, between, isempty(), isnotempty(), ipv4_is_in_range(), and, or, not(…), brackets.

Seeing what it does
  Every call to Abstract's API is shown as it happens (method, path, request, status, time) on standard error,
  in every command, the terminal and the browser app. The API key is always masked.
  --hide-api                              do not show the calls (--quiet also hides them; --show-api with --quiet shows them)
  Errors are also written to a daily log in the logs folder of your exports folder; each export folder has run.log.

  python3 abstract_search.py export --help   every export option
  python3 abstract_search.py --version       the version and build details

Before a command: set ABSTRACT_VENDOR_ACCOUNT_ID, ABSTRACT_EXPECT_TENANT and the key: ABSTRACT_API_KEY, or
ABSTRACT_API_KEY_COMMAND to read it from a secret manager or keychain when needed (see Start-Here-Guide.html, Part 3).
Terminal: NO_COLOR=1 turns colour off; ABX_PLAIN=1 gives plain numbered prompts (screen-reader friendly).
Needs Python 3.8 or newer. Nothing to install. Start-Here-Guide.html, next to the app folder, explains everything.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if sys.version_info < (3, 8):
    sys.exit("Abstract Search & Export needs Python 3.8 or newer (found %d.%d)." % sys.version_info[:2])


def _brand_header(cmd):
    """The branded header, on a terminal only: piped output stays clean."""
    import abx_core as C
    import abx_ui as U
    print()
    U.logo()
    print("\n  " + U.gradient("S E A R C H   &   E X P O R T") + "   " + U.dim(f"v{C.VERSION} · {cmd}"))
    U.rule("━")


def _build_info():
    """The BUILD file the kit build writes at the kit root (version, tree digest, build time, label), verbatim.
    None in a source checkout, where there is none. abx_ui's writer gives ASCII stand-ins where stdout needs them."""
    try:
        with open(os.path.join(HERE, "BUILD"), encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read() or None
    except OSError:
        return None


def _stop_on_sigterm():
    """`docker stop` and service managers send SIGTERM. As a container's first process Python would ignore it, so
    treat it like Ctrl+C and stop cleanly. An export that was running resumes from where it stopped."""
    import signal

    def stop(_sig, _frame):
        raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGTERM, stop)
    except (ValueError, OSError, AttributeError):          # not the main thread, or no SIGTERM on this system
        pass


def _port(argv):
    if "--port" not in argv:
        return 0
    try:
        port = int(argv[argv.index("--port") + 1])
    except (IndexError, ValueError):
        raise ValueError("--port needs a number, e.g. --port 8765")
    if not 0 <= port <= 65535:
        raise ValueError("--port must be between 0 and 65535")
    return port


def main(argv=None):
    """The command line. A fault in the tool itself is written to the log with its details, never a bare traceback."""
    try:
        return _main(argv)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as ex:
        _print_error(f"something went wrong in the tool itself ({type(ex).__name__}: {ex}). The details are in the "
                     f"log; please send it to your Abstract contact", ex)
        return 2


def _main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    import abx_core as C
    # Every API call is part of the live output (standard error), with the key masked. --hide-api turns it off;
    # --quiet does too, unless --show-api asks for it explicitly. Standard output (--json, the result line) never has it.
    if {"--show-api", "--hide-api"} & set(argv) or "--quiet" in argv:
        import abx_engine as E
        if "--hide-api" in argv:
            E.SHOW_API = False
        elif "--show-api" in argv:
            E.SHOW_API = True
        else:
            E.SHOW_API = False
        argv = [a for a in argv if a not in ("--show-api", "--hide-api")]
    if argv[:1] in (["--version"], ["-V"], ["-h"], ["--help"]):
        import abx_ui                                            # noqa: F401  ASCII stand-ins where the encoding needs them
    if argv[:1] in (["--version"], ["-V"]):
        print(f"{C.APP_NAME} {C.VERSION}")
        build = _build_info()
        if build:
            sys.stdout.write(build if build.endswith("\n") else build + "\n")
        return 0
    if argv[:1] in (["-h"], ["--help"]):
        print(HELP)
        return 0
    if argv[:1] == ["--web"]:
        import abx_web
        try:
            port = _port(argv)
        except ValueError as ex:
            _print_error(f"{ex}")
            return 2
        bind = None
        if "--bind" in argv:
            if argv.index("--bind") + 1 >= len(argv):
                _print_error("--bind needs an address, e.g. --bind 0.0.0.0 (containers only)")
                return 2
            bind = argv[argv.index("--bind") + 1]
        _stop_on_sigterm()
        try:
            if abx_web.serve(port=port, open_browser="--no-browser" not in argv, bind=bind) is None:
                return 2
        except KeyboardInterrupt:
            print("\n  stopped.")
        except OSError as ex:
            _print_error(f"could not start the browser app: {C.humanize(ex)}")
            return 2
        return 0
    if argv[:1] in (["fields"], ["export"], ["count"], ["datasets"], ["views"], ["searches"],
                     ["capabilities"], ["preview"], ["validate"], ["exports"]) and \
            sys.stdout.isatty() and "--quiet" not in argv and "--json" not in argv and \
            not ({"-h", "--help"} & set(argv)):
        _brand_header(argv[0])
    if argv[:1] in (["fields"], ["datasets"], ["views"], ["searches"], ["capabilities"], ["exports"]) and \
            {"-h", "--help"} & set(argv):
        print(HELP)
        return 0
    if argv[:1] == ["kql"]:
        import abx_commands as X
        return X.kql_cmd(_connect, argv[1:])
    if argv[:1] == ["nl"]:
        import abx_commands as X
        return X.nl_cmd(_connect, argv[1:])
    if argv[:1] == ["fieldsets"]:
        import abx_commands as X
        return X.fieldsets_cmd(_connect, argv[1:])
    if argv[:1] == ["endpoints"]:
        import abx_commands as X
        return X.endpoints_cmd(argv[1:])
    if argv[:1] == ["content"]:
        import abx_commands as X
        return X.content_cmd(_connect, argv[1:])
    if argv[:1] == ["automate"]:
        import abx_commands as X
        return X.automate_cmd(_connect, _spec_from_args, _search_parser, argv[1:])
    if argv[:1] == ["mcp"]:
        import abx_mcp
        return abx_mcp.main(argv[1:])
    if argv[:1] == ["views"] and argv[1:2] and argv[1] in ("save", "update", "delete"):
        import abx_commands as X
        return X.views_change_cmd(_connect, _spec_from_args, _search_parser, argv[1:])
    if argv[:1] == ["fields"]:
        return fields_cmd(argv[1:])
    if argv[:1] == ["datasets"]:
        return datasets_cmd(argv[1:])
    if argv[:1] == ["views"]:
        return views_cmd(argv[1:])
    if argv[:1] == ["capabilities"]:
        return capabilities_cmd(argv[1:])
    if argv[:1] == ["preview"]:
        return preview_cmd(argv[1:])
    if argv[:1] == ["validate"]:
        return validate_cmd(argv[1:])
    if argv[:1] == ["exports"]:
        return exports_cmd(argv[1:])
    if argv[:1] == ["searches"]:
        return searches_cmd(argv[1:])
    if argv[:1] == ["export"] and _flag_value(argv, "--from-saved") is not None:
        try:
            argv = from_saved_argv(argv)
        except ValueError as ex:
            _print_error(f"{ex}")
            import json
            print(json.dumps({"error": str(ex), "exit_code": 2}))
            return 2
    if argv[:1] in (["export"], ["count"]) and (_flag_value(argv, "--kql") is not None or
                                                _flag_value(argv, "--kql-file") is not None) and \
            not ({"-h", "--help"} & set(argv)):
        try:
            argv = expand_kql_argv(argv)
        except _errors() as ex:
            import json
            _print_error(f"{C.humanize(ex)}")
            print(json.dumps({"error": C.humanize(ex), "exit_code": 2}))
            return 2
        if isinstance(argv, int):
            return argv
    if argv[:1] in (["export"], ["count"]) and any(
            not _LEGACY_WHERE.match(v) for v in _flag_values(argv, "--where")) and not ({"-h", "--help"} & set(argv)):
        try:
            argv = expand_where_argv(argv)
        except _errors() as ex:
            import json
            import abx_core as C
            _print_error(f"{C.humanize(ex)}")
            print(json.dumps({"error": C.humanize(ex), "exit_code": 2}))
            return 2
        if isinstance(argv, int):
            return argv
    if argv[:1] in (["export"], ["count"], ["--help-engines"]):
        import abx_engine
        return abx_engine.main(argv)
    import abx_menu
    if argv[:1] == ["--menu"]:
        abx_menu.START_MENU = True
        argv = argv[1:]
    if argv and argv[0].startswith("-"):
        print(f"unknown option {argv[0]!r}; see --help", file=sys.stderr)
        return 2
    if argv:
        print(f"unknown command {argv[0]!r}; see --help", file=sys.stderr)
        return 2
    return abx_menu.main()


def _flag_value(args, flag):
    """The value of --flag VALUE or --flag=VALUE, or None."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _flag_values(args, flag):
    out = []
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith(flag + "="):
            out.append(a.split("=", 1)[1])
    return out


def expand_where_argv(argv):
    """export / count with condition --where values: every --where (both forms) becomes one --condition-json, so the
    engine runs exactly what preview and validate run. --match any puts every --where in one "at least one" group."""
    import json
    import abx_core as C
    import abx_engine as E
    if _flag_value(argv, "--condition-json") is not None or _flag_value(argv, "--condition-file") is not None:
        raise E.UserError("a condition --where cannot be combined with --condition-json / --condition-file")
    s = _connect(argv)
    if isinstance(s, int):
        return s
    spec = C.empty_spec()
    pieces = []
    for w in _flag_values(argv, "--where"):
        if _LEGACY_WHERE.match(w):
            f, v = (x.strip() for x in w.split("=", 1))
            t = s.field_type(f)
            pieces.append(([{"field": f, "type": t if t in ("Float64", "Ipv4") else "String", "op": "IS_IN_LIST",
                             "value": v} if t != "Boolean" else
                            {"field": f, "type": t, "op": "IS_TRUE" if v.lower() == "true" else "IS_FALSE",
                             "value": ""}], []))
        else:
            pieces.append(where_expression(s, w))
    if (_flag_value(argv, "--match") or "all") == "any":
        filters, groups = C.either_of(pieces)
    else:
        filters = [f for fl, _ in pieces for f in fl]
        groups = [g for _, gr in pieces for g in gr]
    spec.update(filters=filters, any_groups=groups)
    cond = C.build_condition(C.normalize_spec(spec))
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in ("--where", "--match"):
            skip = True
            continue
        if a.startswith("--where=") or a.startswith("--match="):
            continue
        out.append(a)
    return out + ["--condition-json", json.dumps(cond)]


def _drop_flags(argv, flags):
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in flags:
            skip = True
            continue
        if any(a.startswith(f + "=") for f in flags):
            continue
        out.append(a)
    return out


def expand_kql_argv(argv):
    """export / count --kql: the query is checked here first. A query that converts into filters exports exactly,
    with the row check; one that cannot runs as KQL on Abstract where the tenant applies KQL; a query that reshapes
    rows (summarize, extend …) is refused here and saved with `kql save` instead."""
    import json
    import abx_core as C
    import abx_engine as E
    text = _flag_value(argv, "--kql")
    if text is None:
        text = E.read_text_file(_flag_value(argv, "--kql-file"), "KQL file")
    if _flag_value(argv, "--dataset") or _flag_values(argv, "--where") or _flag_value(argv, "--from-view") or \
            _flag_value(argv, "--from-file") or _flag_value(argv, "--condition-json"):
        raise E.UserError("--kql already holds the whole search; do not add --dataset, --where or another search")
    s = _connect(argv)
    if isinstance(s, int):
        return s
    chk = s.kql_check(text)
    rest = _drop_flags(argv, ("--kql", "--kql-file"))
    if chk["runs_as"] == "filters":
        spec = C.normalize_spec(dict(C.empty_spec(), dataset=chk["dataset"], filters=chk["filters"],
                                     any_groups=chk["any_groups"]))
        out = ["--condition-json", json.dumps(C.build_condition(dict(spec, dataset="")))]
        if chk["dataset"]:
            out += ["--dataset", chk["dataset"]]
        return rest + out
    if chk["runs_as"] == "kql":
        if "--quiet" not in argv:
            print("  this query runs as KQL on Abstract (" + chk["reason"].split("; ", 1)[-1] + "); the tool cannot "
                  "re-check KQL row by row, so the export holds what Abstract returns", file=sys.stderr)
        return rest + ["--kql", chk["kql"]]
    if chk["runs_as"] == "kql_result":
        raise E.UserError("this query changes which rows come back (" + chk["reason"] + "). Save its result table "
                          "with: kql save \"<query>\"")
    raise E.UserError("this query cannot run: " + chk["reason"])


def _connect(args):
    """A Session connected from the environment and checked against the expected tenant, or an exit code."""
    import abx_core as C
    S = C.sanitize
    expect = _flag_value(args, "--expect-tenant") or os.environ.get("ABSTRACT_EXPECT_TENANT")
    s = C.Session()
    saved = os.environ.pop("ABSTRACT_EXPECT_TENANT", None)       # checked below, with the engine's wording
    try:
        connected = s.connect_from_env()
    finally:
        if saved is not None:
            os.environ["ABSTRACT_EXPECT_TENANT"] = saved
    if not connected:
        print("set ABSTRACT_VENDOR_ACCOUNT_ID and ABSTRACT_API_KEY (or ABSTRACT_API_KEY_COMMAND) first (see "
              "Start-Here-Guide.html, Part 3)", file=sys.stderr)
        return 2
    name, vid = S(s.tenant["name"]), S(s.tenant["vendor_account_id"])
    if not expect:
        print(f"STOPPED: connected as {name} ({vid}). Set ABSTRACT_EXPECT_TENANT={vid} (or pass --expect-tenant "
              f"{vid}) so nothing can ever run against the wrong tenant.", file=sys.stderr)
        return 2
    if expect != s.tenant["vendor_account_id"]:
        print(f"TENANT MISMATCH: connected as {name} ({vid}), expected {S(expect)}", file=sys.stderr)
        return 2
    return s


def _print_error(msg, exc=None):
    """Every error the command line shows is also written to today's log, and the message says where."""
    import abx_engine as E
    path = E.log_error("ERROR", msg, exc)
    print(f"ERROR: {msg}", file=sys.stderr)
    print(f"  (kept in the log: {path})", file=sys.stderr)


def _errors():
    import abx_engine as E
    return (E.UserError, E.Fatal, E.Retryable, OSError, ValueError)


def _search_parser(cmd):
    import argparse
    p = argparse.ArgumentParser(prog=f"abstract_search.py {cmd}", description=f"{cmd} one search (the same checks "
                                f"as the browser app and the guided terminal)")
    p.add_argument("--dataset", help="the dataset's full name (any case)")
    p.add_argument("--where", action="append", help="field=value[,value…] (the field is one of these values), or any "
                   "condition as in Abstract's search, e.g. \"user_name !contains example.com\"; repeatable")
    p.add_argument("--match", choices=("all", "any"), help="all (default): every --where must match; any: at least one")
    p.add_argument("--last", help="15m, 1h, 4h, 24h, 7d, 30d …")
    p.add_argument("--start", help="UTC start, e.g. 2026-09-28T00:00:00Z")
    p.add_argument("--end", help="UTC end")
    p.add_argument("--storage", choices=("auto", "recent", "archive"), help="where to search (default auto)")
    p.add_argument("--fields", help="comma-separated fields to show or export")
    p.add_argument("--every-field", action="store_true", help="complete events")
    p.add_argument("--from-view", help="one of your saved views in Abstract (name or id)")
    p.add_argument("--from-file", help="a search copied from Abstract's search box, in a text file")
    p.add_argument("--from-saved", help="one of your saved searches (see: searches)")
    p.add_argument("--kql", help="a KQL query, e.g. \"events | where user_name has 'admin'\"")
    p.add_argument("--kql-file", help="a KQL query in a text file (use this in Windows PowerShell)")
    p.add_argument("--expect-tenant", help="the vendor account id you expect (or ABSTRACT_EXPECT_TENANT)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    return p


_LEGACY_WHERE = re.compile(r"^\s*[\w.@-]+\s*=(?![=~])")


def where_expression(s, w):
    """--where 'user_name !contains "example.com"': any KQL condition, converted exactly like a pasted search.
    Returns (filters, any_groups). The old form field=value[,value…] never reaches here."""
    import abx_core as C
    import abx_engine as E
    import abx_kql as K
    names = set(s.fields()) | set(re.findall(r"\bext\.[\w.@-]+", w))          # ext.* is not in the catalog
    try:
        conv = K.convert("events | where " + w, s.field_type, names, bare=True)
    except K.ConvertError as ex:
        raise E.UserError(f"--where {w!r}: {ex}")
    if conv["dataset"] or conv["time"]:
        raise E.UserError(f"--where {w!r}: use --dataset for the dataset and --last / --start / --end for the time")
    for f in conv["filters"] + [x for g in conv["any_groups"] for x in g]:
        prob = C.filter_problem(f)
        if prob:
            raise E.UserError(f"--where {w!r}: {prob}")
    return conv["filters"], conv["any_groups"]


def _spec_from_args(s, a):
    """The same search model the apps use, from command-line options."""
    import abx_core as C
    import abx_engine as E
    kql = getattr(a, "kql", None) or (E.read_text_file(a.kql_file, "KQL file") if getattr(a, "kql_file", None) else None)
    if sum(1 for x in (a.from_view, a.from_file, a.from_saved, kql) if x) > 1:
        raise E.UserError("use one of --from-view, --from-file, --from-saved or --kql")
    if kql:
        if a.dataset or a.where:
            raise E.UserError("--kql already holds the whole search; do not add --dataset or --where")
        chk = s.kql_check(kql)
        if chk["runs_as"] == "refused":
            raise E.UserError("this query cannot run: " + chk["reason"])
        if chk["runs_as"] == "kql_result":
            raise E.UserError("this query changes which rows come back (" + chk["reason"] + "); use kql preview or "
                              "kql save for it")
        spec = C.empty_spec()
        if chk["runs_as"] == "filters":
            spec.update(dataset=chk["dataset"], filters=chk["filters"], any_groups=chk["any_groups"])
        else:
            spec.update(mode="kql", kql=chk["kql"])
        a.from_view = a.from_file = a.from_saved = None
        a.match = None
        return _finish_spec(a, spec)
    if (a.from_view or a.from_file or a.from_saved) and (a.dataset or a.where):
        raise E.UserError("--from-view / --from-file / --from-saved already hold the whole search; do not add "
                          "--dataset or --where")
    if a.from_saved:
        spec = C.saved_load(a.from_saved)
    elif a.from_view or a.from_file:
        if a.from_view:
            want = a.from_view.strip()
            v = next((v for v in s.views(refresh=True) if v["id"] == want or v["name"].casefold() == want.casefold()),
                     None)
            if v is None:
                raise E.UserError(f"no saved view called {want!r} on this tenant (see: views)")
            spec = s.import_search(view_id=v["id"])["spec"]
        else:
            spec = s.import_search(text=E.read_text_file(a.from_file, "search file"))["spec"]
    else:
        spec = C.empty_spec()
        if a.dataset:
            spec["dataset"] = E.check_dataset_name(s.client, a.dataset)
        pieces = []
        for w in a.where or []:
            if not _LEGACY_WHERE.match(w):
                pieces.append(where_expression(s, w))
                continue
            f, v = (x.strip() for x in w.split("=", 1))
            if f not in s.fields() and not (f.startswith("ext.") and len(f) > 4):       # ext.* is not in the catalog
                raise E.UserError(f"These fields do not exist on your tenant: {f} (in --where)")
            t = s.field_type(f)
            if t == "Boolean":
                pieces.append(([{"field": f, "type": t, "op": "IS_TRUE" if v.lower() == "true" else "IS_FALSE",
                                 "value": ""}], []))
            else:
                pieces.append(([{"field": f, "type": t if t in ("Float64", "Ipv4") else "String",
                                 "op": "IS_IN_LIST", "value": v}], []))
        if a.match == "any" and any(gr or len(fl) > 1 for fl, gr in pieces):
            spec["filters"], spec["any_groups"] = C.either_of(pieces)      # at least one --where, exactly
        else:
            spec["filters"] = [f for fl, _ in pieces for f in fl]
            spec["any_groups"] = [g for _, gr in pieces for g in gr]
            if a.match:
                spec["match"] = a.match
    if a.match and (a.from_view or a.from_file or a.from_saved):
        spec["match"] = a.match
    return _finish_spec(a, spec)


def _finish_spec(a, spec):
    import abx_core as C
    if a.storage:
        spec["storage"] = a.storage
    if a.last:
        spec["time"] = {"last": a.last}
    elif a.start or a.end:
        spec["time"] = {"start": a.start or "", "end": a.end or ""}
    if a.every_field:
        spec["full_documents"] = True
    elif a.fields:
        spec["fields"] = list(dict.fromkeys(["@timestamp", "id"] + [x.strip() for x in a.fields.split(",") if x.strip()]))
    return C.normalize_spec(spec)


def preview_cmd(args):
    """How many events match, and the first 25: the same preview as the browser app."""
    import json
    import abx_core as C
    import abx_ui as U
    S = C.sanitize
    a = _search_parser("preview").parse_args(args)
    try:
        s = _connect(args)
        if isinstance(s, int):
            return s
        s.capabilities()
        r = s.preview(_spec_from_args(s, a))
    except _errors() as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    if a.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    print(f"\n  {r['total']:,} events match  {U.dim(r['window'][0][:19] + ' → ' + r['window'][1][:19] + ' UTC')}"
          f"  {U.dim('(first ' + str(len(r['rows'])) + ' shown)')}\n")
    cols = r["columns"][:6]
    flats = [C.flatten(row) for row in r["rows"]]
    ws = [min(28, max([len(c)] + [len(S(C.cell(f.get(c)))) for f in flats])) for c in cols]
    print("  " + "  ".join(U.pad(S(c)[:w], w) for c, w in zip(cols, ws)))
    for flat in flats:
        print("  " + "  ".join(U.pad(S(C.cell(flat.get(c)))[:w], w) for c, w in zip(cols, ws)))
    if len(r["columns"]) > 6:
        print("  " + U.dim(f"(+{len(r['columns']) - 6} more columns in the export; --json shows every field)"))
    print()
    return 0


def validate_cmd(args):
    """Every check an export runs, without exporting: the same checklist as Validate in the browser app."""
    import json
    import abx_core as C
    import abx_ui as U
    S = C.sanitize
    a = _search_parser("validate").parse_args(args)
    try:
        s = _connect(args)
        if isinstance(s, int):
            return s
        s.capabilities()
        r = s.validate(_spec_from_args(s, a))
    except _errors() as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    if a.json:
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["ready"] else 1
    print()
    colour = {"PASS": U.teal, "WARN": U.amber, "FAIL": U.red, "INFO": U.dim}
    for c in r["checks"]:
        print(f"  {colour.get(c['status'], str)(c['status'].ljust(4))}  {S(c['name'])}")
        if c.get("detail"):
            print(f"        {U.dim(S(c['detail']))}")
    plan = r.get("plan") or {}
    arch = plan.get("archive")
    up = "up to " if plan.get("upper_bound") else ""
    when = f" Estimated time: {plan['time_words']}" if plan.get("time_words") else ""
    ready_txt = (f"Ready to export: {up}{plan.get('total', 0):,} events from recent data, plus the part before "
                 f"{str(arch['end'])[:10]} from the archive ({arch['jobs']} archive job(s)"
                 + (f", roughly {arch['minutes']:,} minutes" if arch.get("minutes") else "") + ").") if arch else \
        f"Ready to export: {up}{plan.get('total', 0):,} events."
    ready_txt += when
    print("\n  " + (U.teal(ready_txt) if r["ready"] else
                     U.red("Not ready to export.") if not r["ok"] else U.amber("Nothing matched.")) + "\n")
    return 0 if r["ready"] else 1


def capabilities_cmd(args):
    """What the connected tenant supports, checked live by behaviour (read only, nothing stored)."""
    import json
    import abx_core as C
    S = C.sanitize
    try:
        s = _connect(args)
        if isinstance(s, int):
            return s
        caps = s.capabilities(refresh=True)
    except _errors() as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    if "--json" in args:
        print(json.dumps(caps, indent=2))
        return 0
    print(f"\n  What {S(s.tenant['name'])} supports (checked live, read only)\n")
    w = max(len(i["label"]) for i in caps["items"])
    for i in caps["items"]:
        print(f"  {i['label'].ljust(w)}  {i['status'].ljust(13)}  {S(i['detail'])}")
    print()
    return 0


def datasets_cmd(args):
    """Every dataset (integration configuration) on the tenant, read live, busiest first."""
    import json
    import abx_core as C
    S = C.sanitize
    try:
        s = _connect(args)
        if isinstance(s, int):
            return s
        ds = s.datasets()
    except _errors() as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    if "--json" in args:
        print(json.dumps(ds, indent=2))
        return 0
    w = min(60, max(len(S(d["name"])) for d in ds))
    print(f"\n  {len(ds)} datasets on {S(s.tenant['name'])} (names match in any case; a partial name matches "
          f"nothing)\n")
    for d in ds:
        n = d.get("events_24h")
        vol = "volume unknown" if n is None else ("no events in 24h" if n == 0 else f"{n:,} events in 24h")
        print(f"  {S(d['name']).ljust(w)}  {vol}" + ("  · inactive" if d.get("active") is False else ""))
    print()
    return 0


def views_cmd(args):
    """Every saved view in Abstract that holds a search, read live (never stored), each converted the way an export
    would convert it, so the list says which ones this tool can run exactly and why the others cannot."""
    import json
    import abx_core as C
    S = C.sanitize
    try:
        s = _connect(args)
        if isinstance(s, int):
            return s
        try:
            s.capabilities()                               # where KQL works, views that do not convert run as KQL
        except _errors():
            pass
        views = s.views(refresh=True)
        rows = []
        kinds = {"kql": "KQL", "filters": "filter builder", "replay": "replay", "none": "columns only",
                 "unreadable": "unreadable"}
        for v in views:
            r = {"id": v["id"], "name": v["name"], "search": v["kql"], "kind": kinds.get(v.get("origin"), "KQL"),
                 "updated_at": v.get("updated_at"), "time": v.get("time"), "columns": v.get("fields") or []}
            try:
                conv = s.import_search(view_id=v["id"])
                if conv.get("columns_only"):
                    r.update(converts=False, loads=True, reason="holds no filter, only columns and a time range: "
                             "load it, add a dataset or a filter, and save it")
                else:
                    r.update(converts=True, loads=True, runs_as=conv["readable"], notes=conv["notes"])
            except C.E.UserError as ex:
                r.update(converts=False, loads=False, reason=str(ex))
            rows.append(r)
    except _errors() as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    skipped = sum(1 for r in rows if r["kind"] == "columns only")
    if "--save" in args:                                   # sync: every view that loads becomes a saved search
        saved = C.sync_views(s, rows)
        print(f"\n  saved {len(saved)} views as saved searches on this computer (see: searches)")
    if "--json" in args:
        print(json.dumps({"views": rows, "views_without_a_search": skipped}, indent=2))
        return 0
    ok = sum(1 for r in rows if r["converts"])
    bad = sum(1 for r in rows if not r["loads"])
    print(f"\n  {len(rows)} saved views on {S(s.tenant['name'])}: {ok} ready to export, {skipped} with columns and a "
          f"time range only" + (f", {bad} cannot be converted" if bad else "") + "\n")
    for r in rows:
        mark = "ready  " if r["converts"] else "columns" if r["loads"] else "cannot "
        print(f"  {mark}  {S(r['name'])}  ({S(r['id'])}, {r['kind']})")
        if r["converts"]:
            for line in r["runs_as"]:
                print(f"             • {S(line)}")
        else:
            print(f"             {S(r['reason'])}")
    if ok:
        print("\n  export one with:  export --from-view \"<name or id>\" --merge   (the view's own time range, "
              "or add --last 24h)")
    print("  save them all as saved searches on this computer:  views --save\n")
    return 0


def exports_cmd(args):
    """The exports on this computer, newest first (the same list as the browser app and the terminal), and delete."""
    import json
    import abx_core as C
    S = C.sanitize
    try:
        rows = C.list_exports()
        target = _flag_value(args, "--delete")
        if target:
            hit = [x for x in rows if x["name"] == os.path.basename(target.rstrip("/\\")) or x["run_dir"] == target]
            if not hit:
                _print_error(f"no export called {target!r} (see: exports)")
                return 2
            import abx_web
            abx_web._export_delete(C.Session(), hit[0]["run_dir"])
            print(f"deleted {S(hit[0]['name'])}")
            return 0
    except _errors() as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    if "--json" in args:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    print(f"\n  {len(rows)} exports in {S(C.home_dir())}\n")
    for x in rows:
        print(f"  {('PARTIAL' if x.get('partial') else x['status']).ljust(10)} {S(x['name'])}   "
              f"{x.get('rows_written') or 0:,} events" + (f"   {S(x['merged_file'])}" if x.get("merged_file") else ""))
        if x.get("partial_note"):
            print(f"             {S(x['partial_note'])}")
    if any(x.get("resumable") for x in rows):
        print("\n  resume one with:  export --resume \"<folder>\" --merge")
    print()
    return 0


def searches_cmd(args):
    """Your saved searches (on this computer, in the exports folder), as the browser app and terminal list them."""
    import json
    import abx_core as C
    S = C.sanitize
    try:
        saved = C.saved_list()
    except OSError as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    if "--json" in args:
        print(json.dumps(saved, indent=2))
        return 0
    print(f"\n  {len(saved)} saved searches in {S(os.path.join(C.home_dir(), 'saved-searches'))}\n")
    for x in saved:
        sm = x.get("summary") or {}
        tag = "   (runs with export --from-saved)" if x.get("mode") == "advanced" else \
            "   (KQL: runs only where the tenant supports KQL)" if x.get("mode") == "kql" else ""
        print(f"  {S(x['name'])}{tag}\n      {S(sm.get('query', ''))} · {S(sm.get('window', ''))}")
    if saved:
        print("\n  export one with:  export --from-saved \"<name>\" --merge   (its own time range, or add --last)\n")
    return 0


def from_saved_argv(argv):
    """export --from-saved NAME: the saved search's dataset, filters, fields, format and time range become the
    command line, exactly as the browser app and terminal run it. Options given on the command line win for the time
    range, format and output; the search itself cannot be mixed with --dataset / --where."""
    import abx_core as C
    import abx_engine as E
    name = _flag_value(argv, "--from-saved")
    rest, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--from-saved":
            skip = True
            continue
        if a.startswith("--from-saved="):
            continue
        rest.append(a)
    if not name:
        raise ValueError("--from-saved needs the name of a saved search (see: searches)")
    clash = [f for f in ("--dataset", "--where", "--condition-json", "--condition-file", "--dsl-json", "--dsl-file",
                         "--from-view", "--from-file", "--resume") if any(a == f or a.startswith(f + "=") for a in rest)]
    if clash:
        raise ValueError("--from-saved already holds the whole search; do not add " + ", ".join(clash))
    try:
        spec = C.saved_load(name)
    except E.UserError as ex:
        raise ValueError(str(ex) + " (see: searches)")
    has = lambda f: any(a == f or a.startswith(f + "=") for a in rest)
    if spec["mode"] == "kql":
        # runs as KQL; the export first checks that this tenant applies KQL, and refuses if it does not
        if not (spec.get("kql") or "").strip():
            raise ValueError("that saved search has no KQL text")
        extra = ["--kql", spec["kql"]]
    else:
        probs = [p for p in C.spec_problems(dict(spec, time={"last": "1h"})) if p]
        if probs:
            raise ValueError(probs[0])
        extra = C.query_args(spec)
    if not (has("--fields") or has("--every-field")):
        extra += C.fields_args(spec)
    if not has("--format"):
        extra += ["--format", spec["format"]]
    if not (has("--last") or has("--start") or has("--end")):
        t = spec.get("time") or {}
        extra += ["--last", t["last"]] if t.get("last") and not (t.get("start") or t.get("end")) else \
            ["--start", t.get("start") or "", "--end", t.get("end") or ""]
    return rest[:1] + extra + rest[1:]


def fields_cmd(args):
    """The dataset field profile: which fields a dataset populates, how often, and the suggested export list."""
    import json
    import abx_core as C
    S = C.sanitize
    try:
        s = _connect(args)
        if isinstance(s, int):
            return s
        # Profiles are learned live and kept in memory only (the kit stores no tenant data), so this command
        # always samples now; --refresh is accepted for compatibility.
        if "--all" in args:
            quiet = "--json" in args
            profs = s.profile_all(sink=(lambda line: None) if quiet else (lambda line: print(S(line))), refresh=True)
            if quiet:
                print(json.dumps(profs, indent=2))
            return 0
        dataset = _flag_value(args, "--dataset")
        if not dataset:
            print("usage: fields --dataset NAME [--json] [--expect-tenant ID]   |   "
                  "fields --all [--expect-tenant ID]", file=sys.stderr)
            return 2
        import abx_engine as E
        dataset = E.check_dataset_name(s.client, dataset,       # a wrong name is not "a dataset with no events"
                                       "--allow-unlisted-dataset" in args)
        p = s.dataset_fields(dataset, refresh=True)
    except _errors() as ex:
        _print_error(f"{C.humanize(ex)}")
        return 2
    if "--json" in args:
        print(json.dumps(p, indent=2))
        return 0 if p.get("sampled") else 1
    if not p.get("sampled"):
        print(f"{S(p['dataset'])}: no events in the last 30 days — nothing to learn fields from")
        return 1
    import abx_ui as U
    src = "learned now"
    meta = f"{len(p['fields'])} fields · {p['sampled']:,} sampled events · {src} {str(p.get('profiled') or '')[:10]}"
    print(f"\n  {U.bold(S(p['dataset']))}  {U.dim(meta)}\n")
    sug = set(p.get("suggested") or [])
    for f in p["fields"]:
        cov = f.get("coverage") or 0
        star = U.teal(" ★ suggested") if f["name"] in sug else ""
        print(f"  {U.bar(cov / 100.0, 14)} {cov:>5.1f}%  {S(f['name'])}{star}")
    sug_fields = [f for f in p.get("suggested") or [] if f not in ("@timestamp", "id")]   # always added anyway
    print("\n  " + U.dim("ready to paste (quoted, so it also works in PowerShell):") + "\n  --fields \""
          + S(",".join(sug_fields)) + "\"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
