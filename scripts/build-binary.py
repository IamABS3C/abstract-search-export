#!/usr/bin/env python3
"""Build the single-file program for this computer's system and processor, then prove it starts.

    python3 -m pip install pyinstaller==6.22.3
    python3 scripts/build-binary.py              # writes dist/abstract-search-<version>-<system>-<arch>[.exe]

The result needs no Python and no install: it carries Python and the tool. It is built from search-export/ exactly
as it is in this repository, with the browser app's page, the in-app help and the examples inside it.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "search-export")
MODULES = ["abx_web", "abx_mcp", "abx_menu", "abx_commands", "abx_wizard", "abx_tui_platform", "abx_content",
           "abx_automate", "abx_platform", "abx_kql", "abx_ops", "abx_brand", "abx_core", "abx_engine", "abx_ui"]
DATA = ["web", "docs", "examples"]                       # folders the program reads from beside itself
SKIP = {os.path.join("docs", "screenshots")}             # the guide's pictures: not read by the program


def target():
    system = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system(), platform.system().lower())
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(),
                                                                                    platform.machine().lower())
    return system, arch


def main():
    version = open(os.path.join(KIT, "VERSION"), encoding="utf-8").read().strip()
    system, arch = target()
    name = f"abstract-search-{version}-{system}-{arch}"
    work = os.path.join(ROOT, "build")
    stage = os.path.join(work, "data")
    shutil.rmtree(work, ignore_errors=True)
    for d in DATA:                                         # a clean copy, so nothing but the program's files goes in
        shutil.copytree(os.path.join(KIT, d), os.path.join(stage, d),
                        ignore=lambda folder, names: [n for n in names
                                                      if os.path.relpath(os.path.join(folder, n), KIT) in SKIP
                                                      or n == "__pycache__" or n == ".DS_Store"])
    sep = os.pathsep
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--noconfirm", "--clean",
           "--log-level", "WARN", "--name", name, "--paths", KIT,
           "--distpath", os.path.join(ROOT, "dist"), "--workpath", os.path.join(work, "work"), "--specpath", work,
           "--add-data", os.path.join(KIT, "VERSION") + sep + "."]
    for d in DATA:
        cmd += ["--add-data", os.path.join(stage, d) + sep + d]
    for m in MODULES:
        cmd += ["--hidden-import", m]
    cmd.append(os.path.join(KIT, "abstract_search.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe = os.path.join(ROOT, "dist", name + (".exe" if system == "windows" else ""))
    prove(exe, version)
    print(f"built {exe} ({os.path.getsize(exe) // 1024:,} KB)")


def prove(exe, version):
    """The program reports its version, lists its commands and serves its page, fonts and help from inside itself."""
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120, check=True).stdout
    if version not in out:
        sys.exit(f"--version printed {out!r}, expected {version}")
    subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=120, check=True)
    subprocess.run([exe, "endpoints"], capture_output=True, text=True, timeout=120, check=True)
    port = 18790
    env = dict(os.environ, ABX_OUT_ROOT=os.path.join(ROOT, "build", "exports"))
    for k in ("ABSTRACT_API_KEY", "ABSTRACT_API_KEY_COMMAND", "ABSTRACT_API_KEY_FILE", "ABSTRACT_VENDOR_ACCOUNT_ID"):
        env.pop(k, None)
    app = subprocess.Popen([exe, "--web", "--port", str(port), "--no-browser"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
    try:
        last = None
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                    health = json.loads(r.read())
                break
            except OSError as ex:
                last = ex
                if app.poll() is not None:
                    sys.exit(f"the browser app exited with code {app.returncode} before it answered")
                time.sleep(0.5)
        else:
            sys.exit(f"the browser app did not answer within a minute ({last})")
        if health != {"ok": True, "version": version}:
            sys.exit(f"/healthz answered {health!r}")
        for path in ("/", "/static/app.js", "/static/app.css", "/static/fonts/Barlow-Regular.woff2"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                if r.status != 200 or not r.read(64):
                    sys.exit(f"{path} was not served")
    finally:
        app.terminate()
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
    print(f"proved: version {version}, help, endpoints, and the browser app's page, script, style and font")


if __name__ == "__main__":
    main()
