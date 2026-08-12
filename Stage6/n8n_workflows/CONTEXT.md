# Stage 6 — n8n Workflows Context

> **Purpose**: Job monitoring and progress tracking. Extracts completed Operative Job Sheets (OJS) from EasyBOP SmartForms, tracks job status (completion, codes, blockers), generates reports, and feeds the completion data into Stage 7 (handover/variation).

---

## Files in this folder

| File | Role |
|------|------|
| `ojs_extraction.json` | Scrape new Operative Job Sheets from EasyBOP → append to Operative Job Sheet tracker |
| `job status check.json` | AI-powered job completion assessment per job |
| `BOQ_extraction.json` | Extract BOQ/SOR lines for active jobs |
| `ojs_photos_upload.json` | Upload job photos from EasyBOP to SharePoint/storage |
| `ojs_wi_template_sync.json` | Sync WI template with OJS data |
| `next_appointment_cleanse.json` | Clean/validate next appointment dates |
| `scheduling_register_export.json` | Export scheduling register data |
| `weekly_report.json` | Generate weekly summary report |
| `work_inst_list_export.json` | Export work instruction list |
| `transscript.txt` | Developer notes/transcript (not a workflow) |

---

## Workflow — `ojs_extraction.json` (OJS Extraction)

**Trigger:** Every 2 hours

**What it does:**
1. Reads the **Operative Job Sheet** Google Sheet → builds skip lists (by `smart_form_x_id` and by `SmartForm Name` + `Date Created`)
2. POSTs to `/extract-operative-job-sheets` (API scrapes EasyBOP SmartForms register for completed BCC Response Repairs forms)
3. Filters out forms already in the sheet (dedup by ID and name+date)
4. Appends new rows to the **Operative Job Sheet**
5. Re-sorts the full sheet by `SmartForm Name` + `Date Created`
6. Rewrites sorted rows back to sheet

**Operative Job Sheet columns include:**
- SmartForm Name (job identifier)
- Date Created
- smart_form_x_id (EasyBOP internal ID)
- Job Status
- Ready To Hand over
- Hand Over Task Created
- Variation Created
- QS Review Task Created
- Various Q-field answers (Q2.2, Q3.1, etc. — completion code blockers)

---

## Workflow — `job status check.json` (AI Job Status Check)

**Trigger:** Every 1 hour

**What it does:**
Reads the **Operative Job Sheet** + **OJS BOQ & SMV** sheet, groups rows by SmartForm Name (job), then for each job runs an AI assessment of whether it's complete.

**Skip logic (production optimisation):**
- **Skip entirely**: if Completed date + Ready To Hand over = Yes + inputs unchanged → no AI call needed
- **Code only**: if completion date exists but code blockers present (Q2.2 / Q3.1) → AI only checks codes, not full completion
- **Full assessment**: everything else

**AI output per job:**
- `Job Status` (Completed / In Progress / Blocked / etc.)
- `Ready To Hand over` (Yes/No)
- Code analysis results

**Writes back to:** Operative Job Sheet (updates `Job Status`, `Ready To Hand over` columns)

**Uses Google Sheets:**
- **Operative Job Sheet** (read + write)
- **OJS BOQ & SMV** (read — SOR/BOQ data for the job)

---

## Workflow — `BOQ_extraction.json` (BOQ Extraction)

**Trigger:** Scheduled (runs periodically)

**What it does:**
For active jobs that don't yet have BOQ data in the tracker, fetches their BOQ/SOR lines from EasyBOP using:
- `POST /fetch-boq-batch` (up to 3 jobs in parallel, uses cached `works_id`)
- `POST /fetch-boq-for-corf` (single job fallback)

Appends BOQ/SOR data to the **OJS BOQ & SMV** sheet which is then used by:
- `job status check.json` (for AI completion assessment)
- Stage 7 (for variation building)

---

## How Stage 6 Connects to Other Stages

```
Stage 1
  → uploaded WI into EasyBOP (created the job)
  → EasyBOP generates Operative Job Sheets as operatives work

Stage 6 [ojs_extraction]
  → scrapes completed OJS forms from EasyBOP SmartForms
  → builds the Operative Job Sheet tracker

Stage 6 [BOQ_extraction]
  → fetches BOQ/SOR lines for each job
  → populates OJS BOQ & SMV sheet

Stage 6 [job status check]
  → AI assesses job completion
  → sets "Ready To Hand over" flag on Operative Job Sheet

Stage 7
  → reads Operative Job Sheet (jobs where Ready To Hand over = Yes)
  → creates handover tasks in EasyBOP
  → builds variation submissions (Stage 7 variation & task workflow)
```

---

## Shared Google Sheets

| Sheet | Tab | Used by |
|-------|-----|---------|
| Operative Job Sheet | (main tab) | ojs_extraction, job status check, Stage 7 |
| OJS BOQ & SMV | (BOQ lines) | BOQ_extraction, job status check, Stage 7 |
| SOR-Codes-Template | Various | BOQ lookups, WI template |
| RR_work_instruction_template_s6 | S6 WI data | Stage 7 export_doc_pack |

---

## API Endpoints Used in Stage 6

| Endpoint | Purpose |
|----------|---------|
| `POST /extract-operative-job-sheets` | Scrape EasyBOP SmartForms register |
| `POST /works-index` | Get `works_id` + address for jobs |
| `POST /fetch-boq-for-corf` | Fetch BOQ/SOR for a single job |
| `POST /fetch-boq-batch` | Fetch BOQ/SOR for up to 3 jobs in parallel |
| `POST /boq-pool/reset` | Close persistent BOQ tabs + clear cache after a run |
| `POST /generate-job-report-pdf` | Generate job status PDF report |
| `POST /generate-daily-exception-report-pdf` | Generate daily exception report PDF |

---

## Notes / Gotchas

- **OJS BOQ & SMV** sheet is required for the AI job status check — make sure BOQ extraction runs before job status check in any manual trigger sequence
- The AI assessment uses significant tokens — the skip logic is there to avoid re-assessing already-complete unchanged jobs
- `boq-pool/reset` should be called after BOQ extraction runs to free persistent browser tabs
- `transscript.txt` in this folder is developer notes/transcript — not a workflow, safe to ignore
- The Stage 6 `config.py` sets the API base URL and sheet IDs used by the Python helpers (`api.py`, `automation.py`, etc.)
