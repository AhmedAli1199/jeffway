# Stage 1 — n8n Workflows Context

> **Purpose**: Ingest new Reactive Repairs (RR) works orders from the BCC email inbox, build Work Instructions (WI) and BOQ sheets, then upload them into EasyBOP.

---

## Files in this folder

| File | Role |
|------|------|
| `1.json` | **Master Orchestrator** — runs every 2 hours, sequences all sub-steps |
| `2.json` | **WI + BOQ Builder** — processes the inbox, extracts WO data via AI, builds staging rows |
| `work_inst_import_from_inbox.json` | Earlier/alternative inbox import flow |
| `work_inst_upload.json` | Standalone WI upload helper |
| `workslist_export.json` | Exports the works list from EasyBOP |

---

## Workflow 1 — `1.json` (Master Orchestrator)

**Trigger:** Cron every 2 hours (`0 */2 * * *`)

### Pipeline Steps (in order)

1. **Refresh WI Template** — Clears `RR_work_instruction_template` tab on `SOR-Codes-Template` sheet, then POSTs to `/download-works-template/file` (API on `173.212.233.153:8000`) to get the latest xlsx, reads it row by row, and appends rows back to the sheet. This gives the duplicate-check layer fresh data so WF2 can compare against the latest EasyBOP state.

2. **Execute WI+BOQ** — Calls workflow `2.json` (sub-workflow execute) which reads the BCC mailbox, parses works orders, runs AI extraction, and writes staged rows to the `Reactive Works to Import- copy` sheet on `Reactive-Works-Instructions-Template`.

3. **Check Has Pending WI** — Reads the staging sheet. If it's empty (no new orders), stops cleanly with `NO_NEW_ORDERS` status.

4. **Download WI xlsx** — POSTs to `/download-wi-xlsx` → saves `WI-fill.xlsx` locally.

5. **Upload WI to EasyBOP** — POSTs to `/upload-works-file` (Playwright-based import, synchronous). Uses `contract_id: 321129`.

6. **Verify WI Upload** — Re-downloads the WI template from EasyBOP, compares every CORF (Client Order Reference) in the pending sheet against what is now in EasyBOP. If all CORFs are found → `upload_verified = true`. If any are missing → `WI_UPLOAD_FAILED`, pending sheet is kept intact, retries next 2h run.

7. **BOQ Pipeline** (only if WI verified):
   - Clear pending WI sheet
   - Clear `RR_BOQ_template` tab
   - Download BOQ template xlsx from EasyBOP (`/download-boq-template/file`)
   - Read `Works Instructions` tab
   - Append to `RR_BOQ_template`
   - Fix/prune BOQ (remove rows whose WI Ref is not in the template)
   - Upload BOQ to EasyBOP (`/upload-boq-file`)
   - Clear BOQ tab after successful upload

**Google Sheets used:**
- `SOR-Codes-Template` (`1PRUtOeUec0uMhrj4k1sFEAdo5qvXE9cDOeyAQ67hIDI`) — tabs: `RR_work_instruction_template`, `RR_BOQ_template`, `BOQ-item-match`, `36-BCC-RR-BOQ-TEMPLATE`
- `Reactive-Works-Instructions-Template` (`17WI7AM3bdtdwbMnBj4Ldcb7PVBv9iMOhoNxnqvdau3A`) — tab: `Reactive Works to Import- copy`

---

## Workflow 2 — `2.json` (WI + BOQ Builder / Inbox Processor)

**Trigger:** Called as sub-workflow by `1.json` (also has a midnight cron for standalone runs)

**What it does:**
- Reads Microsoft Graph API to fetch emails from `BCCorders@jeffway.co.uk` inbox
- Filters to category `RR- South` (Response Repairs)
- For each email: extracts full body (HTML → plain text), parses works order fields (UPRN, CORF, address, cost, SOR lines, priority, dates) using regex patterns + optional AI extraction
- Runs AI (`Claude` / OpenAI) to classify the email, extract property details, resident info, description of works
- Builds a `Build WI Fill Row` — a fully structured row matching the EasyBOP WI import template columns
- Runs duplicate pre-check: compares incoming CORFs against existing rows in `SOR-Codes-Template` copy sheet, WI template, and reference sheets. Flags duplicates, variations (same CORF, different cost), and same-run duplicates
- Appends new, non-duplicate rows to `Reactive Works to Import- copy` (the staging sheet)
- Also builds BOQ rows from SOR lines parsed from the email body, appending to `RR_BOQ_template`

**Key Google Sheets:**
- `SOR-Codes-Template` — `BOQ-item-match` tab (SOR code → description lookup), `36-BCC-RR-BOQ-TEMPLATE` (works ref lookup)
- `Reactive-Works-Instructions-Template` — `Reactive Works to Import- copy` (staging/pending WI rows)

---

## How Stage 1 Connects to Other Stages

```
Stage 2 (Pre-Works / JIS)
  ↕ shares: BCC inbox mailbox, SharePoint .msg files, Works Order Form sheet

Stage 1 (This stage)
  → uploads WI + BOQ into EasyBOP
  → EasyBOP WI data is later read by Stage 6 (OJS extraction / job status)

Stage 6
  → reads EasyBOP SmartForms (Operative Job Sheets)
  → reads the same SOR-Codes-Template sheet for BOQ/SOR lookups

Stage 7
  → reads Operative Job Sheet (built by Stage 6)
  → creates handover tasks and variation submissions back into EasyBOP
```

---

## API Server

All Playwright-based operations go through the FastAPI server at `http://173.212.233.153:8000` (or `http://localhost:8000` when running locally). Key endpoints used by Stage 1:

| Endpoint | Purpose |
|----------|---------|
| `POST /download-works-template/file` | Download WI template xlsx (binary response) |
| `POST /download-boq-template/file` | Download BOQ template xlsx (binary response) |
| `POST /download-wi-xlsx` | Export staging sheet to `WI-fill.xlsx` |
| `POST /upload-works-file` | Upload WI xlsx into EasyBOP |
| `POST /upload-boq-file` | Upload BOQ xlsx into EasyBOP |

---

## Key Data Fields

- **CORF** (Client Order Reference) — the primary unique key for a works order (e.g. `12345` or `123/456`)
- **Works Ref** — EasyBOP internal reference, derived from CORF after import
- **SOR codes** — Schedule of Rates codes, e.g. `72EL841007`, used to build BOQ line items
- **Contract ID** — `321129` (hardcoded, BCC Response Repairs contract)

---

## Notes / Gotchas

- **Do NOT clear the BOQ tab before BOQ upload succeeds** — the orchestrator is designed so clearing only happens after a confirmed successful upload
- **Pending sheet is kept intact on failure** — the next 2h run will retry automatically
- BOQ prune step removes rows where the WI Ref does not appear in the EasyBOP template (prevents orphaned BOQ lines)
- `_pipeline_mode` flag on rows can be `full` (WI + BOQ) or `boq_only` (skip WI upload, just do BOQ)
