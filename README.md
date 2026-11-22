# Abstract Search & Export

Find the events you want in your Abstract Security tenant and export every one of them, with proof that the export is complete. It is a companion to Abstract for jobs that are too large, too repetitive or too scripted for the search screen: long ranges that reach into the archive, exports that run on a schedule, and searches driven by a script or an AI assistant.

It is one engine with four ways in: a browser app, a guided terminal, a command line and an MCP server. It uses Python's standard library and nothing else, so there is nothing to install beyond the tool itself. Every way in needs the same two things from you: your **vendor account ID** and an **API key**, typed into the app or read from 1Password, a keychain, a secret file or the environment. Every way in shows the tenant it is connected to.

![The browser app: a search, previewed](search-export/docs/screenshots/web-03-preview.png)

## Start it

Pick the one that fits. Every route ends at the same app.

| You have | Do this | Details |
|---|---|---|
| **Docker** | `docker run --rm -p 127.0.0.1:8765:8765 -v "$HOME/AbstractExports:/exports" iamabs3c/abstract-search-export` and open the link it prints | [Docker](#docker) |
| **Nothing installed** | Download the single file for your system from [Releases](../../releases/latest) and run it | [A single file](#a-single-file) |
| **Python 3.8 or newer** | `python3 search-export/abstract_search.py --web` | [Python](#python) |
| **A browser and a GitHub account** | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/IamABS3C/abstract-search-export?quickstart=1) | [Codespaces and DevPod](docs/codespaces-and-devpod.md) |
| **DevPod** | [![Open in DevPod](https://devpod.sh/assets/open-in-devpod.svg)](https://devpod.sh/open#https://github.com/IamABS3C/abstract-search-export) | [Codespaces and DevPod](docs/codespaces-and-devpod.md) |
| **Claude Code** | `/plugin install abstract-search-export --marketplace IamABS3C/abstract-search-export`, then enter the vendor account ID and the key (or a 1Password or keychain command) when it asks. Or open this repository in a session | [Claude Code and Claude Desktop](docs/claude-environments.md) |
| **Claude Desktop** | Download `abstract-search-export-<version>.mcpb` from [Releases](../../releases/latest) and double-click it; it asks for the vendor account ID and the key | [Claude Code and Claude Desktop](docs/claude-environments.md#claude-desktop) |
| **Another AI assistant** | Point it at the MCP server: `python3 search-export/abstract_search.py mcp` or the image with `mcp` | [MCP](search-export/docs/MCP.md) |

## Connect your tenant

You need two things from Abstract: your **vendor account ID** and an **API key**.

1. Start the app by any route above and open the link it prints.
2. In **Connect to your tenant**, enter the vendor account ID and the API key, and choose **Connect**.
3. The app's header shows the tenant's name and vendor account ID from then on. Check it is the one you meant.

The key is held in the app's memory while it runs. It is never written to disk, to a log, to an export or to the browser's storage, and it is never sent back to the page.

For the command line, scheduled jobs and assistants, give the credentials through the environment instead:

| Variable | Value |
|---|---|
| `ABSTRACT_VENDOR_ACCOUNT_ID` | Your vendor account ID. |
| `ABSTRACT_API_KEY_COMMAND` | A command that prints the key when it is needed. Recommended. 1Password: `op read "op://Security/Abstract API/credential"`. macOS Keychain: `security find-generic-password -s abstract-api -a api-key -w`. Windows: `pwsh -NoProfile -Command "Get-Secret -Name AbstractApiKey -AsPlainText"`. |
| `ABSTRACT_API_KEY_FILE` | Or a file that holds the key on one line, where secrets are mounted as files. |
| `ABSTRACT_API_KEY` | Or the key itself, where a scheduler or CI system injects secrets. |
| `ABSTRACT_EXPECT_TENANT` | Optional. Pins one tenant, for scheduled jobs: every command refuses to run when the key belongs to another. |

The key is always checked against the vendor account ID before anything else runs. Then check the connection. This reads only, names the tenant and says where the key came from, never the key:

```
python3 search-export/abstract_search.py status
```

Every command that connects also prints `Connected to <tenant> (<vendor account ID>) · production` first, on standard error, so `--json` output stays clean.

## Docker

The image holds Python and the tool. It runs as an unprivileged user and downloads nothing when it starts.

```
docker run --rm -p 127.0.0.1:8765:8765 -v "$HOME/AbstractExports:/exports" iamabs3c/abstract-search-export
```

- Open the link in the output. In Docker Desktop, it is in the container's **Logs** tab.
- Exports are written to `AbstractExports` in your home folder.
- The port is published to this computer only. Keep it that way.

Or let the start script do it. It starts the app in the background, prints the link and opens your browser:

```
curl -fsSLO https://raw.githubusercontent.com/IamABS3C/abstract-search-export/main/search-export/run-docker.sh
sh run-docker.sh            # start        sh run-docker.sh link    a new link
sh run-docker.sh stop       # stop         sh run-docker.sh terminal    the guided terminal
```

On Windows, in PowerShell:

```
irm https://raw.githubusercontent.com/IamABS3C/abstract-search-export/main/search-export/run-docker.ps1 -OutFile run-docker.ps1
powershell -ExecutionPolicy Bypass -File .\run-docker.ps1
```

From a checkout of this repository, `./run.sh` (or `.\run.ps1`) builds the image from the source here instead of pulling it.

The same image runs single commands and the MCP server:

```
docker run --rm -e ABSTRACT_VENDOR_ACCOUNT_ID -e ABSTRACT_API_KEY \
  -v "$HOME/AbstractExports:/exports" iamabs3c/abstract-search-export export --dataset "Okta" --last 24h --merge
```

Compose, another port, secret files, scheduled exports and how the container is locked down: [Run it in Docker](search-export/docs/DOCKER.md).

| Image | Where |
|---|---|
| `iamabs3c/abstract-search-export` | [Docker Hub](https://hub.docker.com/r/iamabs3c/abstract-search-export). |
| `ghcr.io/iamabs3c/abstract-search-export` | GitHub's container registry. The same image. |

Both carry the version (`0.6.0`), the minor line (`0.6`) and `latest`, for Intel and Arm.

## A single file

Each [release](../../releases/latest) carries one file per system. It needs no Python and no Docker.

| System | File |
|---|---|
| Windows (Intel) | `abstract-search-<version>-windows-x64.exe` |
| macOS (Apple silicon) | `abstract-search-<version>-macos-arm64` |
| Linux (Intel) | `abstract-search-<version>-linux-x64` |
| Linux (Arm) | `abstract-search-<version>-linux-arm64` |

Run it with no arguments for the guided terminal, or with `--web` for the browser app. Checking the download, the first-run prompts on macOS and Windows, and building it yourself: [The single-file program](docs/binaries.md).

## Python

Python 3.8 or newer, on macOS, Linux or Windows. Nothing to install.

```
git clone https://github.com/IamABS3C/abstract-search-export.git
cd abstract-search-export
python3 search-export/abstract_search.py --web      # the browser app
python3 search-export/abstract_search.py            # the guided terminal
python3 search-export/abstract_search.py --help     # the command line
```

On Windows, use `py` in place of `python3`.

## What it does

Every item below is the same step in the browser app, the guided terminal, the command line and the MCP server.

| | Commands | What you get |
|---|---|---|
| **Connect and discover** | `status`, `capabilities`, `datasets`, `fields`, `views`, `endpoints` | Which tenant you are on and where the key comes from; what the tenant supports (KQL, the archive, complete events); every dataset with its volume; the fields a dataset really fills; every saved view and how it runs here; every API endpoint the tool calls. |
| **Search and validate** | `preview`, `validate`, `kql check`, `kql preview`, `kql save`, `nl` | Filters with every operator Abstract offers, KQL, or plain English turned into KQL (shown first, never run unless you ask). Every search is checked against the tenant before it runs, and a zero is checked again before it is reported. |
| **Export with proof** | `export`, `exports` | Every matching event, recent data and the archive, in slices that resume. Each slice is reconciled against Abstract's own count, every row re-checked against the filter, and the result says COMPLETE, PARTIAL or INCOMPLETE. |
| **Sync views, fieldsets and searches** | `content export`, `content diff`, `content apply`, `fieldsets`, `views save`, `views --save`, `searches` | Saved views and fieldsets as files for git, backups and other tenants, compared with a tenant and applied after one review. Fieldsets built from the fields a dataset fills. Saved views copied into saved searches on this computer. |
| **Schedule and monitor** | `automate`, `ABX_ALERT_WEBHOOK`, exit codes | Ready jobs for cron, launchd, systemd, Windows Task Scheduler, GitHub Actions or a container platform. Each run exports the last whole interval, resumes a stopped window, and alerts on failure, a partial result or an empty one. |
| **API, scripts and assistants** | `--json`, `--show-api`, `mcp` | Machine-readable output on every command, every API call shown as it happens with the key masked, and an MCP server with 27 tools, read-only unless you allow changes. |

The full guide, with every screen: [search-export/README.md](search-export/README.md). In the app, choose **Help**.

## Security in one page

- The key is held in memory only and sent only to the Abstract API, over HTTPS. The tool never follows a redirect.
- The browser app listens on this computer only, needs a one-time link to sign in, and loads nothing from the internet.
- The only things it changes in a tenant are saved views and fieldsets, each after a review you confirm.
- Exports are real data. They stay in your exports folder until you delete them.
- Release files and images carry a build record you can check: `gh attestation verify <file> --repo IamABS3C/abstract-search-export`.

Everything it calls, stores and sends: [Security](search-export/docs/SECURITY.md).

## Licence

MIT. See [LICENSE](LICENSE).

## In this repository

| Path | What it is |
|---|---|
| `search-export/` | The tool: the program, its tests, its docs and the container build. |
| `run.sh`, `run.ps1` | Start it in Docker from a checkout. |
| `.devcontainer/` | The dev container for Codespaces, DevPod and VS Code. |
| `.mcp.json`, `.vscode/mcp.json` | The MCP server, set up for Claude Code and for VS Code. |
| `.claude/`, `CLAUDE.md` | What a Claude Code session needs to use the tool well. |
| `.claude-plugin/`, `agents/` | The Claude Code plugin and its marketplace: the MCP server, the skill and an export agent, with the vendor account ID and the key as install settings. |
| `deploy/claude-self-hosted/` | A runner image for a self-hosted Claude environment. |
| `scripts/` | The session and welcome scripts, the single-file build and the Claude Desktop extension build. |
| `.github/workflows/` | Tests on every push; images, files and a release on every version tag. |

Tests need no network and no key: `cd search-export/tests && python3 -m unittest discover -q`.
