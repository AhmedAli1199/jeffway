# Stage 2 — n8n Workflows Context

> **Purpose**: Pre-Works pipeline — handles asbestos reports, JIS (Job Instruction Sheets) auto-fill, and uploading Works Order `.msg` files to EasyBOP. Sits between the client mailbox receiving works orders and the live EasyBOP system.

---

## Files in this folder

| File | Role |
|------|------|
| `1. Pre-Works Asbestos Bypass.json` | Bypass asbestos requirement for green WO jobs that already have a WO .msg on pre-works |
| `2. Asbestos Report Email to SharePoint.json` | Picks up asbestos PDF reports from BCC mailbox → uploads to SharePoint |
| `3. Pre-Works Asbestos PDF Upload to EasyBOP.json` | Reads SharePoint asbestos PDFs → uploads them into EasyBOP via API |
| `4. Pre-Works JIS Auto-Fill (AI).json` | AI-driven auto-fill of JIS forms in EasyBOP using works order data |
| `5. JIS EasyBOP Filler (Batch).json` | Batch fills JIS forms in EasyBOP |
| `6. BCC Inbox Works Orders to SharePoint.json` | Polls BCC mailbox → saves works order `.msg` files to SharePoint + Google Sheet tracker |
| `7. Pre-Works WO MSG Upload to EasyBOP.json` | Uploads works order `.msg` files from SharePoint into EasyBOP pre-works |
| `asbestos.json` | Earlier/legacy asbestos flow |
| `msg_uploads.json` | Earlier/legacy MSG upload flow |
| `pre_works_asbestos_to_easybop.json` | Earlier/legacy asbestos-to-EasyBOP flow |
| `pre_works_jis_auto_fill.json` | Earlier/legacy JIS auto-fill flow |

> ℹ️ The **numbered files (1–7) are the current live flows**. The un-numbered files are legacy/development versions.

---

## Workflow 1 — Pre-Works Asbestos Bypass

**Trigger:** Daily at 12:00

**What it does:**
Looks at the pre-works table in EasyBOP for jobs with:
- 🔴 Red asbestos status (missing asbestos report) AND
- A green Works Order `.msg` link already present

For those jobs, instead of blocking them, it calls the API endpoint `/pre-works/extract-msg-from-urls-batch` to download the WO `.msg` from EasyBOP (using server-side session), then calls `/asbestos-bypass` to mark the job as bypassed so it doesn't stay stuck waiting for an asbestos report.

**Data flow:**
1. Read `BOQ-Bypass-list` Google Sheet
2. Fetch pre-works HTML table → parse red asbestos rows
3. Fetch works index (CORF → `works_id` map)
4. Check bypass eligibility (green WO link present)
5. POST batch to `/asbestos-bypass`

---

## Workflow 2 — Asbestos Report Email to SharePoint

**Trigger:** Every 2 hours

**What it does:**
Monitors the BCC mailbox for emails with asbestos survey PDF attachments, then uploads them to a SharePoint document library.

**Sources (merged):**
1. Recent messages from the main BCC inbox
2. Messages from the `ASBESTOS SURVEYS` subfolder

**Deduplication:** Checks `asbestos-upload-tracker` Google Sheet using `email_id` + `pdf_filename` — won't re-upload the same PDF twice.

**Data flow:**
1. Init SharePoint drive + Google Sheet tracker
2. Fetch inbox + asbestos subfolder messages
3. Merge and deduplicate email sources
4. Loop emails (batch 40), wait 1s between batches
5. Fetch PDF attachments
6. Filter out already-uploaded PDFs (by checking SP folder listing)
7. Upload new PDFs to SharePoint (batch 4)
8. Mark as uploaded in tracker sheet

**SharePoint target:** `bccvoids` site → BCC document library → Asbestos folder

---

## Workflow 3 — Pre-Works Asbestos PDF Upload to EasyBOP

**Triggers:** Every 30 minutes + daily summary at 18:00 UK time

**What it does:**
Takes the asbestos PDFs that Workflow 2 uploaded to SharePoint, matches them to pre-works jobs in EasyBOP, and uploads the PDF into each job's asbestos document slot.

**Matching logic:**
1. Scrape pre-works jqGrid (all pages) → get jobs with 🔴 red Global Asbestos Survey status
2. POST `/works-index` (paginated) to get CORF → `works_id` + address
3. List SharePoint `Asbestos report` folder → match PDF filenames to CORFs/addresses
4. Download matched PDFs from SharePoint
5. Base64-encode PDFs
6. Batch POST to `/upload-asbestos-pdf-batch` (uploads into EasyBOP per job)

**Daily 18:00 trigger** sends a Gmail summary of that day's asbestos uploads.

**Connects to:**
- Workflow 2 (reads what it uploaded to SharePoint)
- EasyBOP pre-works (writes asbestos PDF to each job)

---

## Workflow 6 — BCC Inbox Works Orders to SharePoint

**Trigger:** Every 1 hour

**What it does:**
The main **inbox poller** for the entire Stage 2 pipeline. Reads BCC mailbox, finds works order emails, saves their `.msg` files to SharePoint, copies emails to regional Outlook folders, and logs everything to a Google Sheet tracker.

**Search categories:** `RR- South`, `RR- North` (and others — regional Response Repairs)

**Data flow:**
1. Init: Get SharePoint drive + Google Sheet tracker + Outlook folder IDs
2. List regional inbox subfolders
3. Fetch messages from each regional folder
4. For each email:
   - Check if MSG already on SharePoint (dedup by file name check)
   - If new: export MIME or download MSG attachment → upload to SharePoint
   - Copy email to appropriate regional Outlook folder
   - Append row to **Work Order Form** Google Sheet (CORF, subject, sender, date, SharePoint URL)
   - Mark as uploaded in tracker
5. Backfill mode: can process historical emails

**Work Order Form sheet** is the central tracker used by Workflow 7.

**SharePoint target:** `bccvoids` site → BCC library → `RR-South` (or North etc.) folder

---

## Workflow 7 — Pre-Works WO MSG Upload to EasyBOP

**Trigger:** Every 1 hour

**What it does:**
Takes the `.msg` files that Workflow 6 uploaded to SharePoint and uploads them into the pre-works Works Order slot in EasyBOP for each matching job.

**Data flow:**
1. Read **Work Order Form** Google Sheet (the tracker built by WF6)
2. Scrape pre-works HTML table (all pages) → get current WO status (green = already uploaded, red = not yet)
3. Fetch works index → build CORF → `works_id` map
4. List SharePoint `RR-South` folder → find `.msg` files
5. Match SharePoint MSGs to pre-works jobs by CORF
6. Filter: only red WO status jobs that have a matching MSG in SharePoint
7. Download MSG from SharePoint → encode as base64
8. Batch POST to `/wi-msg upload-batch` (uploads into EasyBOP per job)
9. Update Work Order Form sheet:
   - Jobs now green on pre-works → mark `.msg on easybop = TRUE`
   - Jobs just uploaded → mark upload date

**Connects to:**
- Workflow 6 (reads its SharePoint uploads + Work Order Form sheet)
- EasyBOP pre-works (uploads the .msg into each job)

---

## How the Numbered Workflows Connect Together

```
BCC Mailbox (BCCorders@jeffway.co.uk)
        │
        ▼
[6] BCC Inbox → SharePoint
  - Saves .msg to SharePoint RR-South folder
  - Logs to Work Order Form sheet
        │
        ▼
[7] Pre-Works WO MSG Upload to EasyBOP
  - Reads SharePoint .msg files
  - Uploads .msg into EasyBOP pre-works per CORF
  - Updates Work Order Form sheet

BCC Mailbox (Asbestos emails)
        │
        ▼
[2] Asbestos Email → SharePoint
  - Saves asbestos PDFs to SharePoint Asbestos folder
        │
        ▼
[3] Asbestos PDF Upload to EasyBOP
  - Matches PDFs to pre-works jobs
  - Uploads PDF into EasyBOP asbestos slot

Pre-Works Red Asbestos + Green WO link
        │
        ▼
[1] Asbestos Bypass
  - Bypasses asbestos block when WO already present

EasyBOP Pre-Works (jobs ready with WO + Asbestos)
        │
        ▼
[4] JIS Auto-Fill (AI)  +  [5] JIS Filler (Batch)
  - AI extracts data from works order
  - Auto-fills JIS form fields in EasyBOP
```

---

## Shared Infrastructure

| Resource | Details |
|----------|---------|
| **API server** | `http://173.212.233.153:8000` (or localhost) — FastAPI + Playwright |
| **SharePoint site** | `bccvoids` |
| **BCC mailbox** | `BCCorders@jeffway.co.uk` (Microsoft Graph API) |
| **Work Order Form sheet** | Central tracker (CORF, SharePoint URL, upload status) |
| **SOR-Codes-Template sheet** | `1PRUtOeUec0uMhrj4k1sFEAdo5qvXE9cDOeyAQ67hIDI` |

---

## Key API Endpoints Used in Stage 2

| Endpoint | Used by |
|----------|---------|
| `POST /pre-works/extract-msg-from-urls-batch` | WF 1 |
| `POST /asbestos-bypass` | WF 1 |
| `POST /upload-asbestos-pdf-batch` | WF 3 |
| `POST /works-index` | WF 3, 7 |
| `POST /wi-msg upload-batch` | WF 7 |

---

## Connection to Other Stages

- **Stage 1**: Stage 1 also reads BCC mailbox for the same works orders but processes them for WI/BOQ import into EasyBOP. Stage 2 handles the pre-works side (before Stage 1 creates the formal work instruction).
- **Stage 3**: Stage 3 handles asbestos monitoring (suspend/unsuspend jobs) — it reads EasyBOP pre-works status built by Stage 2.
- **Stage 6**: Once jobs are active in EasyBOP (after WI upload from Stage 1 and pre-works processing from Stage 2), Stage 6 monitors job completion via Operative Job Sheets.
