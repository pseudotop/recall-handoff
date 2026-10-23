# Recall Handoff — local web and MCP demo

Run from the repository or extracted source-bundle root. Python 3.12 is the tested runtime. The web app listens only on `127.0.0.1`, starts its own MCP server, and uses a local SQLite database. It needs internet access for the public CPSC queries. It does not need an Amazon device or gated Alexa tools.

## Windows PowerShell

Use an installed Python 3.12 executable. Replace the first path below with its
actual location; a Microsoft Store execution alias is not a Python installation.
The basic workflow needs the small MCP dependency, without PyTorch or model files.

```powershell
& 'C:\path\to\Python312\python.exe' -m venv .local\recall-handoff\venv
$python = (Resolve-Path '.local\recall-handoff\venv\Scripts\python.exe').Path
& $python -m pip install -r competitions/amazon-build-ship-shape2026/preparation/requirements-probe.txt
& $python -B competitions/amazon-build-ship-shape2026/tests/run_tests.py
& $python -B competitions/amazon-build-ship-shape2026/src/demo_server.py --db '.local\recall-handoff\cases.sqlite' --port 8787
```

Open `http://127.0.0.1:8787`. Without `--model-dir`, the source-review and ledger
flow works and excerpt ranking reports that the local model is not configured.
Stop the server with Ctrl+C and use the same database path to resume saved cases.

## Basic workflow

```bash
python3 -m venv .local/recall-handoff/venv
.local/recall-handoff/venv/bin/python -m pip install -r competitions/amazon-build-ship-shape2026/preparation/requirements-probe.txt
.local/recall-handoff/venv/bin/python competitions/amazon-build-ship-shape2026/src/demo_server.py --db .local/recall-handoff/cases.sqlite --port 8787
```

Open `http://127.0.0.1:8787`. Search a brand/model, inspect the full manufacturer name and source, choose a notice, and confirm saving the review. Record only the actions you performed. Copy its review ID to reopen it later. Stop and restart the server with the same `--db` path to resume the exact local record. A stopped review is not a manufacturer's remedy decision.

## Optional offline model

The model is approximately 1GB of additional files; PyTorch and its environment need more disk/memory. It was run on Linux CPU. The restore script verifies the fixed revision and all nine file sizes/SHA-256 values. Model files and installed libraries stay under `.local`.

```bash
.local/recall-handoff/venv/bin/python -m pip install 'torch==2.8.0+cpu' --index-url https://download.pytorch.org/whl/cpu
.local/recall-handoff/venv/bin/python -m pip install -r competitions/amazon-build-ship-shape2026/preparation/requirements-model.txt
.local/recall-handoff/venv/bin/python competitions/amazon-build-ship-shape2026/preparation/setup_local_model.py --output-dir .local/recall-handoff/model
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .local/recall-handoff/venv/bin/python competitions/amazon-build-ship-shape2026/src/demo_server.py --db .local/recall-handoff/cases.sqlite --model-dir .local/recall-handoff/model --port 8787
```

After saving a notice, choose **Suggest a source excerpt**. Qwen ranks up to eight excerpts using next-token scores for their labels. The app shows an exact original excerpt and a fixed next-check prompt. These scores are not calibrated confidence and do not establish product safety, eligibility, remedy success, or an advantage over a manual checklist. With no model configured, source review and all ledger operations remain available.

## Verification

```bash
python3 -m unittest discover -s competitions/amazon-build-ship-shape2026/tests -v
```

The explicit browser smoke additionally needs Python Playwright, its ffmpeg asset
for video capture, and a browser. It uses Playwright's installed Chromium by
default, or an existing executable supplied with `--browser-executable`. Install
only the needed runtime assets and use a new output directory. Linux example
using an existing system browser:

```bash
.local/recall-handoff/venv/bin/python -m pip install 'playwright==1.62.0'
PLAYWRIGHT_BROWSERS_PATH=.local/recall-handoff/playwright .local/recall-handoff/venv/bin/python -m playwright install ffmpeg
PLAYWRIGHT_BROWSERS_PATH=.local/recall-handoff/playwright .local/recall-handoff/venv/bin/python competitions/amazon-build-ship-shape2026/preparation/probe_demo.py --browser-executable /usr/bin/chromium --output .local/recall-handoff/new-browser-run
```

Windows PowerShell with an existing Chrome/Chromium executable:

```powershell
& $python -m pip install 'playwright==1.62.0'
$env:PLAYWRIGHT_BROWSERS_PATH = '.local\recall-handoff\playwright'
& $python -m playwright install ffmpeg
& $python -B competitions/amazon-build-ship-shape2026/preparation/probe_demo.py --browser-executable 'C:\path with spaces\chrome.exe' --output '.local\recall-handoff\new-browser-run'
```

Omit `--browser-executable` only when Playwright's matching Chromium is already
installed. Browser reproduction is separate from the offline unit tests. The
full model smoke below requires a configured model; it must not be reported as
passed from a source-only startup or an argument/help check.

The smoke performs three live public CPSC GETs and creates clearly marked development records in the chosen demo database. To check actual restart persistence, stop/restart the app with that database and run the probe again with a different `--output` plus `--resume-result <first-run/result.json>`. Recorded actions concern public text and synthetic review records, not a physical owned product or manufacturer contact.

The CPSC queries cover only the requested fields and may be incomplete. Partial query errors remain partial, incompatible source versions stop the result, and no hits mean no candidate in that query range. The app never requests a remedy or sends a message. This independent frontend is the demonstrated Alexa+ track simulation; an actual Alexa connection has not been run.
