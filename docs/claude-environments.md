# Claude Code, Claude Desktop and Claude environments

Every route below asks for the same two things, the **vendor account ID** and the **API key** (or a command that reads the key from 1Password, a keychain or a vault), and every one names the tenant it is connected to: ask Claude to run `status`.

| Route | You do | Where the key lives |
|---|---|---|
| [The Claude Code plugin](#the-claude-code-plugin) | One install command, then answer two questions | Claude Code's secure credential store, or your secret manager through a command |
| [Claude Desktop](#claude-desktop) | Download one file and double-click it | The system keychain, or your secret manager through a command |
| [This repository in a session](#on-your-own-computer) | Clone it and start Claude Code in it | Your secret manager, through `ABSTRACT_API_KEY_COMMAND` |
| [A cloud environment](#a-cloud-environment) | Create an environment with a network secret | A network secret. The session never sees it. |
| [A self-hosted environment](#a-self-hosted-environment) | Run the runner image | A secret file on your runner |

## The Claude Code plugin

The plugin bundles the MCP server, the skill that tells Claude how to use the tool, and an export agent that runs a whole export or content sync and reports the proof. It needs Python 3.8 or newer on the computer and nothing else.

In Claude Code:

```
/plugin install abstract-search-export --marketplace IamABS3C/abstract-search-export
```

Or in two steps, on any version that has plugins:

```
/plugin marketplace add IamABS3C/abstract-search-export
/plugin install abstract-search-export@abstract-search-export
```

Claude Code then asks for:

| Setting | What to enter |
|---|---|
| Vendor account ID | Your vendor account ID. Required. |
| API key | The key. It is kept in Claude Code's secure credential store, never in a settings file. Leave it empty to use the next setting instead. |
| Or: a command that prints the key | For example `op read "op://Security/Abstract API/credential"` (1Password) or `security find-generic-password -s abstract-api -a api-key -w` (macOS Keychain). |
| API address | Leave it as it is unless Abstract gave you another address. |
| Allow changes to saved views and fieldsets | Off: read-only, changes are planned and shown but never made. On: a change you approve is made. |

Change them later with `/plugin configure abstract-search-export@abstract-search-export`. From a shell, `claude plugin install abstract-search-export@abstract-search-export --config vendor_account_id=AbC123xYz0 --config 'api_key_command=op read "op://Security/Abstract API/credential"'` does the same without questions.

Then ask Claude: "Run status and tell me which tenant this is."

## Claude Desktop

Each release carries `abstract-search-export-<version>.mcpb`, a Claude Desktop extension. Download it from the [latest release](https://github.com/IamABS3C/abstract-search-export/releases/latest) and double-click it, or drag it onto Claude Desktop. Claude Desktop asks for the same settings as the plugin; the key goes into the system keychain. It runs with the computer's Python 3.8 or newer (`python3` on macOS and Linux, the `py` launcher on Windows).

To build the extension yourself: `python3 scripts/build-mcpb.py`, which writes `dist/abstract-search-export-<version>.mcpb` after checking that the bundled server answers.

## Opening this repository in a session


A Claude Code session that opens this repository, without the plugin, gets the tool ready to use: the MCP server `abstract-search` (from `.mcp.json`), the command line, a skill that tells Claude how to use them (`.claude/skills/`), and a start-up check that says whether a tenant is connected (`.claude/settings.json` runs `scripts/session-start.sh`). Nothing is installed, because the tool uses Python's standard library only.

Such a session can run in three places. They differ in where the API key lives.

| Where the session runs | Where the key lives | Browser app |
|---|---|---|
| Your own computer (Claude Code in a terminal, the desktop app or an IDE) | Your secret manager, through `ABSTRACT_API_KEY_COMMAND` | Yes |
| A cloud environment (Anthropic-hosted) | A network secret on the environment. The session never sees it. | No: use the MCP tools and the command line |
| A self-hosted environment (your own runner) | A secret file on your runner | No: use the MCP tools and the command line |

In every case, ask Claude to run `status` first. It reads only, names the tenant and where the key comes from, and says what is missing if the connection is not complete.

## On your own computer

```
git clone https://github.com/IamABS3C/abstract-search-export.git
cd abstract-search-export
export ABSTRACT_VENDOR_ACCOUNT_ID=your-vendor-account-id
export ABSTRACT_API_KEY_COMMAND='op read "op://Security/Abstract API/credential"'
claude
```

Claude Code asks once whether to use the project's MCP server. The server is read-only. To let it change saved views and fieldsets (each change still needs a reviewed plan and a yes), add `"--allow-writes"` after `"mcp"` in `.mcp.json`.

## A cloud environment

A cloud session runs in a virtual machine that Anthropic hosts. Environment variables there can be read by anyone who uses the environment, so the key does not go in one. It goes in a **network secret**: Anthropic's proxy adds it to requests for the Abstract API after they leave the machine, and the tool is told to send no key of its own.

1. At [claude.ai/code](https://claude.ai/code), open the environment selector under the message box and choose **New environment**. Name it, for example, "Abstract search".
2. **Network access:** keep **Limited**. The Abstract API is reached through the network secret, so it needs no entry in **Allowed domains**.
3. **Network secrets → Add secret.** Keep the secret type **Bearer** and fill in:

| Field | Value |
|---|---|
| Allowed websites | `api.abstractsecurity.app` |
| Header name | `Authorization` |
| Prefix | `Bearer` |
| Value | Your API key |

   On a Team or Enterprise plan without an organization admin role, the form is shorter: **Host** `api.abstractsecurity.app`, **Secret type** Token, **Header** `Authorization`, and the key as the token.

4. **Environment variables.** These are not secrets:

```
ABSTRACT_VENDOR_ACCOUNT_ID=your-vendor-account-id
ABSTRACT_API_KEY_FROM_NETWORK=1
```

5. Choose **Add environment**, select this repository, and start a session. The first lines of the session say the tool is there and that credentials are set, with "key: added by the network".
6. Ask Claude: "Run status and tell me which tenant this is." A 401 answer means the secret did not reach the request: check the host, header name and prefix in step 3.

What to know about cloud sessions:

- **One repository per session.** The repository's start-up check and MCP server load in a session with this repository alone, outside a project's threads.
- **No browser app.** The session's machine has no port you can open, so Claude uses the MCP tools and the command line. Both do everything the browser app does.
- **Exports stay in the session's machine.** Ask for a summary, a count or a small result table in the conversation. For a large export, run the tool where the data should land: your own computer, a server or a scheduled job.
- **The network secret is for an Anthropic-hosted environment.** It needs the Abstract API to be reachable from the internet, which it is.

## A self-hosted environment

A self-hosted environment runs sessions on a machine you operate, through Anthropic's runner. It is for Team and Enterprise plans and is switched on by an Owner. Network secrets do not exist there, so the key is a file on your runner that only the runner can read.

`deploy/claude-self-hosted/` has a runner image and a Compose file that follow Anthropic's deployment guide, with the tool's needs added: Python is already in the image, and the key is mounted as a secret file.

1. An Owner allows self-hosted environments and creates one at **claude.ai → Admin settings → Cloud environments → Self-hosted environments → New**, then copies the environment secret. It is shown once.
2. On the runner machine, in `deploy/claude-self-hosted/`:

```
(umask 077 && cat > environment-secret)        # paste the environment secret, then Ctrl-D
(umask 077 && cat > abstract-api-key)          # paste the Abstract API key, then Ctrl-D
export ABSTRACT_VENDOR_ACCOUNT_ID=your-vendor-account-id
docker compose up -d --build
```

3. Start a session in that environment on this repository and ask Claude to run the capabilities check.

The Compose file runs one session per container (`--capacity 1`), which is what Anthropic's guide asks for before connecting production systems. Read their guide for network rules, upgrades and isolation: [Self-hosted environments](https://code.claude.com/docs/en/self-hosted-environments) and [Deploy](https://code.claude.com/docs/en/self-hosted-environments-deploy).

## What Claude is told

`CLAUDE.md` and the skill hold the rules a session follows when it uses the tool for you: say which tenant it is connected to, never show the key, show a query and how it will run before running it, treat plain-English queries as drafts, change saved views and fieldsets only after you say yes to a reviewed plan, and never report "no events" without the window and filter it searched.

## How this was checked

| Part | Checked by |
|---|---|
| The session start-up script, the MCP server and the command line | Run on a computer and in CI on every push. |
| The mode where the network adds the key | Unit tests: the tool sends no `Authorization` header, and a 401 names the secret. It follows Anthropic's documented behaviour for network secrets (read 2026-11-21). It has not yet been run in a live cloud session. |
| The self-hosted runner files | Built from Anthropic's published recipe (read 2026-11-21). |
| The Claude Code plugin | `claude plugin validate` passes. Installed from this repository's marketplace on GitHub into a separate Claude Code configuration, with its settings given as `--config`: the tool is copied into the plugin cache, the skill and the agent load, the MCP server connects, and its `status` tool named the test tenant with the API key read through a key command and an unfilled key setting ignored. |
| The Claude Desktop extension | The manifest passes the official `mcpb validate`, and the build checks that the bundled server answers. It has not yet been installed in Claude Desktop by a person. |

If a session behaves differently from this page, open an issue with what `status` printed.
