# Jeffway EasyBOP Automation

View logs
sudo /root/projects/jeffway/deploy/jeffway-api.sh logs

restart server:
sudo /root/projects/jeffway/deploy/jeffway-api.sh restart

This project contains:

- A FastAPI service in `filler/` for EasyBOP navigation/upload automation.
- An n8n workflow export in `stage1-live/Top10_WI+BOQ.json`.
- Downloaded Excel files in `excel_downloads/`.

## Current FastAPI Scope

The API currently does **not** do individual form filling.

It supports:

- login/session handling
- opening BOQ import page
- uploading BOQ Excel file on import page
- downloading the **Works Instruction import template** from EasyBOP into `work_inst_template/`
- test webhook and helper download endpoint

## Project Structure

- `filler/main.py` - FastAPI app and Playwright flows
- `filler/automation.py` - login helpers used by API
- `filler/config.py` - `.env`-driven settings
- `stage1-live/Top10_WI+BOQ.json` - n8n workflow export
- `excel_downloads/` - local folder for `.xlsx` files
- `work_inst_template/` - EasyBOP WI template downloads (see `POST /download-works-template`)
- `work_inst_template/download_works_template.py` - Playwright flow for template button

## Requirements

- Python 3.12+
- Playwright Chromium
- Linux server or local machine

## Setup

From `filler/`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## Environment File

Create/update `.env` at project root (`/root/projects/jeffway/.env`).

Common keys:

```env
username=YOUR_EASYBOP_USERNAME
password=YOUR_EASYBOP_PASSWORD
easybop_base_url=https://easybop.co.uk
headless=true
EASYBOP_KEEP_PAGE_OPEN=true
EASYBOP_SLOW_MO_MS=80
request_timeout_ms=20000
session_file=easybop_session.json
EASYBOP_RELOAD=false
```

Notes:

- On headless servers, keep `headless=true`.
- `session_file` is relative to `filler/` by default.

## Run API

### Keep running after you close SSH (recommended)

Install as a systemd service so port 8000 stays up when your remote session ends:

```bash
sudo /root/projects/jeffway/deploy/jeffway-api.sh install
```

Manage it:

```bash
sudo /root/projects/jeffway/deploy/jeffway-api.sh status   # is it running?
sudo /root/projects/jeffway/deploy/jeffway-api.sh restart  # after code changes
sudo /root/projects/jeffway/deploy/jeffway-api.sh logs     # live logs
sudo /root/projects/jeffway/deploy/jeffway-api.sh stop
```

The service sets `EASYBOP_RELOAD=false` (no dev auto-reload). Restart after editing Python files.

### Foreground (dev only — stops when terminal closes)

```bash
cd /root/projects/jeffway/filler
.venv/bin/python main.py
```

Swagger:

- Local: `http://127.0.0.1:8000/docs`
- Server IP: `http://<server-ip>:8000/docs`

## Main Endpoints

- `GET /health`  
  Liveness and browser/session status.

- `POST /login`  
  Logs into EasyBOP and stores session state.

- `POST /open-boq-import`  
  Opens EasyBOP BOQ import URL for a contract.

  Example body:

  ```json
  {
    "contract_id": "321129",
    "keep_page_open": true
  }
  ```

- `POST /download-wi-xlsx`  
  Exports the WI-fill tab from Google Sheets to `excel_downloads/WI-fill.xlsx` (call before `/upload-works-file`).

- `POST /download-boq-xlsx`  
  Exports the BOQ tab to `excel_downloads/BOQ-verify.xlsx` (call before `/upload-boq-file`).

- `POST /download-excel-files`  
  Legacy combined download (`download_wi` / `download_boq` flags). Prefer the endpoints above.

- `POST /upload-boq-file`  
  Full BOQ import: FilePond → Upload File → confirm OK(s) → Save changes.

  Example body:

  ```json
  {
    "contract_id": "321129",
    "file_path": "/root/projects/jeffway/excel_downloads/BOQ-verify.xlsx"
  }
  ```

- `POST /upload-works-file`  
  Full WI import: FilePond → Upload File → confirm OK → wait for import result.

  Example body:

  ```json
  {
    "contract_id": "321129",
    "file_path": "/root/projects/jeffway/excel_downloads/WI-fill.xlsx"
  }
  ```

- `POST /download-works-template`  
  Opens `import_works.php` for a contract, clicks **Download Template (Including Works Instructions)** (`#btn_works_template_wi`), saves the file under `work_inst_template/` by default.

- `POST /download-boq-template` / `POST /download-boq-template/file`  
  Opens [import_boqs.php](https://easybop.co.uk/a_planned_works/z_works/import_boqs.php?contract_id=321129), clicks **Download Template** (`#btn_without_data`), saves under `boq_template/`. Use `/file` for n8n binary response (same pattern as WI).

  Example body:

  ```json
  {
    "contract_id": "321129",
    "output_dir": null,
    "keep_page_open": true
  }
  ```

  Optional: set `output_dir` to an absolute path to save elsewhere.

- `GET /files/reactive-works-instructions-template`  
  Serves `work_inst_template/Reactive-Works-Instructions-Template.xlsx` after you run `POST /download-works-template` (for n8n HTTP or scripts).

- `POST /test-webhook`  
  Lightweight connectivity check endpoint.

- `POST /download-excel-files`  
  Attempts server-side sheet export to `excel_downloads/` (legacy — use `/download-wi-xlsx` and `/download-boq-xlsx`).

## Stage 6 Endpoints

- `POST /extract-operative-job-sheets`  
  Scrape completed Operative job sheet PDFs from EasyBOP SmartForms register and parse Q fields.

- `POST /works-index`  
  Scrape all works from the EasyBOP planned-works index (`works_id` + `address`).

- `POST /fetch-boq-for-corf`  
  Fetch BOQ/SOR lines for a single property address.

- `POST /fetch-boq-batch`  
  Fetch BOQ/SOR for up to 3 jobs in parallel (pass `works_id` from `/works-index` for faster lookups).

- `POST /boq-pool/reset`  
  Close persistent BOQ tabs and clear the works-index cache after a workflow run.

- `POST /generate-job-report-pdf`  
  Generate Stage 6 job status PDF from Operative Job Sheet + OJS BOQ & SMV rows (returns PDF bytes).

- `POST /generate-daily-exception-report-pdf`  
  Generate daily exception report PDF from Operative Job Sheet + OJS BOQ & SMV rows (returns PDF bytes).

## Live Logs

If running in foreground:

```bash
cd /root/projects/jeffway/filler
.venv/bin/python main.py
```

Watch logs directly in that terminal.

If already running in background, check port/process:

```bash
ss -ltnp | grep ':8000'
```

## n8n Workflow

Workflow file:

- `stage1-live/Top10_WI+BOQ.json`

Recent duplicate-check behavior:

- if `UPRN` exists -> duplicate key uses `UPRN|Client Order Reference`
- if `UPRN` missing -> duplicate check falls back to `|Client Order Reference`

## Quick Troubleshooting

- `Address already in use` on startup:
  - another process is already using port 8000
  - stop old process or reuse existing running instance

- Upload endpoint returns login/unauthorized:
  - call `POST /login` first
  - verify credentials in `.env`

- API appears down:
  - check `ss -ltnp | grep ':8000'`
  - restart with `.venv/bin/python main.py`

- n8n HTTP node “connection refused” on `GET /files/reactive-works-instructions-template`:
  - FastAPI must be running and reachable from wherever n8n runs (same server → `http://127.0.0.1:8000`; different machine → public IP/DNS and open port `8000`, or a tunnel/reverse proxy).
  - If n8n runs in Docker on the same host as the API, try `http://host.docker.internal:8000` (or the host’s LAN IP).
