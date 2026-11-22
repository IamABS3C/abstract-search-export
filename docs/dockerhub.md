# Abstract Search & Export

Find the events you want in your Abstract Security tenant and export every one of them, with proof that the export is complete. One engine with four ways in: a browser app, a guided terminal, a command line and an MCP server for AI assistants.

Source, guides and release files: [github.com/IamABS3C/abstract-search-export](https://github.com/IamABS3C/abstract-search-export)

## Run the browser app

```
docker run --rm -p 127.0.0.1:8765:8765 -v "$HOME/AbstractExports:/exports" iamabs3c/abstract-search-export
```

Open the link it prints (in Docker Desktop it is in the container's **Logs** tab), then connect with your vendor account ID and API key. Exports are written to `AbstractExports` in your home folder. The port is published to this computer only; keep it that way.

## Run one command, a scheduled export or the MCP server

```
docker run --rm -e ABSTRACT_VENDOR_ACCOUNT_ID -e ABSTRACT_EXPECT_TENANT -e ABSTRACT_API_KEY \
  -v "$HOME/AbstractExports:/exports" iamabs3c/abstract-search-export export --dataset "Okta" --last 24h --merge
```

```
docker run --rm -i -e ABSTRACT_VENDOR_ACCOUNT_ID -e ABSTRACT_EXPECT_TENANT -e ABSTRACT_API_KEY iamabs3c/abstract-search-export mcp
```

A key given with `-e` shows in `docker inspect` until the container is removed. Where you can, mount it as a file and set `ABSTRACT_API_KEY_FILE` instead: [Run it in Docker](https://github.com/IamABS3C/abstract-search-export/blob/main/search-export/docs/DOCKER.md).

## Tags

| Tag | What it is |
|---|---|
| `latest` | The newest release. |
| `0.5` | The newest release in the 0.5 line. |
| `0.5.0` | That exact release. |

Every tag is built for Intel (`linux/amd64`) and Arm (`linux/arm64`). The same image is on GitHub's registry as `ghcr.io/iamabs3c/abstract-search-export`.

## How the image is built

- Python 3.13 on Alpine, with the tool and nothing else. It downloads nothing when it starts.
- It runs as an unprivileged user (uid 10001) and works with a read-only root filesystem, `--cap-drop ALL` and `no-new-privileges`.
- The browser app accepts requests for `localhost` only (plus one address you name in `ABX_WEB_URL`) and needs a one-time sign-in link.
- Each release carries a build-provenance record and an SBOM, built by GitHub Actions from a tagged commit.

Security, settings and every command: [the full guide](https://github.com/IamABS3C/abstract-search-export/blob/main/search-export/README.md).
