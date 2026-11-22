# Codespaces, DevPod and dev containers

The repository has a dev container: Python 3.12, nothing else installed, port 8765 forwarded for the browser app. Codespaces, DevPod and VS Code all use it.

## GitHub Codespaces

1. Add your credentials as Codespaces secrets first, so they are never typed into the workspace. In GitHub: **Settings → Codespaces → Secrets → New secret**, three times, and give each one access to this repository (or your fork of it):

| Secret | Value |
|---|---|
| `ABSTRACT_VENDOR_ACCOUNT_ID` | Your vendor account ID. |
| `ABSTRACT_EXPECT_TENANT` | The same ID. |
| `ABSTRACT_API_KEY` | Your API key. |

2. Choose **Open in GitHub Codespaces** on the repository's main page. When the codespace opens, the terminal says the tool is ready and whether the credentials are set.
3. Start it:

| You opened the codespace in | Run | Then |
|---|---|---|
| VS Code on your computer | `python3 search-export/abstract_search.py --web --port 8765 --no-browser` | Open the link it prints. VS Code forwards the port to your computer. |
| A web browser | `python3 search-export/abstract_search.py` | Use the guided terminal. It does everything the browser app does. |

The browser app accepts requests for `localhost` only, which is what VS Code's port forwarding gives you. A codespace's `*.app.github.dev` address in a web browser is refused, so use the guided terminal there.

> In a codespace, the tool and your exports run on GitHub's computers. The exports stay in GitHub's cloud until you delete them or the codespace. Download what you need, then delete the export.

## DevPod

Choose **Open in DevPod** on the repository's main page, or from a terminal:

```
devpod up github.com/IamABS3C/abstract-search-export --ide vscode
```

DevPod runs the same dev container on the provider you choose: Docker on your computer, a cloud VM or Kubernetes. Give it the credentials as workspace environment variables, or set `ABSTRACT_API_KEY_COMMAND` to a secret-manager command that works inside the workspace. Then start the tool as in the table above.

## VS Code on your own computer

Open the folder in VS Code and choose **Reopen in Container**. Connect in the browser app, or set `ABSTRACT_API_KEY_COMMAND`. Do not put a key in `devcontainer.json`, a `.env` file or the repository.

## The MCP server in VS Code

`.vscode/mcp.json` registers the tool's MCP server as `abstract-search` for GitHub Copilot's agent mode. It reads the same environment variables and is read-only. See [MCP](../search-export/docs/MCP.md).
