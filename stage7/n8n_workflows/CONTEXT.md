# Stage 7 — n8n Workflows Context

> **Purpose**: End-of-job processing. Once Stage 6 marks a job as "Ready To Hand over", Stage 7 creates the handover task in EasyBOP, submits variations (additional cost SOR lines), creates QS review tasks, and exports the full document pack (photos, job report, WI xlsx).

---

## Files in this folder

| File | Role |
|------|------|
| `variation & task.json` | Build variation from SOR codes → submit to EasyBOP + create QS review task |
| `handover_task.json` | Create handover task in EasyBOP for each completed job |
| `export_doc_pack.json` | Export full document pack (photos zip, Excel, job report) and create email draft |
| `post handover.json` | Post-handover processing steps |

---

## Workflow — `variation & task.json` (Part 1)

**Trigger:** Scheduled (interval not set in JSON — likely manual or configured in n8n UI)

**What it does:**
Processes completed jobs from the **Operative Job Sheet** that have BOQ/SOR data but haven't had a variation submitted yet.

**Steps:**
1. Read **Operative Job Sheet** → filter jobs that are completed + have BOQ data + `Variation Created` is empty
2. Read **SOR Code Mappings** sheet
3. For each job: AI scores the operative's description against the SOR codes found in the OJS to determine which SOR lines are genuine additional work (variations)
4. Run a final AI scoring pass to confirm/refine variation lines
5. Build variation API payload (SOR code, quantity, cost, description per line)
6. POST to `stage7_add_variation_post_And_create_tasks2` endpoint:
   - Submits the variation to EasyBOP
   - Creates a QS review task in EasyBOP
7. Update **Operative Job Sheet** with `Variation Created` date + `QS Review Task Created` date

**Key AI behaviour:**
- Compares what the operative wrote in their job notes against the BOQ/SOR lines
- Scores each SOR line 0–100 for relevance
- Only includes lines scoring above threshold as variation items

**Skips** jobs where `Variation Created` or `QS Review Task Created` are already set.

---

## Workflow — `handover_task.json` (Stage 7 — Handover Task)

**Trigger:** Manual trigger (test mode — likely triggered by schedule or from another workflow in production)

**What it does:**
Creates a handover task in EasyBOP for Shannon (the handover coordinator) for each job that is ready.

**Filter criteria (from Operative Job Sheet):**
- Form type: BCC Response Repairs
- Job Status = Completed (note: this is `Job Status` column, NOT the SmartForm `Status` field)
- `Ready To Hand over` = empty or No (i.e., Stage 6 hasn't marked it yet, or it just became ready)
- `Hand Over Task Created` = empty or No

**Steps per job:**
1. `POST /resolve-works-id` → get `works_id` for this CORF
2. `POST /create-handover-task` → create task in EasyBOP assigned to Shannon
3. Update **Operative Job Sheet** `Hand Over Task Created` = date

**Note:** The workflow is set up for "Shannon" as the assignee — this is a named person in the business.

---

## Workflow — `export_doc_pack.json` (Part 2 — Export Doc Pack)

**Trigger:** Scheduled

**What it does:**
When `Ready to handover = Yes` AND `QS Reviewed = Yes`, exports a full document pack for the job.

> ⚠️ Note: Google Sheets filters are case-sensitive for `yes/Yes` — the code node handles this with case-insensitive matching.

**Document pack contents:**
1. **Job Report PDF** — generated from OJS data
2. **Excel** — WI xlsx for the job (from `RR_work_instruction_template_s6`)
3. **Photos ZIP** — downloads all photos from EasyBOP for the job, zips them

**Steps:**
1. Read `RR_work_instruction_template_s6` → filter rows where Ready to handover = Yes + QS Reviewed = Yes + not already exported
2. Build doc pack queue
3. For each job:
   - POST to `stage7_export_job_documents_post` (generates the PDFs)
   - Download images from EasyBOP photos → zip them
   - Match address + property ref for the email
   - Create email draft with all attachments:
     - Photos ZIP
     - Excel WI file
     - Job report PDF
4. Create a final review task in Stage 7

**Connects to:**
- Stage 6 Operative Job Sheet (reads `Ready To Hand over`, `QS Reviewed`)
- Stage 6 `RR_work_instruction_template_s6` (reads WI data for the job)
- EasyBOP (downloads photos, generates documents)
- Email system (creates draft for review before sending)

---

## How Stage 7 Connects to Other Stages

```
Stage 6 [job status check]
  → sets "Ready To Hand over" = Yes on Operative Job Sheet
  → sets Job Status = Completed

Stage 7 [handover_task]
  → creates Shannon's handover task in EasyBOP

Stage 7 [variation & task]
  → submits variation (additional SOR costs) to EasyBOP
  → creates QS review task in EasyBOP

Stage 7 [export_doc_pack]  ← triggers after QS Reviewed = Yes
  → exports photos zip + Excel + job report PDF
  → creates email draft for sending to client

Stage 7 [post handover]
  → any post-handover cleanup / final steps
```

---

## Shared Google Sheets

| Sheet | Used for |
|-------|---------|
| **Operative Job Sheet** | Read job status, write task/variation created dates |
| **OJS BOQ & SMV** | Read BOQ/SOR lines for variation building |
| **RR_work_instruction_template_s6** | Read WI data for doc pack export |
| **SOR Code Mappings** | Lookup SOR code descriptions for AI scoring |

---

## API Endpoints Used in Stage 7

| Endpoint | Purpose |
|----------|---------|
| `POST /resolve-works-id` | Get `works_id` for a CORF |
| `POST /create-handover-task` | Create handover task in EasyBOP |
| `POST /stage7_add_variation_post_And_create_tasks` | Submit variation + create QS task |
| `POST /stage7_export_job_documents_post` | Generate job report PDF + Excel pack |

---

## Open Questions / Review Items (from workflow sticky notes)

- **Variation & Task**: How many days to send the variation for? What should the email subject be?
- **Export Doc Pack**: Review what the email body should say before going live

---

## Notes / Gotchas

- The variation workflow uses **AI scoring** — if SOR descriptions are vague, the AI may miss or incorrectly include variation lines. Review the threshold.
- `handover_task.json` is currently on a **manual trigger** — confirm whether it should run on a schedule in production
- Doc pack export creates **draft emails** (not sent) — a human reviews and sends them
- `Operative Job Sheet` columns `Variation Created` and `QS Review Task Created` must exist — add them if missing (noted in workflow sticky)
- Google Sheets `QS Reviewed` column uses lowercase `yes` in data but the filter needs case-insensitive matching (handled in code node)
