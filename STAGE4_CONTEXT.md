# Stage 4 — Full Project Context & Handoff Document

> **Last Updated:** 2026-08-12
> **Author:** Ahmed Ali / Antigravity AI Assistant
> **Purpose:** Complete reference for continuing Stage 4 development from any machine.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture & Infrastructure](#2-architecture--infrastructure)
3. [Server Setup & Deployment](#3-server-setup--deployment)
4. [Stage 4 Codebase Map](#4-stage-4-codebase-map)
5. [API Endpoints — Complete Reference](#5-api-endpoints--complete-reference)
6. [End-to-End Stage 4 Pipeline Flow](#6-end-to-end-stage-4-pipeline-flow)
7. [n8n Workflow Architecture](#7-n8n-workflow-architecture)
8. [Voice Agent (VAPI Michael) — Full Specification](#8-voice-agent-vapi-michael--full-specification)
9. [Key Implementation Details & Edge Cases](#9-key-implementation-details--edge-cases)
10. [Client Requirements (from Mark Bence Meeting)](#10-client-requirements-from-mark-bence-meeting)
11. [Current Progress & What's Left](#11-current-progress--whats-left)
12. [Known Bugs & Gotchas](#12-known-bugs--gotchas)
13. [Environment Variables & Config](#13-environment-variables--config)

---

## 1. Project Overview

### What This System Does
Jeffway Housing Maintenance manages reactive repair works orders for Birmingham City Council (BCC). This automation system handles the **full lifecycle** of a works order — from email receipt to job completion handover.

### Stage 4 Specifically
Stage 4 handles the **pre-works appointment scheduling pipeline**:
1. **Identifies qualifying jobs** from EasyBOP that need appointments created
2. **Extracts JIS (Job Information Sheet)** data to determine how many appointments are needed, what trades, and how many hours each
3. **Creates appointments** on EasyBOP's scheduling board ("Jobs to Allocate")
4. **Monitors scheduling** — checks if Jeffway's admin team has dragged appointments onto the scheduling board
5. **Verifies scheduled appointments** — confirms which appointments are on the board and extracts operative, date, time details
6. **Voice calls tenants** via VAPI AI agent "Michael" to confirm appointment dates/times
7. **Reports outcomes** — generates Excel reports for Jeffway's team

### The Bigger Pipeline
```
Stage 1 (WI+BOQ Import) → Stage 2 (Pre-Works .msg/asbestos) → Stage 3 (Asbestos Monitor)
    → Stage 4 (THIS — JIS + Appointments + Voice Calls)
    → Stage 5 (Stalled Appointment Check) → Stage 6 (OJS + Job Monitoring) → Stage 7 (Handover)
```

---

## 2. Architecture & Infrastructure

### Production Server
- **IP:** `173.212.233.153` (Contabo VPS, `vmi2875246`)
- **OS:** Ubuntu Linux
- **Python Path:** `/root/projects/jeffway/filler/.venv/bin/python`
- **Working Dir:** `/root/projects/jeffway/filler/`
- **API Port:** `8000` (production) / `8010` (local dev)
- **Swagger UI:** `http://173.212.233.153:8000/docs`

### Tech Stack
| Component | Technology |
|-----------|-----------|
| API Server | FastAPI + Uvicorn |
| Browser Automation | Playwright (Chromium, headless on server) |
| Workflow Orchestration | n8n (self-hosted) |
| Voice Agent | VAPI.ai (outbound calls) |
| Data Storage | Google Sheets (tracking/staging) |
| Target System | EasyBOP (web-based housing management) |
| AI | OpenAI / Claude (JIS generation, job assessment) |

### EasyBOP Key IDs
| ID | Value | Purpose |
|----|-------|---------|
| `contract_id` | `321129` | BCC Reactive Repairs contract |
| `item_id` | `27059` | JIS pre-works UDF column (`wi_pw`) |
| `apt_board_id` | `1598` | Scheduling Register board for appointments |

---

## 3. Server Setup & Deployment

### Systemd Service
The API runs as a systemd service on the production server:
```bash
# Service file: /etc/systemd/system/jeffway-api.service
# Working directory: /root/projects/jeffway/filler
# Entry point: python main.py

# Restart commands:
sudo /root/projects/jeffway/deploy/jeffway-api.sh restart
sudo systemctl restart jeffway-api
sudo systemctl status jeffway-api

# View logs:
sudo journalctl -u jeffway-api -f
sudo journalctl -u jeffway-api --since "10 minutes ago"
```

### Local Development
```bash
cd filler/
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
playwright install chromium
python main.py
# → http://localhost:8010/docs
```

### Deploying Code Changes
1. Edit files locally
2. Push to GitHub
3. SSH into server: `ssh root@173.212.233.153`
4. `cd /root/projects/jeffway && git pull`
5. `sudo /root/projects/jeffway/deploy/jeffway-api.sh restart`

Or manually copy files:
```bash
scp Stage4/pre_works_jis.py root@173.212.233.153:/root/projects/jeffway/Stage4/
scp Stage4/api.py root@173.212.233.153:/root/projects/jeffway/Stage4/
sudo systemctl restart jeffway-api
```

---

## 4. Stage 4 Codebase Map

### File Structure
```
jeffway-easybop-Jeffway-server-clean/
├── .env                          # Environment variables (credentials, config)
├── .gitignore                    # Git ignore rules
├── PIPELINE_OVERVIEW.md          # Master pipeline overview (all stages)
├── STAGE4_CONTEXT.md             # THIS FILE — full Stage 4 reference
├── meeting_with_mark_stage4.txt  # Meeting transcript with client (July 30, 2026)
│
├── Stage4/
│   ├── api.py                    # FastAPI router (460 lines) — all Stage 4 endpoints
│   └── pre_works_jis.py          # Core automation logic (2720 lines) — all Playwright functions
│
├── filler/
│   ├── main.py                   # Main API server entry point (1446 lines)
│   ├── config.py                 # Settings from .env
│   ├── automation.py             # EasyBOP login helper
│   ├── ip_allowlist.py           # IP-based access control middleware
│   ├── requirements.txt          # Python dependencies
│   ├── easybop_session.json      # Saved Playwright session state
│   └── ... (other stage modules)
│
├── deploy/
│   ├── jeffway-api.service       # Systemd service definition
│   ├── jeffway-api.sh            # Restart/status helper script
│   └── lock-api-port.sh          # Firewall port lock script
│
└── (stage1/, stage2/, stage3/, Stage5/, Stage6/, stage7/, etc.)
```

### Key Files in Detail

#### `Stage4/pre_works_jis.py` (2720 lines)
The core Playwright automation module. Contains all `async def run_*` functions:

| Function | Line | Purpose |
|----------|------|---------|
| `_format_appointment_period()` | 33 | Convert raw duration (3, "03:00", etc.) → HH:MM for `<select>` |
| `_parse_date_received()` | 89 | Parse date strings from EasyBOP grid |
| `_normalize_corf_list()` | 105 | Unwrap n8n-style `{json: {corf: "..."}}` objects |
| `_extract_date_from_comment()` | 144 | Regex date extraction from task comments |
| `_navigate()` | 254 | Navigate Playwright page with timeout |
| `_page_is_login()` | 266 | Detect if we're on EasyBOP login page |
| `_ensure_authenticated()` | 280 | Auto re-login if session expired |
| `_parse_api_payload()` | 439 | Parse Propeller SmartForms API JSON |
| `_fetch_smart_form_via_api()` | 490 | Intercept SmartForm API calls |
| `run_create_contractor_task()` | 609 | Create a task for Shannon Slade |
| `run_pre_works_jis()` | 686 | **MAIN** — fetch qualifying jobs + extract JIS |
| `run_check_completed_tasks()` | 1217 | Scrape completed tasks from Tasks Issued By Me |
| `run_process_completed_task()` | 1352 | Update notes + UDFs after task completion |
| `run_book_appointment()` | 1482 | Create unallocated appointment on EasyBOP |
| `run_get_staff_availability()` | 1813 | Extract staff timeline from FullCalendar |
| `run_get_unallocated_jobs()` | 2129 | Scrape "Jobs to Allocate" panel |
| `run_verify_scheduled_appointment()` | 2350 | Search scheduling board + verify appointment |

#### `Stage4/api.py` (460 lines)
FastAPI router that wraps each `run_*` function into an HTTP endpoint.

#### `filler/main.py` (1446 lines)
The main FastAPI application. Manages:
- Playwright browser lifecycle (launch, session restore)
- IP allowlist middleware
- Mounts all stage routers (including Stage 4)
- All other endpoint definitions for stages 1-7

---

## 5. API Endpoints — Complete Reference

All Stage 4 endpoints are prefixed with `/stage4/`.

### `POST /stage4/qualifying-jobs`
**Purpose:** Fetch qualifying pre-works jobs that need appointments created.

**Request:**
```json
{
  "contract_id": "321129",
  "item_id": "27059",
  "existing_corfs": [{"json": {"corf": "12345"}}],
  "keep_page_open": false
}
```

**Logic:**
1. Navigates to pre-works report page
2. Filters jobs with status "Pre-completion Items To Complete" AND green JIS cell
3. For each qualifying job:
   - Clicks the JIS "Click on me" link → opens dialog
   - Extracts `smart_form_id` → navigates to SmartForm
   - Intercepts Propeller API to extract appointment sections
   - Classifies each appointment as `"subcontractor"` or `"in-house"`
4. Returns list of jobs with appointments

**Key behaviors:**
- Limits to first 10 qualifying jobs
- Sorts by `date_received` ascending (oldest first)
- Assigns sequential appointment numbers: "Appointment 1", "Appointment 2", etc.
- Splits multi-day appointments >8 hours into daily chunks (e.g., 15 hrs → Day 1: 8h, Day 2: 7h)
- Caps total hours >40 to 40 hours (5 workdays max)
- `existing_corfs` filters out already-processed jobs
- Subcontractor trades: Roofing, Scaffolding, Scaffolder, Asbestos Removal Subcontract, Asbestos Removal, or contains "Subcontract - book separately"

**Response:**
```json
{
  "success": true,
  "qualifying_jobs": [
    {
      "works_id": "906702",
      "corf": "RR-12345",
      "address": "1 Failand Crescent (KS1985)",
      "date_received": "01/08/2026",
      "tenant_mobile": "07123456789",
      "appointments": [
        {
          "appointment_number": "Appointment 1 (Day 1 of 5)",
          "trade": "PLASTERER",
          "hours": 8,
          "description": "Plaster and skim 3 bedrooms...",
          "classification": "in-house",
          "unique_key": "906702_Appointment_1_Day_1_of_5"
        }
      ]
    }
  ]
}
```

---

### `POST /stage4/book-appointment`
**Purpose:** Create an unallocated appointment on EasyBOP (appears in "Jobs to Allocate").

**Request:**
```json
{
  "works_id": "906702",
  "contract_id": "321129",
  "reason": "[Day 1 of 5 — 8 Hours] Plaster and skim 3 bedrooms...",
  "duration_hours": 8,
  "note": "Appointment 1 (Day 1 of 5)\n\nTrade: PLASTERER\nDuration: 8 hours",
  "trade": "PLASTERER"
}
```

**Logic:**
1. Navigates to job's process monitor tab
2. Clicks "Add Other Appointment"
3. Fills reason (truncated to 120 chars for title field)
4. Selects duration from `#aptj_time_allowed_override` dropdown (00:00 to 24:00 in 15-min increments)
5. Fills note into `#apt_event_note`
6. Selects workboard "Appointments"
7. Saves

**Key behaviors:**
- Uses `asyncio.Lock()` to serialize parallel requests (prevents DOM race conditions)
- Handles native browser dialog `accept()` events
- Defaults to "01:00" if duration is blank/N/A
- `duration_hours` accepts: `3`, `3.0`, `"3"`, `"03:00"`, `"2.5"`, `"2 hours"`

---

### `POST /stage4/create-contractor-task`
**Purpose:** Create a High priority task for Shannon Slade under "Book work for contractor" category.

**Request:**
```json
{
  "works_id": "906702",
  "contract_id": "321129"
}
```

---

### `POST /stage4/check-completed-tasks`
**Purpose:** Scrape completed tasks from "Tasks Issued By Me" page.

**Request:**
```json
{
  "category": "Book work for contractor"
}
```

**Response:** Array of completed task objects with `works_id`, comment text, completed date.

---

### `POST /stage4/process-completed-task`
**Purpose:** After Shannon completes a task, update the job's internal notes and UDFs.

**Request:**
```json
{
  "works_id": "906702",
  "contract_id": "321129",
  "comment": "Booked appointment for 17/07 with Clayton"
}
```

**Logic:**
1. Appends comment to `#internal_notes`
2. Parses date from comment (regex: supports `17/07`, `17.07`, `July 17`, etc.)
3. Fills date into UDF calendar input `#udf_pw_works_5088`
4. Selects "Appointment booked" in multi-select `#udf_pw_works_5090`
5. Saves

---

### `POST /stage4/staff-availability`
**Purpose:** Get staff availability from the Scheduling Register's FullCalendar timeline.

**Request:**
```json
{
  "contract_id": "321129",
  "target_date": "12/08/2026",
  "staff_names": ["Clayton Pantan", "Chris Pantan"],
  "required_hours": 2,
  "days_count": 5
}
```

**Logic:**
1. Navigates to scheduling board
2. For each day (5 working days, skipping weekends):
   - Navigates FullCalendar to that date
   - For each staff member row:
     - Extracts all `.fc_event` blocks with pixel positions
     - Converts pixel positions to times (2.4 px/min scale, 1152.4px = 08:00 AM)
     - Calculates busy slots and free windows

**Response:** Per-staff, per-day busy slots + free time windows.

---

### `POST /stage4/unallocated-jobs`
**Purpose:** Scrape all appointments sitting in the "Jobs to Allocate" side panel.

**Request:**
```json
{
  "contract_id": "321129",
  "apt_board_id": "1598"
}
```

**Logic:**
1. Navigates to scheduling board
2. Finds all `.fc_event` cards in the unallocated panel
3. For each card:
   - Right-clicks → "Display details"
   - Waits for `#apt_notes_list` jqGrid table
   - Extracts note content from `td[aria-describedby*="n.notes"]`
   - Parses full `appointment_number` (e.g., "Appointment 1 (Day 1 of 5)")
   - Extracts `trade`
   - Closes dialog, purges overlay

**Response:**
```json
{
  "success": true,
  "unallocated_items": [
    {
      "works_id": "906702",
      "apt_event_id": "340185",
      "duration": "08:00",
      "appointment_number": "Appointment 1 (Day 1 of 5)",
      "trade": "PLASTERER",
      "address": "1 Failand Crescent",
      "post_code": "KS1985"
    }
  ]
}
```

---

### `POST /stage4/verify-scheduled-appointment`
**Purpose:** Check if a specific appointment has been scheduled on the board by Jeffway's admin.

**Request:**
```json
{
  "contract_id": "321129",
  "apt_board_id": "1598",
  "address": "1 Failand Crescent (KS1985)",
  "appointment_number": "Appointment 1 (Day 1 of 5)"
}
```

**Logic:**
1. Opens Search dialog on scheduling board
2. Fills address into `#dlg_sch_list_search`
3. Searches `#list_apt_schedule` grid
4. For each matching row:
   - Double-clicks to open detail modal
   - Inspects `#apt_notes_list` for note content
   - Extracts appointment number + day chunk from note
   - **Multi-day awareness:** If target is "Appointment 1 (Day 2 of 5)", it only matches rows whose note contains BOTH "Appointment 1" AND "Day 2"
5. Returns match details

**Response (when found):**
```json
{
  "success": true,
  "scheduled": true,
  "works_id": "906702",
  "apt_event_id": "340193",
  "appointment_number": "Appointment 1 (Day 1 of 5)",
  "trade": "PLASTERER",
  "address": "1 Failand Crescent",
  "operative_name": "Clayton Pantan",
  "appointment_date": "11/08/2026 08:15",
  "scheduled_duration": "08:00",
  "resident_mobile": "07123456789",
  "resident_email": "tenant@email.com",
  "appointment_status": "Appointment Booked - Staff Allocated"
}
```

**Multi-day matching rules:**
1. If target has a Day (e.g., "Day 2 of 5"): Both appointment digit AND day digit must match
2. If target has no Day (e.g., "Appointment 3"): Matches if appointment digit matches AND note has no day OR note day is "1"
3. Fallback: Direct substring match

---

## 6. End-to-End Stage 4 Pipeline Flow

### Phase 1: Appointment Creation (Every 2 Hours)
```
n8n Cron (2h) → POST /stage4/qualifying-jobs → Filter new jobs
    → For each job: POST /stage4/book-appointment (for each appointment)
    → Write to Google Sheet "Stage 4 Tracking"
    → POST /stage4/create-contractor-task (notify Shannon)
```

### Phase 2: Scheduling Verification (Every 2 Hours)
```
n8n Cron (2h) → Read Google Sheet "Stage 4 Tracking"
    → POST /stage4/unallocated-jobs (check what's still unallocated)
    → POST /stage4/verify-scheduled-appointment (for each appointment)
    → Update Sheet: scheduled=Yes/No, operative_name, date, duration
```

### Phase 3: Voice Confirmation Calls (Every 2 Hours)
```
n8n Cron (2h) → Read Sheet → Filter: ALL appointments for a works_id = scheduled "Yes"
    → POST /stage4/staff-availability (get operative free slots for rescheduling)
    → Format Vapi context → POST https://api.vapi.ai/call
    → Vapi webhook on call end → Parse structured output
    → Update Sheet: confirmed/rescheduled per appointment
```

### Phase 4: Daily Reports (6 AM)
```
n8n Cron (6 AM) → Read Sheet
    → Report 1: "Appointments Created" (what AI created yesterday, not yet booked)
    → Report 2: "Appointment Confirmation" (outcomes of Michael's calls)
    → Send as Excel attachments to Chris + team
```

---

## 7. n8n Workflow Architecture

### Workflow 1: Qualifying Jobs + Appointment Creation
```
Schedule Trigger (2h)
  → Google Sheets: Get all existing CORFs from Stage 4 sheet
  → HTTP Request: POST /stage4/qualifying-jobs (with existing_corfs)
  → IF: qualifying_jobs.length > 0
    → Loop: For each job
      → Loop: For each appointment in job
        → HTTP Request: POST /stage4/book-appointment
        → Google Sheets: Append row (works_id, address, appointment_number, trade, duration, scheduled=No)
      → HTTP Request: POST /stage4/create-contractor-task
```

### Workflow 2: Scheduling Verification
```
Schedule Trigger (2h)
  → Google Sheets: Get rows where scheduled != "Yes"
  → Loop: For each row
    → HTTP Request: POST /stage4/verify-scheduled-appointment
    → IF scheduled == true:
      → Google Sheets: Update row (scheduled=Yes, operative_name, date, duration, status)
```

### Workflow 3: Voice Confirmation (Michael Calls)
```
Schedule Trigger (2h)
  → Google Sheets: Get ALL rows
  → Code Node: Group by works_id, filter where ALL appointments = scheduled "Yes"
  → Loop: For each ready job
    → HTTP Request: POST /stage4/staff-availability (for booked operatives)
    → Set Node: Build vapi_context string
    → HTTP Request: POST https://api.vapi.ai/call
  → Webhook (on call end): Parse Vapi structured output
    → Loop: For each appointment outcome
      → Google Sheets: Update (confirmed/rescheduled, agreed_date, agreed_slot)
```

### n8n Job Consolidation Code (for Workflow 3)
```javascript
const rows = $input.all().map(item => item.json);
const jobsMap = {};

for (const row of rows) {
    const worksId = String(row.works_id || '').trim();
    if (!worksId) continue;

    if (!jobsMap[worksId]) {
        jobsMap[worksId] = {
            works_id: worksId,
            address: row.address || '',
            post_code: row.post_code || '',
            resident_name: row.resident_name || 'Tenant',
            resident_mobile: row.resident_mobile || '',
            appointments: [],
            operatives: new Set(),
            all_scheduled: true
        };
    }

    const isScheduled = String(row.scheduled || '').toLowerCase();
    if (isScheduled !== 'yes' && isScheduled !== 'true') {
        jobsMap[worksId].all_scheduled = false;
    }

    const operativeName = row.operative_name || '';
    if (operativeName && operativeName !== '-') {
        jobsMap[worksId].operatives.add(operativeName);
    }

    jobsMap[worksId].appointments.push({
        appointment_number: row.appointment_number || '',
        trade: row.trade || '',
        scheduled_date: row.appointment_date || '',
        scheduled_duration: row.scheduled_duration || '08:00',
        operative_name: operativeName,
        status: row.appointment_status || ''
    });
}

const readyJobs = Object.values(jobsMap)
    .filter(job => job.all_scheduled && job.appointments.length > 0 && job.resident_mobile)
    .map(job => ({ ...job, operatives: Array.from(job.operatives) }));

return readyJobs.map(job => ({ json: job }));
```

---

## 8. Voice Agent (VAPI Michael) — Full Specification

### Agent Identity
- **Name:** Michael
- **Role:** Scheduling coordinator for Jeffway Housing Maintenance
- **Call Type:** Outbound confirmation calls to tenants

### Call Purpose
Confirm pre-booked maintenance appointment dates/times with tenants. Walk through each appointment one by one.

### Slot Definitions (per client email)
Jeffway operatives work **08:00 to 16:00 Monday–Friday**:
- **Morning Slot:** 08:00 to 12:00
- **Afternoon Slot:** 12:00 to 16:00
- **All-Day Slot:** 08:00 to 16:00 (for 8-hour jobs)

### Full System Prompt
```
[Identity]
You are Michael, a friendly, courteous, and highly professional scheduling coordinator
calling from Jeffway Housing Maintenance. You are calling tenants to confirm and align
pre-booked maintenance appointments at their homes.

[Tone & Style]
- Warm, polite, reassuring, and unhurried.
- Speak in natural, concise sentences (1 to 2 sentences at a time).
- Never sound robotic or scripted.
- Never read internal technical terms, SOR codes, or job IDs.

[Call Objective]
Walk the tenant through each scheduled appointment one by one, clearly stating the booked
date, time, and expected duration, and confirm whether they will be home and available.
If they cannot make the pre-set slot, negotiate a convenient alternative matching the
assigned operative's availability.

[Standard Slot Definitions]
1. Morning Slot: 08:00 to 12:00
2. Afternoon Slot: 12:00 to 16:00
3. All-Day Slot: 08:00 to 16:00 (for full-day / 8-hour appointments)

[Conversation Flow]
Step 1: Greet + verify tenant/address
Step 2: Go through appointments ONE at a time
  - State: appointment label, trade, date, time, duration
  - Wait for reply
  - If confirmed → move to next
  - If can't make it → offer operative's available alternative slots
Step 3: For multi-day appointments, explain work takes multiple days
Step 4: Final summary of all confirmed dates + "we'll text you the day before"

[Error Handling]
- Outside hours → "Our teams work 8 AM to 4 PM weekdays"
- Confused → restate with 2 clear options
- Voicemail → leave brief message

# CONTEXT FOR THIS CALL:
{{vapi_context}}
```

### Vapi Structured Output Schema
```json
{
  "name": "tenant_call_outcome",
  "type": "object",
  "properties": {
    "overall_call_status": {
      "type": "string",
      "enum": ["completed_confirmed", "completed_rescheduled", "partial_rescheduled",
               "no_answer", "voicemail", "refused", "callback_requested"]
    },
    "tenant_notes": { "type": "string" },
    "appointments": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "appointment_number": { "type": "string" },
          "trade": { "type": "string" },
          "original_scheduled_date": { "type": "string" },
          "status": {
            "type": "string",
            "enum": ["confirmed", "rescheduled", "refused", "pending"]
          },
          "agreed_date": { "type": "string" },
          "agreed_slot": {
            "type": "string",
            "enum": ["morning", "afternoon", "all_day", "custom_time"]
          },
          "agreed_start_time": { "type": "string" },
          "reschedule_reason": { "type": "string" }
        },
        "required": ["appointment_number", "status", "agreed_date", "agreed_slot"]
      }
    }
  },
  "required": ["overall_call_status", "appointments"]
}
```

### Vapi Create Call API
```
POST https://api.vapi.ai/call
Authorization: Bearer YOUR_VAPI_API_KEY
Content-Type: application/json

{
  "assistantId": "YOUR_ASSISTANT_ID",
  "phoneNumberId": "YOUR_PHONE_NUMBER_ID",
  "customer": {
    "number": "+447123456789",
    "name": "John Smith"
  },
  "assistantOverrides": {
    "variableValues": {
      "vapi_context": "...formatted context string..."
    }
  },
  "metadata": {
    "works_id": "906702",
    "address": "1 Failand Crescent"
  }
}
```

---

## 9. Key Implementation Details & Edge Cases

### Multi-Day Appointment Handling
- JIS can specify >8 hours for a trade (e.g., 40 hours of plastering)
- System auto-splits: 40 hrs → 5 days × 8 hrs each
- Each day becomes a separate appointment: "Appointment 1 (Day 1 of 5)", "Appointment 1 (Day 2 of 5)", etc.
- Verification must match BOTH the appointment number AND the day number
- The `unallocated-jobs` endpoint preserves the full label including "(Day X of Y)"

### Appointment Duration Handling
- EasyBOP's `#aptj_time_allowed_override` dropdown has options from "00:00" to "24:00" in 15-min increments
- `_format_appointment_period()` converts any input format to HH:MM
- The duration is selected via JavaScript `<option>` matching with jQuery `.val().trigger('change')`

### Session Management
- EasyBOP sessions expire periodically
- `_ensure_authenticated()` checks if page shows login form
- If expired, automatically re-logs in using saved credentials
- Session state is persisted to `easybop_session.json`
- Mid-loop recovery: if session drops while processing jobs, it re-authenticates and resumes

### Book Appointment Lock
- `_book_appointment_lock = asyncio.Lock()` in `api.py`
- Serializes all `/book-appointment` requests
- Prevents DOM race conditions when n8n sends parallel requests for the same job

### Search Dialog Edge Cases (verify-scheduled-appointment)
- First attempt to open Search may fail (FullCalendar not ready)
- Uses 3-attempt retry loop with Playwright click + JS fallback
- Waits for `#dlg_sch_list_search` to become visible before typing
- After typing address, presses Enter to trigger search
- Closes detail modals without closing the search dialog (so next row iteration works)

### Note Extraction from `#apt_notes_list`
- Notes are stored in a jqGrid table inside detail modals
- Target cell: `td[aria-describedby="apt_notes_list_n.notes"]`
- Falls back to searching all `<td>` cells for text containing "Appointment"
- Ignores cells where text is exactly "Appointment Note" (that's the category name, not content)

### Overlay/Dialog Cleanup
- EasyBOP uses jQuery UI dialogs with `.ui-widget-overlay` backdrop elements
- After closing dialogs, overlays must be forcibly removed from DOM
- Otherwise they block all future clicks on the page

---

## 10. Client Requirements (from Mark Bence Meeting — July 30, 2026)

### Key Decisions
1. **Only process jobs WITHOUT appointment dates** — skip jobs that already have `next_appointment_date` set
2. **Two daily reports at 6 AM:**
   - "Appointments Created" — what AI created, not yet booked on scheduling board
   - "Appointment Confirmation" — outcomes of Michael's voice calls
3. **Reports in Excel format**, emailed as attachments
4. **Agent sweeps every 2 hours** for new qualifying jobs
5. **Appointments remain in "Jobs to Allocate"** until admin drags them onto scheduling board
6. **Report should filter out already-scheduled appointments** — only show what's remaining
7. **Flag ordering issues** — if Appointment 2 is booked before Appointment 1, flag it
8. **Shannon Slade** gets a task for each job needing contractor booking

### Client Email on Voice Agent Behavior (August 2026)
> We would prefer that the agent align the install days with the tenant for each appointment.
> The install days are between 8 and 4pm each day. Appointments should be given:
> - **Morning slot** (8 to 12)
> - **Afternoon slot** (12 to 4)
> - **All day slot**
> We can then confirm the appointments with a call or text the day before.

### Operative Availability Requirement
Before calling the tenant, the workflow MUST fetch the booked operative's availability using `POST /stage4/staff-availability`. If the tenant can't make the scheduled time, Michael should only offer alternative slots when the operative is actually free.

---

## 11. Current Progress & What's Left

### ✅ Completed
- [x] `POST /stage4/qualifying-jobs` — full JIS extraction + multi-day splitting
- [x] `POST /stage4/book-appointment` — appointment creation with lock + duration handling
- [x] `POST /stage4/create-contractor-task` — Shannon Slade task creation
- [x] `POST /stage4/check-completed-tasks` — completed task scraping
- [x] `POST /stage4/process-completed-task` — notes + UDF update
- [x] `POST /stage4/staff-availability` — FullCalendar timeline extraction
- [x] `POST /stage4/unallocated-jobs` — Jobs to Allocate panel scraping
- [x] `POST /stage4/verify-scheduled-appointment` — multi-day aware scheduling verification
- [x] Multi-day appointment label preservation ("Appointment 1 (Day 1 of 5)")
- [x] Multi-day verification matching (Day number must match)
- [x] Vapi system prompt design
- [x] Vapi structured output schema design
- [x] n8n consolidation code (grouping by works_id)

### 🔲 TODO — Build in n8n
- [ ] **Workflow 1:** Qualifying Jobs + Appointment Creation (2-hour cron)
- [ ] **Workflow 2:** Scheduling Verification (2-hour cron)
- [ ] **Workflow 3:** Voice Confirmation Calls (2-hour cron, after all appointments scheduled)
- [ ] **Workflow 4:** 6 AM Daily Reports (Excel email)

### 🔲 TODO — Vapi Setup
- [ ] Create/update Vapi assistant with new system prompt
- [ ] Configure structured output schema
- [ ] Set up webhook endpoint for call-end events
- [ ] Test with real phone numbers

### 🔲 TODO — Post-Call Actions
- [ ] Parse Vapi webhook payload → update Google Sheet
- [ ] Handle "rescheduled" outcomes → move appointment on EasyBOP scheduling board
- [ ] Handle "refused" / "no_answer" → create follow-up task

### 🔲 TODO — Reports
- [ ] Excel report generation (Appointments Created)
- [ ] Excel report generation (Appointment Confirmation)
- [ ] Email delivery to Chris + team at 6 AM

---

## 12. Known Bugs & Gotchas

### Duration Setting Non-Determinism
**Problem:** When booking multiple appointments rapidly, some appointments end up with 1:00 duration instead of the requested duration (e.g., 8:00, 2:00, 3:00).
**Root Cause:** The `<select>` dropdown option selection via JavaScript sometimes doesn't trigger the jQuery `.change()` event before the form saves.
**Mitigation:** Added `asyncio.Lock()` to serialize requests + added extra `asyncio.sleep()` after option selection + double-verification of selected value.

### Search Dialog Timing
**Problem:** On the scheduling board, clicking the Search button doesn't always open the dialog immediately.
**Root Cause:** FullCalendar's heavy DOM manipulation delays jQuery UI dialog initialization.
**Mitigation:** 3-attempt retry loop with 5-second waits between attempts.

### Overlay Blocking
**Problem:** After closing a detail dialog, `.ui-widget-overlay` elements remain in the DOM and block all clicks.
**Mitigation:** Forcibly remove overlay elements via `document.querySelectorAll('.ui-widget-overlay').forEach(el => el.remove())`.

### EasyBOP Session Expiry
**Problem:** Sessions expire mid-operation, causing navigation to redirect to login page.
**Mitigation:** `_ensure_authenticated()` checks every navigation. Mid-loop recovery re-authenticates and resumes.

---

## 13. Environment Variables & Config

### `.env` File (Development — Local PC)
```env
username=Cybixdemo
password=Cybix1234
headless=false
EASYBOP_KEEP_PAGE_OPEN=true
EASYBOP_SLOW_MO_MS=0
JIS_PRE_ITEM_ITEM_ID=27059
request_timeout_ms=25000
API_ALLOWED_IPS=127.0.0.1,::1,144.126.195.119
API_PORT=8010
```

### `.env` File (Production — Server)
```env
username=Cybixdemo
password=Cybix1234
headless=true
EASYBOP_KEEP_PAGE_OPEN=false
EASYBOP_SLOW_MO_MS=0
JIS_PRE_ITEM_ITEM_ID=27059
request_timeout_ms=25000
API_ALLOWED_IPS=127.0.0.1,144.126.195.119,172.81.133.172,::1
API_PORT=8000
```

### Key Differences (Dev vs Prod)
| Setting | Dev (Local) | Prod (Server) |
|---------|-------------|----------------|
| `headless` | `false` (see browser) | `true` (no GUI) |
| `EASYBOP_KEEP_PAGE_OPEN` | `true` (debug) | `false` (cleanup) |
| `API_PORT` | `8010` | `8000` |
| `API_ALLOWED_IPS` | localhost only | + server IPs |

---

## Quick Reference: How to Resume Work

1. **Clone the repo** on any machine
2. **Install deps:** `cd filler && pip install -r requirements.txt && playwright install chromium`
3. **Set up `.env`** (copy from above, adjust headless/port)
4. **Run locally:** `python main.py`
5. **Test endpoints:** Open `http://localhost:8010/docs`
6. **Read this file** for full context on what's built and what's next
7. **Key files to edit:** `Stage4/pre_works_jis.py` (logic) and `Stage4/api.py` (endpoints)
