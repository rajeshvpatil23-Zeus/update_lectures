# LectureUpdate — Automation Scripts

Playwright-based bulk update scripts for lecture and cohort management.

---

## Setup (run once per terminal session)

```bash
cd /Users/inno/Projects/lectureUpdate
source .venv/bin/activate
```

---

## Directory Structure

```
lectureUpdate/
├── updateLecture/          Bulk lecture updater (category, module, tags, etc.)
│   ├── update_lecture.py
│   ├── input/              Place input CSV here before running
│   └── logs/               Run logs + result CSVs saved here
│       └── archive/        Copy of input CSV kept per run
│
├── updateTitles/           Bulk lecture title updater
│   ├── update_title.py
│   ├── input/
│   └── logs/
│       └── archive/
│
├── updateMasaiCohorts/     Masai cohort settings updater
│   ├── update_cohort.py
│   ├── input/
│   ├── logs/
│   │   └── archive/
│   └── browser_profile/    Created automatically on first login
│
└── updatePrepleafCohorts/  Prepleaf (iHub) cohort settings updater
    ├── update_cohort.py
    ├── input/
    ├── logs/
    │   └── archive/
    └── browser_profile/    Created automatically on first login
```

---

## 1. updateLecture — Bulk lecture updater

Updates category, module, tags, mandatory flag, and show-feedback toggle.

**Required CSV columns:**
| Column | Description |
|--------|-------------|
| `lecture_url` | Full URL of the lecture edit page |
| `updated_category` | New category value |
| `updated_module` | New module value |
| `updated_tags` | Comma-separated tags |
| `updated_mandatory` | `TRUE` or `FALSE` |
| `updated_show_feedback` | `TRUE` or `FALSE` |

**Run:**
```bash
cd updateLecture
python update_lecture.py
```

**How it works:**
- Auto-selects the CSV if only one is in `input/`, otherwise prompts you to pick
- Logs into experience-admin.masaischool.com automatically (falls back to manual login if credentials change)
- For each lecture: reads current DOM values, skips fields already correct, updates the rest
- Verifies all fields after updating (retries once on mismatch)
- Saves a timestamped `.log` and `.csv` to `logs/`
- Summary at the end lists any failed/error lecture IDs

---

## 2. updateTitles — Bulk title updater

Updates the title of lectures.

**Required CSV columns:**
| Column | Description |
|--------|-------------|
| `lecture_url` | Full URL of the lecture edit page |
| `updated_title` | New title text |

**Run:**
```bash
cd updateTitles
python update_title.py
```

**How it works:**
- Auto-selects the CSV if only one is in `input/`
- Reads current title from DOM, skips if already correct
- Verifies the title after setting it
- Summary at the end lists any failed/error lecture IDs

---

## 3. updateMasaiCohorts — Masai cohort updater

Updates cohort settings on [admissions-admin.masaischool.com](https://admissions-admin.masaischool.com).

**Required CSV columns** (`cohort_id` required; all others optional — leave blank to skip):
| Column | Description |
|--------|-------------|
| `cohort_id` | Numeric cohort ID |
| `batch_id` | Batch ID text |
| `hall_ticket_prefix` | Hall ticket prefix |
| `student_prefix` | Student prefix |
| `foundation_starts` | Date — any standard format (DD/MM/YYYY, YYYY-MM-DD, etc.) |
| `batch_start_date` | Date — same formats as above |
| `lms_batch_id` | LMS batch name to search & select |
| `lms_section_ids` | Comma-separated section names (replaces existing) |
| `manager_id` | Manager ID |
| `enable_kit` | `TRUE` or `FALSE` |
| `disable_welcome_kit_tshirt` | `TRUE` or `FALSE` |

**Run:**
```bash
cd updateMasaiCohorts
python update_cohort.py
```

**Resume from a specific cohort** (if a previous run was interrupted):
```bash
python update_cohort.py --start-cohort 2007
```

**How it works:**
- Opens a visible Chrome window for login check (OTP if session expired)
- After login confirmed, switches to headless browser for the bulk updates
- Saves session to `browser_profile/` — subsequent runs skip the OTP
- Summary at the end lists any failed/error cohort IDs with the specific fields that failed

**First run:** `browser_profile/` is empty — a browser window will open for OTP login. Complete the login, then press ENTER in the terminal.

---

## 4. updatePrepleafCohorts — Prepleaf cohort updater

Updates cohort settings on [dashboard-admin.prepleaf.com](https://dashboard-admin.prepleaf.com).

**CSV columns:** Same as Masai cohorts above.

**Run:**
```bash
cd updatePrepleafCohorts
python update_cohort.py
```

**Resume from a specific cohort:**
```bash
python update_cohort.py --start-cohort 53
```

**First run:** Same OTP login flow as Masai — a browser window opens, complete the login, press ENTER.

---

## Output files

Each run produces two files in `logs/`:

| File | Description |
|------|-------------|
| `run_<name>_<timestamp>.log` | Full timestamped terminal output |
| `run_<name>_<timestamp>.csv` | Per-item result: CHANGED / SKIPPED / FAILED / ERROR per field |

Input CSVs are automatically archived to `logs/archive/` after each run.

---

## Fixing failures

At the end of every run the summary lists failed IDs:

```
  Cohorts with failures/errors: 3/116

  ── Failed / Error cohort IDs ─────────────────────────
    [2101]  {'lms_batch_id': 'FAILED', 'lms_section_ids': 'FAILED'}
    [2094]  {'hall_ticket_prefix': 'FAILED'}
    [2096]  {'foundation_starts': 'FAILED'}  — cannot parse date 'Recordings'
  ─────────────────────────────────────────────────────
```

To re-run only the failed cohorts: create a new CSV with just those rows and run again (or use `--start-cohort`).

Common causes:
- **Timeout failures** — transient network/site slowness; safe to re-run
- **Date parse errors** — wrong value in the CSV (e.g. text instead of a date); fix the CSV cell
- **Category verify fail** — site dropdown value differs slightly from CSV; check the exact label on the site
