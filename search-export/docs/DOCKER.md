# Run it in Docker

Docker runs the whole tool in a container, so the computer needs Docker and nothing else: no Python, nothing installed. The browser app, the guided terminal, the command line and the MCP server are all in the one image.

The files named here are in the program folder: `app` in the download, `search-export` in the repository.

## Start the browser app

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it. On Linux, Docker Engine works the same way.
2. Open a terminal in the program folder and run the start script.

| System | Command |
|---|---|
| macOS, Linux | `./run-docker.sh` |
| Windows (PowerShell) | `.\run-docker.ps1` |

The first run builds the image from the folder, which takes about a minute. Then the script prints a link and opens it in your browser:

```
  Abstract Search & Export is running. Open this link in your browser:

    http://127.0.0.1:8765/?code=…

  The link works once. For another one: ./run-docker.sh link      To stop the app: ./run-docker.sh stop
  Exports are written to: /home/you/AbstractExports
```

3. In the page, enter your vendor account ID and API key and choose **Connect**. The key stays in the app's memory. It is not written to disk, and it is gone when the container stops.

The link works once. When you close the page, run `./run-docker.sh link` for a new one, or open `http://127.0.0.1:8765/` and read the new link in the app's log (Docker Desktop: the container's **Logs** tab).

| To | macOS, Linux | Windows |
|---|---|---|
| Get a new link | `./run-docker.sh link` | `.\run-docker.ps1 link` |
| Follow the log | `./run-docker.sh logs` | `.\run-docker.ps1 logs` |
| Stop the app | `./run-docker.sh stop` | `.\run-docker.ps1 stop` |
| Use the guided terminal | `./run-docker.sh terminal` | `.\run-docker.ps1 terminal` |
| Run one command | `./run-docker.sh datasets` | `.\run-docker.ps1 datasets` |

Stopping the app while an export runs does not lose it. Start the app again and choose **Resume** in **Export history**.

## Where your exports go

Exports, saved searches and logs are written to one folder on your computer, `AbstractExports` in your home folder, which the container sees as `/exports`. Nothing else in the container can be written to. To use another folder, set `ABX_EXPORTS` before you start:

```
ABX_EXPORTS=/data/abstract ./run-docker.sh
```

On Linux the container runs as your own user, so the exported files belong to you.

## Connect without typing in the page

Typing the key in the page is the safest way: it exists only in the app's memory. For a server or a scheduled job, there are two other ways.

**A secret file (recommended).** Put the key in a file only you can read, mount it read-only, and name it with `ABSTRACT_API_KEY_FILE`. The key is never part of the container's settings:

```
docker run -d --name abstract-search -p 127.0.0.1:8765:8765 \
  -v "$HOME/AbstractExports:/exports" \
  -v "$HOME/.abstract/api-key:/run/secrets/abstract_api_key:ro" \
  -e ABSTRACT_API_KEY_FILE=/run/secrets/abstract_api_key \
  -e ABSTRACT_VENDOR_ACCOUNT_ID=your-vendor-account-id \
  -e ABSTRACT_EXPECT_TENANT=your-vendor-account-id \
  abstract-search-export
```

Docker secrets and Kubernetes secrets mount a file the same way.

**Environment variables.** Set your credentials in the terminal before you start, and the script passes them to the container. They are not written into the image or into any file in the kit. Docker itself keeps a container's environment in that container's settings, where `docker inspect` shows it to anyone who can run Docker on this computer, until the container is removed. `./run-docker.sh stop` removes it.

```
export ABSTRACT_VENDOR_ACCOUNT_ID=your-vendor-account-id
export ABSTRACT_EXPECT_TENANT=your-vendor-account-id
export ABSTRACT_API_KEY_COMMAND='op read op://vault/abstract/api-key'
export ABSTRACT_API_KEY="$($ABSTRACT_API_KEY_COMMAND)"
./run-docker.sh
```

`ABSTRACT_EXPECT_TENANT` is the tenant guard: every command refuses to run when the key belongs to any other tenant.

## Without the script

The script only runs Docker for you. These are the same steps by hand.

```
docker build -t abstract-search-export .
docker run -d --name abstract-search \
  -p 127.0.0.1:8765:8765 \
  -v "$HOME/AbstractExports:/exports" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  abstract-search-export
docker logs abstract-search
```

Open the link that `docker logs` shows. In Docker Desktop, you can also run the image from the **Images** list: set the host port to `8765` and bind your exports folder to `/exports`.

With Docker Compose, from the program folder:

```
docker compose up -d
docker compose logs app
docker compose down
```

The image is also published, so you can run it without this folder. It is the same image in two registries, for Intel and Arm, tagged with the version (for example `0.5.0`), the minor line (`0.5`) and `latest`:

| Registry | Image |
|---|---|
| Docker Hub | `iamabs3c/abstract-search-export` |
| GitHub's container registry | `ghcr.io/iamabs3c/abstract-search-export` |

```
docker run --rm -p 127.0.0.1:8765:8765 -v "$HOME/AbstractExports:/exports" iamabs3c/abstract-search-export
```

Set `ABX_IMAGE` to either name and the start script and the Compose file use it without building. The start script on its own, outside the program folder, pulls `ghcr.io/iamabs3c/abstract-search-export`; set `ABX_PUBLISHED_IMAGE=iamabs3c/abstract-search-export` to pull from Docker Hub instead.

## Another port

Set `ABX_PORT` when 8765 is taken:

```
ABX_PORT=9000 ./run-docker.sh
```

By hand, publish the port and tell the app the address you will open, so its links and its checks use it:

```
docker run -d --name abstract-search -p 127.0.0.1:9000:8765 -e ABX_WEB_URL=http://127.0.0.1:9000/ \
  -v "$HOME/AbstractExports:/exports" abstract-search-export
```

## The command line and scheduled exports

Anything after the image name is a command, the same ones as the command line:

```
docker run --rm -v "$HOME/AbstractExports:/exports" \
  -e ABSTRACT_VENDOR_ACCOUNT_ID -e ABSTRACT_EXPECT_TENANT -e ABSTRACT_API_KEY \
  abstract-search-export export --dataset "Okta" --last 24h --merge
```

The exit code is the command's own (0 complete, 3 complete with a warning, anything else a failure), so a scheduler or a CI job can act on it. `automate --target container-cron` writes a ready scheduled job for a container platform.

## An AI assistant (MCP)

The MCP server runs on standard input and output, so an assistant can start the container itself. In the assistant's MCP settings:

```json
{
  "mcpServers": {
    "abstract-search": {
      "command": "docker",
      "args": ["run", "--rm", "-i",
               "-e", "ABSTRACT_VENDOR_ACCOUNT_ID", "-e", "ABSTRACT_EXPECT_TENANT", "-e", "ABSTRACT_API_KEY",
               "-v", "/home/you/AbstractExports:/exports",
               "abstract-search-export", "mcp"]
    }
  }
}
```

It reads only, unless you add `--allow-writes` after `mcp`. See MCP.md.

## How the container is locked down

- **This computer only.** The port is published to `127.0.0.1`, so nothing else on the network can reach the app. Do not publish it on `0.0.0.0`. To use it from another computer, forward the port over SSH.
- **The same sign-in as the local app.** The page needs the one-time link from the app's log, and the app accepts requests only for the address it was started at.
- **No privileges.** The app runs as an unprivileged user, with every Linux capability dropped, a read-only file system, and one writable folder.
- **Nothing installed at run time.** The image holds Python and this tool. It downloads nothing when it starts.
- **Health.** `GET /healthz` answers `{"ok": true}` with the version and nothing else. Docker uses it to show the container as healthy.

## If something goes wrong

| What you see | What to do |
|---|---|
| "Docker is installed but not running" | Start Docker Desktop and wait until it says it is running. |
| "The app could not start. Is another program using the port?" | Set `ABX_PORT` to another number and start again. |
| The page says "This link has already been used" | Run the script with `link`, or read the new link in the container's log. |
| The page says "bad host" | You opened the app at an address it was not started with. Use the link the app printed, or set `ABX_WEB_URL` to the address you use. |
| "could not reach https://api.abstractsecurity.app" | The container has no route to the internet. Check Docker's proxy settings; `HTTPS_PROXY` is honoured when you pass it with `-e HTTPS_PROXY`. |
| Exports folder is empty on Linux | The folder was created by another user. Give it to yourself (`sudo chown -R "$USER" ~/AbstractExports`) and start again. |
