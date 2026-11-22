# Claude environments

A Claude Code session that opens this repository gets the tool ready to use: the MCP server `abstract-search` (from `.mcp.json`), the command line, a skill that tells Claude how to use them (`.claude/skills/`), and a start-up check that says whether a tenant is connected (`.claude/settings.json` runs `scripts/session-start.sh`). Nothing is installed, because the tool uses Python's standard library only.

There are three places a session can run. They differ in where the API key lives.

| Where the session runs | Where the key lives | Browser app |
|---|---|---|
| Your own computer (Claude Code in a terminal, the desktop app or an IDE) | Your secret manager, through `ABSTRACT_API_KEY_COMMAND` | Yes |
| A cloud environment (Anthropic-hosted) | A network secret on the environment. The session never sees it. | No: use the MCP tools and the command line |
| A self-hosted environment (your own runner) | A secret file on your runner | No: use the MCP tools and the command line |

In every case, ask Claude to run `capabilities` first. It reads only, names the tenant, and says what is missing if the connection is not complete.

## On your own computer

```
git clone https://github.com/IamABS3C/abstract-search-export.git
cd abstract-search-export
export ABSTRACT_VENDOR_ACCOUNT_ID=your-vendor-account-id
export ABSTRACT_EXPECT_TENANT=your-vendor-account-id
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
ABSTRACT_EXPECT_TENANT=your-vendor-account-id
ABSTRACT_API_KEY_FROM_NETWORK=1
```

5. Choose **Add environment**, select this repository, and start a session. The first lines of the session say the tool is there and that credentials are set, with "key: added by the network".
6. Ask Claude: "Run the capabilities check and tell me which tenant this is." A 401 answer means the secret did not reach the request: check the host, header name and prefix in step 3.

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

If a cloud or self-hosted session behaves differently from this page, open an issue with what the capabilities check printed.
