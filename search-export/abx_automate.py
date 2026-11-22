"""Automation recipes: the files that run one search as a scheduled export on cron, launchd, systemd, the Windows Task
Scheduler, GitHub Actions or a container (Docker or a Kubernetes CronJob).

Every recipe:
- never holds the API key: it comes from ABSTRACT_API_KEY_COMMAND (your keychain or secret manager) on a computer, or
  from the platform's secret store (an Actions secret, a Kubernetes secret) in the cloud;
- sets ABSTRACT_EXPECT_TENANT, so a mixed-up key can never export from another tenant;
- exports fixed, back-to-back windows: each run takes the last whole interval that ended 10 minutes ago, so runs
  neither overlap nor leave gaps, and a run that is retried resumes its own folder;
- treats exit code 0 as success and 3 as success with a warning, and anything else as a failure, which it reports
  to $ABX_ALERT_WEBHOOK (a JSON POST) when that is set;
- keeps one log per run and deletes logs older than 30 days.
"""
import json
import os
import plistlib
import re
import shlex
import sys
from typing import Any, Dict, List, Optional

import abx_core as C
import abx_engine as E

TARGETS = ("cron", "launchd", "systemd", "windows-task", "github-actions", "container-cron")
EVERY = {"15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
LAG_S = 600
HERE = os.path.dirname(os.path.abspath(__file__))


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:40] or "search"


# Marks an argument that names a file next to the script. Not "@": field lists start with "@timestamp" (the first
# live run of a generated job tripped over exactly that).
FILE = "\x00file:"


def _search_files(spec) -> Dict[str, Any]:
    """The search as files next to the script, so no shell or YAML quoting can change it: a condition JSON or a KQL
    text file, plus the arguments that refer to them (relative names; the script resolves them)."""
    spec = C.normalize_spec(spec)
    files: Dict[str, str] = {}
    args: List[str] = []
    if spec["mode"] == "kql":
        if not (spec.get("kql") or "").strip():
            raise E.UserError("this search has no KQL text")
        files["search.kql"] = spec["kql"].strip() + "\n"
        args += ["--kql-file", FILE + "search.kql"]
    elif spec["mode"] == "advanced":
        obj = json.loads(spec["advanced"])
        name = "search-dsl.json" if spec.get("advanced_kind") == "dsl" else "search-condition.json"
        files[name] = json.dumps(obj, indent=2) + "\n"
        args += ["--dsl-file" if name == "search-dsl.json" else "--condition-file", FILE + name]
    else:
        probs = [p for p in C.spec_problems(dict(spec, time={"last": "1h"})) if p]
        if probs:
            raise E.UserError(probs[0])
        cond = C.build_condition(dict(spec, dataset=""))
        if cond.get("conditions") or cond.get("groups"):
            files["search-condition.json"] = json.dumps(cond, indent=2) + "\n"
            args += ["--condition-file", FILE + "search-condition.json"]
        if spec["dataset"]:
            args += ["--dataset", spec["dataset"]]
    if spec["storage"] != "auto":
        args += ["--storage", spec["storage"]]
    args += C.fields_args(spec) + ["--format", spec["format"] or "ndjson.gz", "--merge"]
    files["search.json"] = json.dumps(spec, indent=2, sort_keys=True) + "\n"      # the search, for people to read
    return {"files": files, "args": args}


def _sh_quote(a):
    return shlex.quote(a)


def _ps_quote(a):
    return "'" + str(a).replace("'", "''") + "'"


def _sh_args(args):
    out = []
    for a in args:
        out.append('"$HERE/' + a[len(FILE):] + '"' if a.startswith(FILE) else _sh_quote(a))
    return " ".join(out)


def _ps_args(args):
    return ", ".join("(Join-Path $Here " + _ps_quote(a[len(FILE):]) + ")" if a.startswith(FILE) else _ps_quote(a)
                     for a in args)


WINDOW_PY = ('import datetime as d,sys;s=int(sys.argv[1]);lag=int(sys.argv[2]);'
             'n=d.datetime.now(d.timezone.utc).timestamp()-lag;e=int(n//s*s);'
             'f=lambda t:d.datetime.fromtimestamp(t,d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ");'
             'print(f(e-s),f(e))')
# 1 when the run's result line says nothing matched (the export's last JSON line has zero_result true)
EMPTY_PY = ('import json,sys;r=[l for l in open(sys.argv[1],encoding="utf-8",errors="replace") '
            'if l.startswith("{") and "zero_result" in l];'
            'print(1 if r and json.loads(r[-1]).get("zero_result") else 0)')
ALERT_PY = ('import json,os,sys,urllib.request as u;'
            'b=json.dumps({"tool":"abstract-search-export","job":sys.argv[1],"tenant":sys.argv[2],'
            '"window":[sys.argv[3],sys.argv[4]],"exit_code":int(sys.argv[5]),"log":sys.argv[6],'
            '"reason":sys.argv[7]}).encode();'
            'r=u.Request(os.environ["ABX_ALERT_WEBHOOK"],data=b,headers={"Content-Type":"application/json"});'
            'u.urlopen(r,timeout=20).read()')


def _sh_script(job, vid, base_url, every_s, args, app_path, key_hint):
    return f"""#!/bin/sh
# Abstract Search & Export: scheduled export "{job}".
# Runs the export for the last whole {_every_words(every_s)} window that ended 10 minutes ago. Never put the API key
# in this file: set ABSTRACT_API_KEY_COMMAND (a command that prints the key from your keychain or secret manager)
# or provide ABSTRACT_API_KEY from your platform's secret store.
# Alerts go to ABX_ALERT_WEBHOOK (a JSON POST) and nowhere until you set it: on a failure, on a run that exported
# nothing (ABX_ALERT_ON_EMPTY=0 turns that one off), and on exit code 3 (the export finished, but part of the range
# could not be searched, for example the archive; ABX_ALERT_ON_WARNING=0 turns that one off).
set -eu

# schedulers start jobs with almost no PATH: find python3 where installers put it
PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:${{PATH:-}}"
export PATH
HERE=$(cd "$(dirname "$0")" && pwd)
PY="${{ABX_PYTHON:-python3}}"
APP="${{ABX_APP:-{app_path}}}"
JOB={_sh_quote(job)}
EVERY={every_s}
: "${{ABSTRACT_VENDOR_ACCOUNT_ID:={vid}}}"
: "${{ABSTRACT_EXPECT_TENANT:={vid}}}"
: "${{ABSTRACT_BASE_URL:={base_url}}}"
: "${{ABX_OUT_ROOT:=${{HOME:-/tmp}}/AbstractExports}}"
: "${{ABX_LOG_DIR:=$ABX_OUT_ROOT/automation-logs}}"
: "${{ABX_ALERT_ON_EMPTY:=1}}"
: "${{ABX_ALERT_ON_WARNING:=1}}"
export ABSTRACT_VENDOR_ACCOUNT_ID ABSTRACT_EXPECT_TENANT ABSTRACT_BASE_URL ABX_OUT_ROOT
if [ -z "${{ABSTRACT_API_KEY_COMMAND:-}}" ] && [ -z "${{ABSTRACT_API_KEY:-}}" ]; then
  # {key_hint}
  echo "set ABSTRACT_API_KEY_COMMAND (or ABSTRACT_API_KEY from a secret store) first" >&2
  exit 2
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "$PY was not found: install Python 3.8 or newer, or set ABX_PYTHON to its full path" >&2
  exit 2
fi
if [ ! -f "$APP" ]; then
  echo "$APP was not found: set ABX_APP to the full path of abstract_search.py" >&2
  exit 2
fi

mkdir -p "$ABX_LOG_DIR" "$ABX_OUT_ROOT/$JOB"
LOG="$ABX_LOG_DIR/$JOB-$(date -u +%Y%m%dT%H%M%SZ).log"
WINDOW=$("$PY" -c {_sh_quote(WINDOW_PY)} "$EVERY" {LAG_S})
START=${{WINDOW% *}}
END=${{WINDOW#* }}
STAMP=$(printf '%s' "$START" | tr -d ':-')
OUT="$ABX_OUT_ROOT/$JOB/$STAMP"

set +e
if [ -f "$OUT/manifest.json" ]; then
  "$PY" "$APP" export --resume "$OUT" --merge --quiet >>"$LOG" 2>&1
else
  "$PY" "$APP" export {_sh_args(args)} --start "$START" --end "$END" --out "$OUT" --quiet >>"$LOG" 2>&1
fi
RC=$?
set -e

alert() {{
  if [ -n "${{ABX_ALERT_WEBHOOK:-}}" ]; then
    "$PY" -c {_sh_quote(ALERT_PY)} "$JOB" "$ABSTRACT_EXPECT_TENANT" "$START" "$END" "$RC" "$LOG" "$1" >>"$LOG" 2>&1 || true
  fi
}}
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
case "$RC" in
  0)
    EMPTY=$("$PY" -c {_sh_quote(EMPTY_PY)} "$LOG" 2>/dev/null || echo 0)
    if [ "$EMPTY" = "1" ]; then
      echo "$NOW $JOB $START..$END complete, but NO events matched" >>"$LOG"
      if [ "${{GITHUB_ACTIONS:-}}" = "true" ]; then echo "::warning::$JOB $START..$END exported no events"; fi
      if [ "$ABX_ALERT_ON_EMPTY" != "0" ]; then alert empty; fi
    else
      echo "$NOW $JOB $START..$END complete" >>"$LOG"
    fi
    ;;
  3)
    echo "$NOW $JOB $START..$END complete with a warning (see above)" >>"$LOG"
    if [ "${{GITHUB_ACTIONS:-}}" = "true" ]; then echo "::warning::$JOB $START..$END finished with a warning (exit 3): see the log"; fi
    if [ "$ABX_ALERT_ON_WARNING" != "0" ]; then alert warning; fi
    ;;
  *)
    echo "$NOW $JOB $START..$END FAILED with exit code $RC" >>"$LOG"
    alert failed
    ;;
esac

# keep 30 days of logs
find "$ABX_LOG_DIR" -name "$JOB-*.log" -type f -mtime +30 -exec rm -f {{}} \\; 2>/dev/null || true

if [ "$RC" -eq 0 ] || [ "$RC" -eq 3 ]; then
  exit 0
fi
exit "$RC"
"""


def _ps_script(job, vid, base_url, every_s, args, app_path):
    return f"""# Abstract Search & Export: scheduled export "{job}" (Windows PowerShell 5.1 or PowerShell 7).
# Runs the export for the last whole {_every_words(every_s)} window that ended 10 minutes ago. Never put the API key
# in this file: set ABSTRACT_API_KEY_COMMAND to a command that prints it from your secret store, for example
#   powershell -NoProfile -Command "Get-Secret -Name abstract-search-export -AsPlainText"
# (the Microsoft.PowerShell.SecretManagement module with a registered vault), or an Azure Key Vault command.
# Alerts go to ABX_ALERT_WEBHOOK (a JSON POST) and nowhere until you set it: on a failure, on a run that exported
# nothing (ABX_ALERT_ON_EMPTY=0 turns that one off), and on exit code 3 (the export finished, but part of the range
# could not be searched; ABX_ALERT_ON_WARNING=0 turns that one off).
$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$Job = {_ps_quote(job)}
$Every = {every_s}
if (-not $env:ABSTRACT_VENDOR_ACCOUNT_ID) {{ $env:ABSTRACT_VENDOR_ACCOUNT_ID = {_ps_quote(vid)} }}
if (-not $env:ABSTRACT_EXPECT_TENANT) {{ $env:ABSTRACT_EXPECT_TENANT = {_ps_quote(vid)} }}
if (-not $env:ABSTRACT_BASE_URL) {{ $env:ABSTRACT_BASE_URL = {_ps_quote(base_url)} }}
if (-not $env:ABX_OUT_ROOT) {{ $env:ABX_OUT_ROOT = Join-Path $env:USERPROFILE 'AbstractExports' }}
$LogDir = if ($env:ABX_LOG_DIR) {{ $env:ABX_LOG_DIR }} else {{ Join-Path $env:ABX_OUT_ROOT 'automation-logs' }}
$App = if ($env:ABX_APP) {{ $env:ABX_APP }} else {{ {_ps_quote(app_path)} }}
$Py = if ($env:ABX_PYTHON) {{ $env:ABX_PYTHON }} else {{ 'py' }}
if (-not $env:ABSTRACT_API_KEY_COMMAND -and -not $env:ABSTRACT_API_KEY) {{
  Write-Error 'Set ABSTRACT_API_KEY_COMMAND (or ABSTRACT_API_KEY from a secret store) first.'
  exit 2
}}
if (-not (Test-Path -LiteralPath $App)) {{
  Write-Error "$App was not found: set ABX_APP to the full path of abstract_search.py."
  exit 2
}}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $env:ABX_OUT_ROOT $Job) | Out-Null
$Log = Join-Path $LogDir ("{{0}}-{{1}}.log" -f $Job, [DateTime]::UtcNow.ToString("yyyyMMdd'T'HHmmss'Z'"))
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - {LAG_S}
$e = [long]([math]::Floor($now / $Every) * $Every)
$Start = [DateTimeOffset]::FromUnixTimeSeconds($e - $Every).UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$End = [DateTimeOffset]::FromUnixTimeSeconds($e).UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$Out = Join-Path (Join-Path $env:ABX_OUT_ROOT $Job) ($Start -replace '[:-]', '')

$ErrorActionPreference = 'Continue'
if (Test-Path (Join-Path $Out 'manifest.json')) {{
  $argv = @($App, 'export', '--resume', $Out, '--merge', '--quiet')
}} else {{
  $argv = @($App, 'export', {_ps_args(args)}, '--start', $Start, '--end', $End, '--out', $Out, '--quiet')
}}
if ($Py -eq 'py') {{ $argv = @('-3') + $argv }}
& $Py @argv *>> $Log
$Rc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'

$stamp = [DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
function Send-Alert([string]$Reason) {{
  if ($env:ABX_ALERT_WEBHOOK) {{
    $body = @{{ tool = 'abstract-search-export'; job = $Job; tenant = $env:ABSTRACT_EXPECT_TENANT;
               window = @($Start, $End); exit_code = $Rc; log = $Log; reason = $Reason }} | ConvertTo-Json -Compress
    try {{ Invoke-RestMethod -Method Post -Uri $env:ABX_ALERT_WEBHOOK -ContentType 'application/json' -Body $body -TimeoutSec 20 | Out-Null }} catch {{ }}
  }}
}}
$empty = $false
$last = Get-Content -Path $Log -ErrorAction SilentlyContinue | Where-Object {{ $_ -like '{{*"zero_result"*' }} | Select-Object -Last 1
if ($last) {{ try {{ $empty = [bool](($last | ConvertFrom-Json).zero_result) }} catch {{ }} }}
if ($Rc -eq 0 -and $empty) {{
  Add-Content -Path $Log -Value ("{{0}} {{1}} {{2}}..{{3}} complete, but NO events matched" -f $stamp, $Job, $Start, $End)
  if ($env:ABX_ALERT_ON_EMPTY -ne '0') {{ Send-Alert 'empty' }}
}} elseif ($Rc -eq 0) {{
  Add-Content -Path $Log -Value ("{{0}} {{1}} {{2}}..{{3}} complete" -f $stamp, $Job, $Start, $End)
}} elseif ($Rc -eq 3) {{
  Add-Content -Path $Log -Value ("{{0}} {{1}} {{2}}..{{3}} complete with a warning (see above)" -f $stamp, $Job, $Start, $End)
  if ($env:ABX_ALERT_ON_WARNING -ne '0') {{ Send-Alert 'warning' }}
}} else {{
  Add-Content -Path $Log -Value ("{{0}} {{1}} {{2}}..{{3}} FAILED with exit code {{4}}" -f $stamp, $Job, $Start, $End, $Rc)
  Send-Alert 'failed'
}}

# keep 30 days of logs
Get-ChildItem -Path $LogDir -Filter "$Job-*.log" -File -ErrorAction SilentlyContinue |
  Where-Object {{ $_.LastWriteTime -lt (Get-Date).AddDays(-30) }} | Remove-Item -Force -ErrorAction SilentlyContinue

if ($Rc -eq 0 -or $Rc -eq 3) {{ exit 0 }}
exit $Rc
"""


def _every_words(s):
    return {900: "15-minute", 3600: "hour", 21600: "6-hour", 86400: "day"}.get(s, f"{s}-second")


def _cron_expr(every, at_h, at_m):
    return {"15m": "*/15 * * * *", "1h": f"{at_m} * * * *", "6h": f"{at_m} */6 * * *",
            "1d": f"{at_m} {at_h} * * *"}[every]


def _oncalendar(every, at_h, at_m):
    return {"15m": "*:0/15", "1h": f"*-*-* *:{at_m:02d}:00 UTC", "6h": f"*-*-* 00/6:{at_m:02d}:00 UTC",
            "1d": f"*-*-* {at_h:02d}:{at_m:02d}:00 UTC"}[every]


def recipe(spec, target, vendor_account_id, base_url=None, every="1d", at="00:20", job=None, app_path=None,
           folder=None):
    """{files: [{path, text, mode}], steps: [...], notes: [...], target, job, every}. spec is a saved search or a
    search spec (abx_core.empty_spec shape); vendor_account_id is the tenant the job may run against."""
    if target not in TARGETS:
        raise E.UserError("choose one of: " + ", ".join(TARGETS))
    if every not in EVERY:
        raise E.UserError("run it every 15m, 1h, 6h or 1d")
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(at or "00:20"))
    if not m:
        raise E.UserError("--at takes a UTC time such as 00:20")
    at_h, at_m = int(m.group(1)), int(m.group(2))
    if not vendor_account_id or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", str(vendor_account_id)):
        raise E.UserError("say which tenant the job runs against (its vendor account ID)")
    spec = C.normalize_spec(spec)
    job = _slug(job or spec.get("name") or spec.get("dataset") or "search")
    base_url = (base_url or C.default_base_url()).rstrip("/")
    every_s = EVERY[every]
    sf = _search_files(spec)
    folder = folder or f"/path/to/abstract-automation/{job}"
    app_local = app_path or os.path.join(HERE, "abstract_search.py")
    files: List[Dict[str, Any]] = [{"path": p, "text": t, "mode": 0o644} for p, t in sf["files"].items()]
    steps: List[str] = []
    notes = ["Each run exports the last whole " + _every_words(every_s) + " that ended 10 minutes before it ran, so "
             "back-to-back runs cover time without gaps or overlap. Events that arrive later than that are not in "
             "the export; widen the lag by running later if your sources are slow.",
             "Alerts are posted as JSON to ABX_ALERT_WEBHOOK, and go nowhere until you set it. A run alerts when it "
             "fails, when it exported nothing (set ABX_ALERT_ON_EMPTY=0 if this search is often empty), and on exit "
             "code 3: the export finished but part of the range, such as the archive, could not be searched (set "
             "ABX_ALERT_ON_WARNING=0 to stop that one). Exit codes 0 and 3 end the job as a success; anything else "
             "ends it as a failure."]
    mac_key = f"security find-generic-password -s abstract-search-export -a {vendor_account_id} -w"
    linux_key = f"secret-tool lookup service abstract-search-export account {vendor_account_id}"
    on_mac = sys.platform == "darwin"
    if target in ("cron", "launchd", "systemd"):
        hint = mac_key if target == "launchd" or (target == "cron" and on_mac) else linux_key
        files.append({"path": "run-export.sh", "text": _sh_script(job, vendor_account_id, base_url, every_s, sf["args"],
                                                                  app_local, "e.g. ABSTRACT_API_KEY_COMMAND='" + hint +
                                                                  "'"), "mode": 0o755})
        store = (f"security add-generic-password -s abstract-search-export -a {vendor_account_id} -w"
                 if target == "launchd" or (target == "cron" and on_mac) else
                 f"secret-tool store --label='Abstract API key' service abstract-search-export account "
                 f"{vendor_account_id}")
        steps.append(f"Store the API key once in your keychain: {store}")
        steps.append(f"Copy this folder to {folder} (the scripts find their files next to themselves).")
    if target == "cron":
        cron_key = mac_key if on_mac else linux_key
        line = (f"{_cron_expr(every, at_h, at_m)} ABSTRACT_API_KEY_COMMAND='{cron_key}' "
                f"/bin/sh {folder}/run-export.sh")
        files.append({"path": "crontab.txt", "text": "# cron runs in the computer's local time; the export window "
                                                     "is always computed in UTC.\n" + line + "\n", "mode": 0o644})
        steps += ["Run `crontab -e` and add the line in crontab.txt (on macOS the launchd recipe is the native "
                  "choice; cron works too, with the keychain command shown).",
                  f"Test it once by hand: ABSTRACT_API_KEY_COMMAND='{cron_key}' sh {folder}/run-export.sh"]
    elif target == "launchd":
        label = f"com.abstract.search-export.{job}"
        sched: Dict[str, Any] = {"StartCalendarInterval": {"Hour": at_h, "Minute": at_m}} if every == "1d" else \
            {"StartInterval": every_s}
        pl = {"Label": label, "ProgramArguments": ["/bin/sh", f"{folder}/run-export.sh"],
              "EnvironmentVariables": {"ABSTRACT_API_KEY_COMMAND": mac_key,
                                       "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
              "StandardOutPath": f"{folder}/launchd.out.log", "StandardErrorPath": f"{folder}/launchd.err.log",
              "RunAtLoad": False}
        pl.update(sched)
        files.append({"path": f"{label}.plist", "text": plistlib.dumps(pl).decode("utf-8"), "mode": 0o644})
        steps += [f"Copy {label}.plist to ~/Library/LaunchAgents/",
                  f"Load it: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist",
                  f"Run it once now to test: launchctl kickstart gui/$(id -u)/{label}",
                  "launchd runs daily jobs in the Mac's local time; the export window is always computed in UTC."]
    elif target == "systemd":
        unit = f"abstract-export-{job}"
        files.append({"path": f"{unit}.service", "text": f"""[Unit]
Description=Abstract Search & Export: {job}
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=ABSTRACT_API_KEY_COMMAND={linux_key}
ExecStart=/bin/sh {folder}/run-export.sh
""", "mode": 0o644})
        files.append({"path": f"{unit}.timer", "text": f"""[Unit]
Description=Run {unit} every {every}

[Timer]
OnCalendar={_oncalendar(every, at_h, at_m)}
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
""", "mode": 0o644})
        steps += [f"Copy {unit}.service and {unit}.timer to ~/.config/systemd/user/",
                  f"systemctl --user daemon-reload && systemctl --user enable --now {unit}.timer",
                  "So it runs while you are logged out: loginctl enable-linger $USER",
                  f"Run it once now: systemctl --user start {unit}.service; see the result with journalctl --user -u "
                  f"{unit}"]
    elif target == "windows-task":
        win_folder = folder if re.match(r"^[A-Za-z]:\\", folder) else f"C:\\abstract-automation\\{job}"
        win_app = app_path or "C:\\abstract-search-export\\app\\abstract_search.py"
        files.append({"path": "run-export.ps1", "text": _ps_script(job, vendor_account_id, base_url, every_s,
                                                                    sf["args"], win_app), "mode": 0o644})
        if every == "1d":
            trig = f"New-ScheduledTaskTrigger -Daily -At ([DateTime]::Parse('{at_h:02d}:{at_m:02d}'))"
        else:
            trig = (f"New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes({at_m}) "
                    f"-RepetitionInterval (New-TimeSpan -Seconds {every_s}) -RepetitionDuration (New-TimeSpan -Days 3650)")
        files.append({"path": "register-task.ps1", "text": f"""# Registers the scheduled task for "{job}". Run once in PowerShell as the user the task runs as.
$ErrorActionPreference = 'Stop'
$Folder = {_ps_quote(win_folder)}
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Folder 'run-export.ps1') + '"')
$Trigger = {trig}
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName {_ps_quote('Abstract export ' + job)} -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Abstract Search & Export scheduled export' -Force
""", "mode": 0o644})
        steps += [f"Copy this folder to {win_folder}",
                  "Store the key in a secret vault: Install-Module Microsoft.PowerShell.SecretManagement, "
                  "Microsoft.PowerShell.SecretStore; Register-SecretVault -Name local -ModuleName "
                  "Microsoft.PowerShell.SecretStore; Set-Secret -Name abstract-search-export",
                  "Set ABSTRACT_API_KEY_COMMAND for your user: [Environment]::SetEnvironmentVariable("
                  "'ABSTRACT_API_KEY_COMMAND', 'powershell -NoProfile -Command \"Get-Secret -Name "
                  "abstract-search-export -AsPlainText\"', 'User')",
                  f"Register the task: powershell -ExecutionPolicy Bypass -File {win_folder}\\register-task.ps1",
                  "The task runs only while you are signed in. To run it signed out, open Task Scheduler, choose "
                  "the task, and select Run whether user is logged on or not (Windows asks for your password).",
                  "Daily tasks start in the computer's local time; the export window is always computed in UTC."]
    elif target == "github-actions":
        files.append({"path": "run-export.sh", "text": _sh_script(job, vendor_account_id, base_url, every_s, sf["args"],
                                                                  "search-export/abstract_search.py",
                                                                  "in Actions the key comes from the "
                                                                  "ABSTRACT_API_KEY secret"), "mode": 0o755})
        wf = f"abstract-export-{job}.yml"
        files.append({"path": wf, "text": f"""name: Abstract export {job}

on:
  schedule:
    - cron: "{_cron_expr(every, at_h, at_m)}"
  workflow_dispatch: {{}}

permissions:
  contents: read

concurrency:
  group: abstract-export-{job}
  cancel-in-progress: false

jobs:
  export:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    env:
      ABSTRACT_API_KEY: ${{{{ secrets.ABSTRACT_API_KEY }}}}
      ABSTRACT_VENDOR_ACCOUNT_ID: ${{{{ vars.ABSTRACT_VENDOR_ACCOUNT_ID }}}}
      ABSTRACT_EXPECT_TENANT: ${{{{ vars.ABSTRACT_VENDOR_ACCOUNT_ID }}}}
      ABX_ALERT_WEBHOOK: ${{{{ secrets.ABX_ALERT_WEBHOOK }}}}
      ABX_OUT_ROOT: ${{{{ runner.temp }}}}/exports
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Export
        run: sh automation/{job}/run-export.sh
      - name: Keep the export as a workflow artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: abstract-export-{job}-${{{{ github.run_id }}}}
          path: ${{{{ runner.temp }}}}/exports
          retention-days: 7
""", "mode": 0o644})
        steps += ["Put the kit's source in the repository at search-export/ (or change ABX_APP in run-export.sh).",
                  f"Copy run-export.sh and the search files to automation/{job}/ in the repository, and {wf} to "
                  ".github/workflows/.",
                  "In the repository's Settings → Secrets and variables → Actions, add the secret ABSTRACT_API_KEY "
                  "(and ABX_ALERT_WEBHOOK if you want alerts), and the variable ABSTRACT_VENDOR_ACCOUNT_ID.",
                  "Run it once from the Actions tab (Run workflow) before relying on the schedule."]
        notes.append("GitHub keeps each export as a workflow artifact for 7 days, in GitHub's cloud. Exports hold your "
                     "events: use a private repository and shorten retention-days, or replace that step with an upload "
                     "to your own storage. Scheduled workflows can start several minutes late at busy times.")
    elif target == "container-cron":
        files.append({"path": "run-export.sh", "text": _sh_script(job, vendor_account_id, base_url, every_s, sf["args"],
                                                                  "/opt/abstract/app/abstract_search.py",
                                                                  "in a container the key comes from a secret as "
                                                                  "ABSTRACT_API_KEY"), "mode": 0o755})
        files.append({"path": "Dockerfile", "text": f"""# Abstract Search & Export scheduled job "{job}". Build from a folder holding this Dockerfile, the job's
# files and the kit's app/ folder.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 abx
WORKDIR /opt/abstract
COPY app/ /opt/abstract/app/
COPY run-export.sh search*.json search*.kql /opt/abstract/job/
ENV ABX_OUT_ROOT=/exports
USER 10001
ENTRYPOINT ["/bin/sh", "/opt/abstract/job/run-export.sh"]
""", "mode": 0o644})
        files.append({"path": "cronjob.yaml", "text": f"""apiVersion: batch/v1
kind: CronJob
metadata:
  name: abstract-export-{job}
spec:
  schedule: "{_cron_expr(every, at_h, at_m)}"
  timeZone: "Etc/UTC"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          restartPolicy: Never
          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
          containers:
            - name: export
              image: REGISTRY/abstract-export-{job}:latest
              env:
                - name: ABSTRACT_VENDOR_ACCOUNT_ID
                  value: "{vendor_account_id}"
                - name: ABSTRACT_EXPECT_TENANT
                  value: "{vendor_account_id}"
                - name: ABSTRACT_API_KEY
                  valueFrom:
                    secretKeyRef:
                      name: abstract-search-export
                      key: api-key
                - name: ABX_ALERT_WEBHOOK
                  valueFrom:
                    secretKeyRef:
                      name: abstract-search-export
                      key: alert-webhook
                      optional: true
              volumeMounts:
                - name: exports
                  mountPath: /exports
          volumes:
            - name: exports
              persistentVolumeClaim:
                claimName: abstract-exports
""", "mode": 0o644})
        steps += ["Copy the kit's app/ folder next to these files.",
                  f"docker build -t REGISTRY/abstract-export-{job}:latest . && docker push "
                  f"REGISTRY/abstract-export-{job}:latest",
                  "kubectl create secret generic abstract-search-export --from-literal=api-key=… "
                  "(or from your secret manager), and a PersistentVolumeClaim called abstract-exports",
                  "Replace REGISTRY in cronjob.yaml, then: kubectl apply -f cronjob.yaml",
                  f"Without Kubernetes, run the image from the host's scheduler: docker run --rm -e "
                  f"ABSTRACT_API_KEY -v /srv/abstract-exports:/exports REGISTRY/abstract-export-{job}:latest"]
        notes.append("Exports are written to the /exports volume. Keep it on encrypted storage: exports hold your "
                     "events.")
    files.append({"path": "README.txt", "text": _readme(job, target, every, at, steps, notes), "mode": 0o644})
    for f in files:
        if re.search(r"(?i)\b(bearer\s+[a-z0-9._-]{12,})", f["text"]):
            raise E.UserError("a recipe file looks like it holds a key; nothing was written")
    return {"target": target, "job": job, "every": every, "files": files, "steps": steps, "notes": notes,
            "folder": folder}


def _readme(job, target, every, at, steps, notes):
    lines = [f"Abstract Search & Export: scheduled export \"{job}\" ({target}, every {every}"
             + (f" at {at} UTC" if every == "1d" else "") + ")", "", "Steps:"]
    lines += [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
    lines += ["", "Notes:"] + [f"  - {n}" for n in notes] + [""]
    return "\n".join(lines)


def write(r, out_dir):
    """Write a recipe's files into out_dir with their modes. Returns the paths written."""
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for f in r["files"]:
        p = os.path.join(out_dir, f["path"])
        with open(p, "w", encoding="utf-8", newline="\n" if not f["path"].endswith(".ps1") else "\r\n") as fh:
            fh.write(f["text"])
        try:
            os.chmod(p, f["mode"])
        except OSError:
            pass
        paths.append(p)
    return paths


def from_saved(name):
    return C.saved_load(name)
