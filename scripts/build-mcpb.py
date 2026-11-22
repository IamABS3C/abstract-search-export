#!/usr/bin/env python3
"""Build the desktop extension (.mcpb): the MCP server in one file that Claude Desktop installs with a double-click
and asks for the vendor account ID and the API key (kept in the system keychain) on install.

    python3 scripts/build-mcpb.py          # writes dist/abstract-search-export-<version>.mcpb
    npx -y @anthropic-ai/mcpb validate build/mcpb/manifest.json     # optional: the official schema check

The bundle holds search-export/'s program files and a manifest. It runs with the computer's Python 3.8 or newer
(python3 on macOS and Linux, the py launcher on Windows); the tool uses nothing beyond Python's standard library.
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "search-export")
REPO = "https://github.com/IamABS3C/abstract-search-export"
FILES = ["VERSION", "LICENSE"]
FOLDERS = ["docs", "examples"]                       # the in-tool help and the example saved searches
SKIP = {os.path.join("docs", "screenshots")}


def manifest(version):
    args = ["${__dirname}/server/abstract_search.py", "mcp"]
    return {
        "$schema": "https://raw.githubusercontent.com/anthropics/mcpb/main/schemas/mcpb-manifest-v0.4.schema.json",
        "manifest_version": "0.4",
        "name": "abstract-search-export",
        "display_name": "Abstract Search & Export",
        "version": version,
        "description": "Search, check and export events from an Abstract Security tenant, and keep its saved views "
                       "and fieldsets as code.",
        "long_description": "Counts, previews and validates searches, checks KQL, drafts KQL from plain English, "
                            "exports every matching event with proof that the export is complete, and reads, "
                            "builds and copies saved views and fieldsets. Read-only unless you turn on changes; "
                            "every change is planned first and needs your yes.",
        "author": {"name": "The Abstract Search & Export authors", "url": REPO},
        "repository": {"type": "git", "url": REPO + ".git"},
        "homepage": REPO,
        "documentation": REPO + "/blob/main/search-export/docs/MCP.md",
        "support": REPO + "/issues",
        "license": "MIT",
        "keywords": ["abstract", "security", "siem", "search", "export", "kql"],
        "server": {
            "type": "python",
            "entry_point": "server/abstract_search.py",
            "mcp_config": {
                "command": "python3",
                "args": args,
                "env": {
                    "ABSTRACT_VENDOR_ACCOUNT_ID": "${user_config.vendor_account_id}",
                    "ABSTRACT_API_KEY": "${user_config.api_key}",
                    "ABSTRACT_API_KEY_COMMAND": "${user_config.api_key_command}",
                    "ABSTRACT_BASE_URL": "${user_config.base_url}",
                    "ABSTRACT_MCP_ALLOW_WRITES": "${user_config.allow_writes}",
                },
                "platform_overrides": {"win32": {"command": "py", "args": ["-3"] + args}},
            },
        },
        "tools_generated": True,
        "user_config": {
            "vendor_account_id": {
                "type": "string", "title": "Vendor account ID", "required": True,
                "description": "Your Abstract vendor account ID. The tool checks that the key belongs to it and names "
                               "the tenant in every answer.",
            },
            "api_key": {
                "type": "string", "title": "API key", "sensitive": True, "required": False,
                "description": "An Abstract API key. Kept in the system keychain. Leave it empty to use the command "
                               "below instead.",
            },
            "api_key_command": {
                "type": "string", "title": "Or: a command that prints the key", "required": False,
                "description": "Reads the key from a secret manager when it is needed, for example "
                               "op read \"op://Security/Abstract API/credential\" (1Password). Leave it empty when "
                               "you entered the key above.",
            },
            "base_url": {
                "type": "string", "title": "API address", "required": False,
                "default": "https://api.abstractsecurity.app",
                "description": "Leave as it is unless Abstract gave you another address.",
            },
            "allow_writes": {
                "type": "boolean", "title": "Allow changes to saved views and fieldsets", "required": False,
                "default": False,
                "description": "Off: read-only; changes are planned and shown but never made. On: a change you "
                               "approve is made.",
            },
        },
        "compatibility": {"platforms": ["darwin", "win32", "linux"], "runtimes": {"python": ">=3.8"}},
    }


def main():
    version = open(os.path.join(KIT, "VERSION"), encoding="utf-8").read().strip()
    work = os.path.join(ROOT, "build", "mcpb")
    shutil.rmtree(work, ignore_errors=True)
    server = os.path.join(work, "server")
    os.makedirs(server)
    for fn in sorted(os.listdir(KIT)):
        if fn.endswith(".py") or fn in FILES:
            shutil.copy2(os.path.join(KIT, fn), server)
    for d in FOLDERS:
        for dp, dns, fns in os.walk(os.path.join(KIT, d)):
            rel = os.path.relpath(dp, KIT)
            if rel in SKIP:
                dns[:] = []
                continue
            dns[:] = [x for x in dns if x != "__pycache__"]
            os.makedirs(os.path.join(server, rel), exist_ok=True)
            for f in fns:
                shutil.copy2(os.path.join(dp, f), os.path.join(server, rel, f))
    with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest(version), fh, indent=2)
        fh.write("\n")
    # prove the bundled server answers an MCP initialize before packing it
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "build"}}})
    env = {k: v for k, v in os.environ.items() if not k.startswith("ABSTRACT_")}
    p = subprocess.run([sys.executable, os.path.join(server, "abstract_search.py"), "mcp"], input=msg + "\n",
                       capture_output=True, text=True, timeout=60, env=env)
    if '"serverInfo"' not in p.stdout:
        sys.exit(f"the bundled MCP server did not answer initialize:\n{p.stdout}\n{p.stderr}")
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", f"abstract-search-export-{version}.mcpb")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, _, fns in os.walk(work):
            for f in sorted(fns):
                full = os.path.join(dp, f)
                z.write(full, os.path.relpath(full, work).replace(os.sep, "/"))
    print(f"wrote {out} ({os.path.getsize(out) // 1024} KB); the bundled server answered initialize")


if __name__ == "__main__":
    main()
