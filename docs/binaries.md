# The single-file program

Each release carries one file per system that needs no Python and no Docker. It contains Python, the tool, the browser app's page and the in-app help.

| System | File |
|---|---|
| Windows 10 or 11, Intel | `abstract-search-<version>-windows-x64.exe` |
| macOS, Apple silicon | `abstract-search-<version>-macos-arm64` |
| Linux, Intel (glibc 2.35 or newer) | `abstract-search-<version>-linux-x64` |
| Linux, Arm (glibc 2.35 or newer) | `abstract-search-<version>-linux-arm64` |

An Intel Mac, or an older Linux, runs the tool with Docker or with Python instead.

## Check the download

Download `SHA256SUMS` from the same release and compare:

```
shasum -a 256 -c SHA256SUMS --ignore-missing      # macOS
sha256sum -c SHA256SUMS --ignore-missing          # Linux
```

```
(Get-FileHash .\abstract-search-0.6.0-windows-x64.exe).Hash      # Windows: compare with the line in SHA256SUMS
```

With the GitHub CLI you can also check that the file was built by this repository's release workflow:

```
gh attestation verify abstract-search-0.6.0-macos-arm64 --repo IamABS3C/abstract-search-export
```

## Run it

macOS and Linux:

```
chmod +x abstract-search-0.6.0-macos-arm64
./abstract-search-0.6.0-macos-arm64 --web        # the browser app
./abstract-search-0.6.0-macos-arm64              # the guided terminal
./abstract-search-0.6.0-macos-arm64 --help       # the command line
```

Windows: double-click the file for the guided terminal, or in PowerShell run `.\abstract-search-0.6.0-windows-x64.exe --web`.

The first start takes a few seconds while the file unpacks itself. Every command in the docs works the same way: where they say `python3 abstract_search.py …`, use the file's name instead.

## The first-run prompts

The files are not signed with an Apple or Microsoft certificate, so each system asks before the first run.

- **macOS** says the file cannot be opened because the developer cannot be verified. After checking the download as above, open **System Settings → Privacy & Security**, find the message about the file and choose **Open Anyway**.
- **Windows** shows "Windows protected your PC". After checking the download, choose **More info**, then **Run anyway**.

Where your organisation does not allow unsigned programs, use the Docker image or Python.

## What is different from the Python version

- The scheduled-job generator (`automate`) writes jobs that run the Python version. To schedule exports with the single file, call it from your own scheduler entry (`abstract-search export … --merge`; exit code 0 or 3 is success), or run the generated job with Python or the Docker image.
- There is no `tests` folder inside it. The tests run from the repository.

## Build it yourself

```
python3 -m pip install pyinstaller==6.22.3
python3 scripts/build-binary.py
```

The script builds the file for the system it runs on, then proves it: it must report its version, print its help, and serve the browser app's page, script, style and a font from inside itself. The result is in `dist/`.
