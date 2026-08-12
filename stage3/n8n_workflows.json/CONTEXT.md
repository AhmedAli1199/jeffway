# Stage 3 — n8n Workflows Context

> **Purpose**: Asbestos monitoring and job suspension management. Tracks jobs that are missing asbestos reports, suspends them in EasyBOP, writes notes, creates tasks, and unsuspends them once reports are received.

> ⚠️ Note: Stage 3 workflows live in a folder named `n8n_workflows.json` (unusual naming — it's a directory, not a file).

---

## Files in this folder

| File | Role |
|------|------|
| `fetch_missingASB_suspend_note_task.json` | Find missing asbestos → suspend jobs → write notes → create tasks |
| `fetch_unsuspend.json` | Detect when asbestos reports arrive → unsuspend jobs → write unsuspend notes |

---

## Workflow 1 — `fetch_missingASB_suspend_note_task.json`

**Trigger:** Every 1 hour

**What it does (4 sequential phases):**

### Phase 1 — Find Missing Asbestos
- POSTs to `/fetch-missing-asbestos` (API scrapes EasyBOP pre-works)
- Gets list of jobs that should have an asbestos report but don't
- Appends new entries to **ASB_Tracking** sheet (tab on `SOR-Codes-Template`)
- Skips rows already in the sheet

### Phase 2 — Suspend Jobs
- Reads **ASB_Tracking** rows that are flagged `pending_suspend`
- POSTs to `/suspend-works` (bulk)
- Updates ASB_Tracking with `status = suspended` + timestamp

### Phase 3 — Write Suspension Notes
- Reads **ASB_Tracking** rows flagged `pending_note`
- POSTs to `/bulk-add-notes` with a standard suspension note
- Clears `pending_note` flag after notes are written
- Sends an email notification summarising suspended jobs

### Phase 4 — Create Tasks
- Reads **ASB_Tracking** rows flagged `pending_task`
- Enriches rows with `works_id` using `/works-index enrich` (no extra API call — uses cached data from phase 1)
- POSTs to `/add-asbestos-tasks` (creates a task in EasyBOP for the surveyor to upload the asbestos report)
- Clears `pending_task` flag after tasks are created

---

## Workflow 2 — `fetch_unsuspend.json`

**Triggers:**
- Cron every hour during business hours (Mon–Fri 09:00–19:00 UK)
- Manual trigger (for testing)

**What it does (3 phases):**

### Phase 1 — Find Ready-to-Unsuspend Jobs
- POSTs to `/fetch-ready-for-unsuspend` (API checks pre-works for jobs where asbestos is now green/complete)
- Returns list of CORFs that are now ready
- Shapes the data for the next phase

### Phase 2 — Unsuspend Jobs
- Reads **ASB_Tracking** rows where CORF is in the ready list AND `status = suspended`
- POSTs to `/unsuspend-works` (bulk)
- Updates ASB_Tracking with `status = unsuspended` + timestamp

### Phase 3 — Write Unsuspend Notes
- Reads **ASB_Tracking** rows flagged `pending_unsuspend_note`
- POSTs to `/bulk-add-notes-report-ordered` (writes a note confirming the asbestos report was received and job was unsuspended)
- Clears `pending_unsuspend_note` flag after notes are written

---

## The ASB_Tracking Sheet

Central tracking sheet for Stage 3. Lives as a tab on **SOR-Codes-Template** (`1PRUtOeUec0uMhrj4k1sFEAdo5qvXE9cDOeyAQ67hIDI`).

**Key columns:**
| Column | Meaning |
|--------|---------|
| CORF | Client Order Reference (primary key) |
| works_id | EasyBOP internal job ID |
| status | `pending_suspend` → `suspended` → `unsuspended` |
| pending_note | Flag: write suspension note next run |
| pending_task | Flag: create asbestos task next run |
| pending_unsuspend_note | Flag: write unsuspend note next run |

---

## How Stage 3 Connects to Other Stages

```
Stage 2 [WF3 — Asbestos PDF Upload]
  → uploads asbestos PDFs to EasyBOP
  → EasyBOP changes job's asbestos status from red to green

Stage 3 [fetch_missingASB_suspend_note_task]
  → monitors for NEW red-asbestos jobs → suspends them
  → Stage 2 WF1 (Asbestos Bypass) can bypass jobs that have a WO .msg

Stage 3 [fetch_unsuspend]
  → detects jobs that Stage 2 WF3 uploaded asbestos for (now green)
  → unsuspends them → job can proceed to Stage 6

Stage 6
  → after unsuspend, job becomes active and operative job sheets start being generated
```

---

## API Endpoints Used in Stage 3

| Endpoint | Purpose |
|----------|---------|
| `POST /fetch-missing-asbestos` | Get all pre-works jobs missing asbestos reports |
| `POST /suspend-works` | Bulk suspend jobs in EasyBOP |
| `POST /bulk-add-notes` | Write standard suspension notes to jobs |
| `POST /add-asbestos-tasks` | Create surveyor task for each suspended job |
| `POST /fetch-ready-for-unsuspend` | Get all pre-works jobs now ready (asbestos green) |
| `POST /unsuspend-works` | Bulk unsuspend jobs in EasyBOP |
| `POST /bulk-add-notes-report-ordered` | Write confirmation note when job is unsuspended |
| `POST /works-index enrich` | Get `works_id` for CORFs (cached from pre-works scrape) |

---

## Notes / Gotchas

- All API calls use a **persistent browser session** on the VPS (server-side Playwright) — the same session that scrapes EasyBOP pre-works
- The notes written are standardised text — if the wording needs changing, update the API or the n8n node building the note payload
- Unsuspend only runs Mon–Fri during business hours to avoid acting on data updated outside working hours
- `works_id` enrichment uses the data from the initial pre-works scrape (no second `/works-index` call needed)
