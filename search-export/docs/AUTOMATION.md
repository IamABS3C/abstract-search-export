# Run a search on a schedule

The tool writes the files that run one search as a scheduled export. You can run it on:

- a computer with cron, launchd (macOS) or systemd timers;
- Windows Task Scheduler;
- GitHub Actions;
- a container, with Docker or a Kubernetes CronJob.

You choose the search, how often it runs and where. The tool writes the script, the scheduler file and a README with the steps.

```
python3 abstract_search.py automate cron --from-saved "Okta failures" --every 1d --at 00:20 --out ./okta-job
python3 abstract_search.py automate github-actions --kql 'events | where data_stream.dataset =~ "okta"' --every 1h --out ./okta-job
```

The search comes from:

- `--from-saved NAME`, a saved search on this computer;
- `--dataset`/`--where`, `--kql` or `--from-view`, which connect to read it.

`--every` is 15m, 1h, 6h or 1d. `--at` is the UTC start time for a daily run. Without `--out`, the files are printed. In the guided terminal, use **Saved searches → Run it on a schedule** or the main menu's **Run a search on a schedule**. In the browser app, use **Export → Run it on a schedule**.

## One command per scheduler

```bash
python3 abstract_search.py automate cron --from-saved "Okta failures" --every 1d --at 01:30 --out ~/jobs/okta-cron
python3 abstract_search.py automate launchd --from-saved "Okta failures" --every 1d --at 01:30 --out ~/jobs/okta-launchd
python3 abstract_search.py automate systemd --from-saved "Okta failures" --every 1h --out ~/jobs/okta-systemd
python3 abstract_search.py automate windows-task --from-saved "Okta failures" --every 1d --at 01:30 --out C:\jobs\okta
python3 abstract_search.py automate github-actions --from-saved "Okta failures" --every 1h --out ./okta-actions
python3 abstract_search.py automate container-cron --from-saved "Okta failures" --every 6h --out ./okta-container
```

Each writes the files for that scheduler, a README with the exact steps, and the search itself next to them. Without `--out` the files are printed instead.

## What every recipe does

- **The key is never in a file.**
  - On a computer, the script expects `ABSTRACT_API_KEY_COMMAND`: a command that prints the key from your keychain or secret manager, run when the export starts.
  - In GitHub Actions and Kubernetes, the key comes from the platform's secret store as `ABSTRACT_API_KEY`.
  - If neither is set, the script stops with exit code 2.
- **The tenant guard is set.** `ABSTRACT_EXPECT_TENANT` is your vendor account ID, so a key for another tenant can never export.
- **Windows follow each other exactly.**
  - Each run exports the last whole interval that ended 10 minutes before it started. A daily run at 00:20 UTC exports the previous UTC day, and an hourly run exports the previous whole hour.
  - Back-to-back runs therefore cover time with no gaps and no overlap.
  - Each window has its own folder under `ABX_OUT_ROOT/<job>/`. A run that starts again for the same window resumes that folder instead of starting over.
  - Events that arrive more than 10 minutes late are not in that window's export. If your sources are slower, run later.
- **The search is stored as files next to the script, never as quoted arguments:**
  - `search-condition.json` for a search with filters;
  - `search.kql` for KQL;
  - `search.json`, a readable copy of the whole search.
- **Exit codes and alerts:**
  - 0 is success.
  - 3 is **Partial: recent data only**: older data was not searched, and the log names the range. The job still ends as a success, but it alerts (set `ABX_ALERT_ON_WARNING=0` to stop that).
  - A run that exported nothing ends with 0, and it alerts too (set `ABX_ALERT_ON_EMPTY=0` to stop that).
  - Anything else is a failure. The script exits with that code and alerts.
  - Alerts go nowhere until `ABX_ALERT_WEBHOOK` is set. Then each one is a JSON message posted to that URL: the job, the tenant ID, the window, the exit code, the log's path and the reason (never your events). A chat incoming webhook, an incident tool or your own endpoint all work.
- **Logs.** One log per run in `ABX_OUT_ROOT/automation-logs`. Logs older than 30 days are deleted.

Where your data goes: cron, launchd, systemd and Windows Task Scheduler run the export on your own machine. GitHub Actions and container platforms run it on that service's computers, so your events pass through the service and are stored there until you delete them. Use them only if your organisation allows it.

## cron (Linux and Unix)

Files: `run-export.sh`, `crontab.txt`, the search files.

1. Store the key in your keyring once: `secret-tool store --label='Abstract API key' service abstract-search-export account <vendor account id>`.
2. Copy the folder to where it will live, for example `/opt/abstract-automation/<job>`.
3. Run `crontab -e` and add the line from `crontab.txt`. cron uses the computer's local time to start the job; the export window is always computed in UTC.
4. Test it once by hand with the command the README shows.

## launchd (macOS)

Files: `run-export.sh`, `com.abstract.search-export.<job>.plist`, the search files.

1. Store the key in your keychain once: `security add-generic-password -s abstract-search-export -a <vendor account id> -w`.
2. Copy the folder to where it will live, and the `.plist` to `~/Library/LaunchAgents/`.
3. Load it: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.
4. Run it once now: `launchctl kickstart gui/$(id -u)/<label>`.

The first time the job reads the key, macOS may ask whether to allow it: choose Always Allow.

## systemd timers (Linux)

Files: `run-export.sh`, `abstract-export-<job>.service`, `abstract-export-<job>.timer`, the search files.

1. Store the key with `secret-tool` as for cron.
2. Copy the `.service` and `.timer` files to `~/.config/systemd/user/`.
3. `systemctl --user daemon-reload && systemctl --user enable --now abstract-export-<job>.timer`.
4. So it runs while you are logged out: `loginctl enable-linger $USER`.
5. Run it once: `systemctl --user start abstract-export-<job>.service`, and read the result with `journalctl --user -u abstract-export-<job>`.

The timer is `Persistent`, so a run missed while the computer was off happens when it starts again.

## Windows Task Scheduler

Files: `run-export.ps1`, `register-task.ps1`, the search files. Both scripts work in Windows PowerShell 5.1 and PowerShell 7.

1. Store the key in a secret vault. Install the SecretManagement and SecretStore modules, register a vault and add the secret, as the README shows.
2. Set `ABSTRACT_API_KEY_COMMAND` for your user to the `Get-Secret` command the README shows.
3. Copy the folder to where it will live, for example `C:\abstract-automation\<job>`.
4. Run `register-task.ps1` once, as the user the task runs as.
5. To run while signed out, open Task Scheduler, choose the task and select **Run whether user is logged on or not**. Windows asks for your password.

The script uses the `py` launcher. Set `ABX_PYTHON` to another Python if you need to.

## GitHub Actions

Files: `run-export.sh`, `abstract-export-<job>.yml`, the search files.

1. Use a private repository holding the kit's source at `search-export/`. Or set `ABX_APP` in `run-export.sh` to where `abstract_search.py` is.
2. Copy `run-export.sh` and the search files to `automation/<job>/`, and the `.yml` file to `.github/workflows/`.
3. In **Settings → Secrets and variables → Actions**:
   - add the secret `ABSTRACT_API_KEY`;
   - add `ABX_ALERT_WEBHOOK` if you want alerts;
   - add the variable `ABSTRACT_VENDOR_ACCOUNT_ID`.
4. Run it once from the Actions tab before relying on the schedule.

The workflow keeps each export as a workflow artifact for 7 days, in GitHub's cloud. Exports hold your events: use a private repository, shorten `retention-days`, or replace that step with an upload to your own storage. GitHub can start scheduled workflows several minutes late at busy times. That changes when the run starts, not which window it exports.

## Containers: Docker and Kubernetes

Files: `run-export.sh`, `Dockerfile`, `cronjob.yaml`, the search files.

1. Put the kit's `app/` folder next to these files.
2. Build and push the image: `docker build -t <registry>/abstract-export-<job>:latest . && docker push <registry>/abstract-export-<job>:latest`.
3. In Kubernetes:
   - create the secret: `kubectl create secret generic abstract-search-export --from-literal=api-key=…`, or from your secret manager;
   - create a PersistentVolumeClaim called `abstract-exports`;
   - set the image in `cronjob.yaml`, then `kubectl apply -f cronjob.yaml`.
4. Without Kubernetes, run the image from the host's scheduler: `docker run --rm -e ABSTRACT_API_KEY -v /srv/abstract-exports:/exports <image>`.

The CronJob runs one export at a time (`concurrencyPolicy: Forbid`), as a non-root user, on UTC. Keep the exports volume on encrypted storage.

## Setting a job up by hand

The generated recipes above are the recommended way: they compute the window, resume a stopped run and send alerts. A job you write yourself has none of that: each run exports the last 24 hours from the moment it starts, so runs can overlap or leave gaps, and nothing tells you when one fails or is partial. Check the exit code yourself.

A scheduled job cannot type the API key, so it reads it from your password manager or key vault. Set `ABSTRACT_API_KEY_COMMAND` to a command that prints the key:

| Where the key is kept | `ABSTRACT_API_KEY_COMMAND` |
|---|---|
| 1Password (any system) | `op read "op://Security/Abstract API/credential"` |
| Azure Key Vault (any system) | `az keyvault secret show --vault-name my-vault --name abstract-api-key --query value -o tsv` |
| AWS Secrets Manager (any system) | `aws secretsmanager get-secret-value --secret-id abstract/api-key --query SecretString --output text` |
| HashiCorp Vault (any system) | `vault kv get -field=api_key secret/abstract` |
| Mac Keychain | `security find-generic-password -s abstract-api -a api-key -w` |
| Windows SecretManagement | `pwsh -NoProfile -Command (Get-Secret -Name AbstractApiKey -AsPlainText)` |

**Mac and Linux.** Save this as `daily-export.sh` in the app folder, with your own vendor account ID, key command and dataset:

```bash
#!/bin/sh
cd "$(dirname "$0")" || exit 2
export ABSTRACT_VENDOR_ACCOUNT_ID=AbC123xYz0
export ABSTRACT_EXPECT_TENANT=AbC123xYz0
export ABSTRACT_API_KEY_COMMAND='op read "op://Security/Abstract API/credential"'
python3 abstract_search.py export --dataset "Palo Alto Firewall" --last 24h --merge --quiet
```

Make it runnable with `chmod +x daily-export.sh`, then run `crontab -e` and add this line, which runs it every day at 01:30. Replace `/path/to/app` with the real path of your app folder:

```bash
30 1 * * * /path/to/app/daily-export.sh >> /path/to/app/daily-export.log 2>&1
```

**Windows.** Save this as `daily-export.ps1` in the app folder, with your own values. The key command above needs PowerShell 7 (`pwsh`) and the SecretManagement module:

```powershell
Set-Location $PSScriptRoot
$env:ABSTRACT_VENDOR_ACCOUNT_ID = "AbC123xYz0"
$env:ABSTRACT_EXPECT_TENANT = "AbC123xYz0"
$env:ABSTRACT_API_KEY_COMMAND = 'pwsh -NoProfile -Command (Get-Secret -Name AbstractApiKey -AsPlainText)'
py abstract_search.py export --dataset "Palo Alto Firewall" --last 24h --merge --quiet
exit $LASTEXITCODE
```

Then schedule it for every day at 01:30 by running this once in PowerShell. Replace `C:\path\to\app` with the real path of your app folder. `RemoteSigned` lets a script you wrote on this computer run without lifting the policy for everything else:

```powershell
schtasks /create /sc daily /st 01:30 /tn "Abstract daily export" /tr "pwsh -NoProfile -ExecutionPolicy RemoteSigned -File C:\path\to\app\daily-export.ps1"
```

Each run creates a new export folder, and the scheduler adds to its own log (`daily-export.log`). Neither is deleted for you: remove old ones yourself.
