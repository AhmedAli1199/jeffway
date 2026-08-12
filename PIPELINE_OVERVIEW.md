# Jeffway EasyBOP Automation — Master Pipeline Overview

> A full end-to-end automation for handling BCC Response Repairs works orders, from email receipt through EasyBOP job creation, asbestos management, job monitoring, and final handover/variation submission.

---

## The Full Pipeline at a Glance

```
┌─────────────────────────────────────────────────────────────────────┐
│                    BCC Mailbox (BCCorders@jeffway.co.uk)            │
│                   Works Order emails arrive (RR-South, RR-North)    │
└────────────────────────┬────────────────────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
    ┌─────────────────┐   ┌──────────────────────┐
    │   STAGE 1       │   │   STAGE 2            │
    │ WI + BOQ Import │   │ Pre-Works Pipeline   │
    └────────┬────────┘   └──────────┬───────────┘
             │                       │
             │                       │
             ▼                       │
    ┌─────────────────┐              │
    │  EasyBOP        │              │
    │  WI + BOQ       │              │
    │  imported       │              │
    └────────┬────────┘              │
             │                       ▼
             │            ┌──────────────────────┐
             │            │   STAGE 3            │
             │            │ Asbestos Monitor     │
             │            │ (suspend/unsuspend)  │
             │            └──────────┬───────────┘
             │                       │
             │            (after unsuspend)
             │                       │
             └──────────┬────────────┘
                        ▼
             ┌──────────────────────┐
             │   STAGE 6            │
             │ OJS Extraction +     │
             │ Job Status Check     │
             │ (AI completion)      │
             └──────────┬───────────┘
                        │
              (Ready To Hand over = Yes)
                        │
                        ▼
             ┌──────────────────────┐
             │   STAGE 7            │
             │ Handover Task +      │
             │ Variation Submit +   │
             │ Doc Pack Export      │
             └──────────────────────┘
```

---

## Stage Summary

### Stage 1 — WI + BOQ Import
📁 `stage1/n8n_workflows/`

**What:** Reads BCC mailbox, extracts works order data with AI, builds WI rows and BOQ lines, uploads into EasyBOP every 2 hours.

**Key flows:**
- `1.json` — Master orchestrator (cron every 2h)
- `2.json` — Inbox processor + AI extraction (sub-workflow)

**Outputs:** WI and BOQ imported into EasyBOP for contract `321129`

---

### Stage 2 — Pre-Works Pipeline
📁 `stage2/n8n_workflows/`

**What:** Manages the pre-works side — saves works order `.msg` files and asbestos PDFs to SharePoint, then uploads them into EasyBOP pre-works, and auto-fills JIS forms.

**Key flows (numbered = live):**
- `1.` — Asbestos bypass (daily noon)
- `2.` — Asbestos emails → SharePoint (every 2h)
- `3.` — SharePoint asbestos PDFs → EasyBOP (every 30min)
- `6.` — BCC inbox `.msg` → SharePoint + tracker (every 1h)
- `7.` — SharePoint `.msg` → EasyBOP pre-works (every 1h)
- `4.` + `5.` — JIS auto-fill (AI-driven form fill)

**Outputs:** Each pre-works job has its WO `.msg` + asbestos PDF uploaded in EasyBOP

---

### Stage 3 — Asbestos Monitoring
📁 `stage3/n8n_workflows.json/`

**What:** Tracks jobs missing asbestos reports, suspends them in EasyBOP, writes notes and creates tasks for surveyors, then unsuspends when reports arrive.

**Key flows:**
- `fetch_missingASB_suspend_note_task.json` — Find → suspend → note → task (every 1h)
- `fetch_unsuspend.json` — Detect ready → unsuspend → note (Mon–Fri 9–19h)

**Shared data:** `ASB_Tracking` tab in `SOR-Codes-Template` sheet

**Outputs:** Jobs suspended/unsuspended in EasyBOP, notes + tasks created

---

### Stage 6 — Job Monitoring
📁 `Stage 6/n8n_workflows/`

**What:** Scrapes completed Operative Job Sheets from EasyBOP SmartForms, fetches BOQ/SOR data, runs AI to determine job completion, generates reports.

**Key flows:**
- `ojs_extraction.json` — Scrape OJS → append to tracker (every 2h)
- `BOQ_extraction.json` — Fetch BOQ per job
- `job status check.json` — AI completion assessment (every 1h)
- Various report exports

**Shared data:** `Operative Job Sheet`, `OJS BOQ & SMV` Google Sheets

**Outputs:** `Ready To Hand over = Yes` on completed jobs → feeds Stage 7

---

### Stage 7 — Handover + Variation + Doc Pack
📁 `stage7/n8n_workflows/`

**What:** End-of-job processing — creates handover tasks, submits cost variations, exports document packs (photos zip + job report PDF + WI Excel) and creates email drafts.

**Key flows:**
- `handover_task.json` — Create Shannon's handover task in EasyBOP
- `variation & task.json` — AI-scored variation submission + QS review task
- `export_doc_pack.json` — Export full doc pack when QS Reviewed = Yes

**Outputs:** Handover tasks + variations in EasyBOP, doc pack email drafts ready to send

---

## Shared Resources (used across all stages)

### Google Sheets
| Sheet | Purpose |
|-------|---------|
| `SOR-Codes-Template` | SOR code lookup, BOQ template, WI template, ASB_Tracking |
| `Reactive-Works-Instructions-Template` | Staging sheet for WI import (Stage 1) |
| `Work Order Form` | Works order tracker (Stage 2 WF6/7) |
| `Operative Job Sheet` | Job completion tracker (Stage 6 → Stage 7) |
| `OJS BOQ & SMV` | BOQ/SOR per job (Stage 6 → Stage 7) |
| `RR_work_instruction_template_s6` | Stage 6 WI data for doc pack |

### API Server (FastAPI + Playwright)
- **URL:** `http://173.212.233.153:8000` (or `http://localhost:8000`)
- **Manages:** EasyBOP browser sessions, all Playwright-based operations
- **Key endpoints:** See each stage's CONTEXT.md for the specific endpoints used

### External Services
| Service | Used for |
|---------|---------|
| Microsoft Graph API | BCC mailbox (`BCCorders@jeffway.co.uk`) — read emails |
| SharePoint (`bccvoids` site) | Store `.msg` files + asbestos PDFs |
| Google Sheets API | All tracking/staging sheets |
| OpenAI / Claude AI | AI extraction, JIS auto-fill, variation scoring, job completion |
| Gmail | Daily summary emails (asbestos, reports) |

---

## Data Keys (how stages find the same job across systems)

| Key | Description | Used across |
|-----|-------------|-------------|
| **CORF** (Client Order Reference) | Primary works order ID (e.g. `12345` or `123/456`) | All stages |
| **works_id** | EasyBOP internal job ID | Stage 2, 3, 6, 7 |
| **SmartForm Name** | OJS form identifier | Stage 6, 7 |
| **Contract ID** | `321129` (BCC RR contract) | Stage 1 |

---

## Reading Order for Context Files

1. [`stage1/n8n_workflows/CONTEXT.md`](./stage1/n8n_workflows/CONTEXT.md) — Start here: WI/BOQ import pipeline
2. [`stage2/n8n_workflows/CONTEXT.md`](./stage2/n8n_workflows/CONTEXT.md) — Pre-works: .msg + asbestos + JIS
3. [`stage3/n8n_workflows.json/CONTEXT.md`](./stage3/n8n_workflows.json/CONTEXT.md) — Asbestos suspension management
4. [`Stage 6/n8n_workflows/CONTEXT.md`](./Stage%206/n8n_workflows/CONTEXT.md) — Job monitoring + OJS + AI status
5. [`stage7/n8n_workflows/CONTEXT.md`](./stage7/n8n_workflows/CONTEXT.md) — Handover + variations + doc packs
