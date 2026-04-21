"""
update_cohort.py — Masai cohort management updater
Site: https://admissions-admin.masaischool.com/iit/cohort-management

CSV columns (cohort_id required; all others optional — leave blank to skip):
  cohort_id, batch_id, hall_ticket_prefix, student_prefix,
  foundation_starts, batch_start_date, lms_batch_id, lms_section_ids,
  manager_id, enable_kit, disable_welcome_kit_tshirt

Place input CSV in ./input/ and run:
  python update_cohort.py
  python update_cohort.py --start-cohort 2007
"""

import re
import os
import sys
import glob
import shutil
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

DEFAULT_PLATFORM = "masai"

# ── Directories ────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR   = os.path.join(BASE_DIR, "input")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOGS_DIR, "archive")

for d in (INPUT_DIR, LOGS_DIR, ARCHIVE_DIR):
    os.makedirs(d, exist_ok=True)

# ── Platform config ────────────────────────────────────────────────────────────
PLATFORMS = {
    "masai": {
        "base_url":    "https://admissions-admin.masaischool.com/iit/cohort-management",
        "login_url":   "https://admissions-admin.masaischool.com/",
        "profile_dir": os.path.join(BASE_DIR, "browser_profile"),
    },
    "prepleaf": {
        "base_url":    "https://dashboard-admin.prepleaf.com/iit/cohort-management",
        "login_url":   "https://www.ihubiitrcourses.org/signup",
        "profile_dir": os.path.join(BASE_DIR, "browser_profile"),
    },
}

# Back-compat aliases
BASE_URL    = PLATFORMS[DEFAULT_PLATFORM]["base_url"]
LOGIN_URL   = PLATFORMS[DEFAULT_PLATFORM]["login_url"]
PROFILE_DIR = PLATFORMS[DEFAULT_PLATFORM]["profile_dir"]

# ── Status constants ───────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED  = "FAILED"
ERROR   = "ERROR"

RESULT_FIELDS = [
    "cohort_id", "batch_id", "hall_ticket_prefix", "student_prefix",
    "foundation_starts", "batch_start_date", "lms_batch_id", "lms_section_ids",
    "manager_id", "enable_kit", "disable_welcome_kit_tshirt", "notes",
]
SUMMARY_FIELDS = RESULT_FIELDS[1:-1]


# ── Tee logger ─────────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, filepath):
        self._file    = open(filepath, "w", buffering=1, encoding="utf-8")
        self._stdout  = sys.stdout
        self._pending = ""

    def write(self, data):
        self._stdout.write(data)
        self._pending += data
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"{ts} | {line}\n")

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def close(self):
        if self._pending:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._file.write(f"{ts} | {self._pending}\n")
        self._file.close()

    def __getattr__(self, name):
        return getattr(self._stdout, name)


_tee = None


def _start_log(stem: str):
    global _tee
    path = os.path.join(LOGS_DIR, f"{stem}.log")
    _tee = _Tee(path)
    sys.stdout = _tee
    print(f"Log → {path}")


def _stop_log():
    global _tee
    if _tee:
        sys.stdout = _tee._stdout
        _tee.close()
        _tee = None


# ── Helpers ────────────────────────────────────────────────────────────────────
def is_empty(val) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass
    return str(val).strip() == ""


def to_bool(val):
    if is_empty(val):
        return None
    s = str(val).strip().upper()
    if s in ("TRUE", "YES", "1"):
        return True
    if s in ("FALSE", "NO", "0"):
        return False
    return None


def parse_dt(val: str):
    val = str(val).strip()
    for fmt in (
        "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%d-%m-%Y %H:%M", "%d-%m-%Y",
        "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d %b %Y %H:%M", "%d %b %Y",
        "%d %B %Y %H:%M", "%d %B %Y",
        "%m/%d/%Y %H:%M", "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    try:
        return pd.to_datetime(val, dayfirst=True).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        pass
    return None


def dt_display(val: str) -> str:
    try:
        return datetime.strptime(val.strip(), "%Y-%m-%dT%H:%M").strftime("%d/%m/%Y %H:%M")
    except Exception:
        return val.strip()


def _dismiss_dialog(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass


# ── Tab navigation ─────────────────────────────────────────────────────────────
def _go_to_tab(page, name: str):
    _dismiss_dialog(page)
    btn = page.get_by_role("button", name=name)
    btn.wait_for(state="visible", timeout=15_000)
    btn.first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1_500)


# ── Labeled field (Batch ID, Hall Ticket Prefix, Student Prefix) ───────────────
def _update_labeled_field(page, label_text: str, desired, field_name: str) -> str:
    if is_empty(desired):
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED

    desired = str(desired).strip()
    try:
        section = page.locator("div.p-3").filter(
            has=page.locator("span.text-gray-600", has_text=label_text)
        )
        section.wait_for(state="visible", timeout=6_000)
        pencil = section.locator("button.text-blue-600")
        pencil.wait_for(state="visible", timeout=6_000)
        pencil.click()
        page.wait_for_timeout(600)

        textbox = page.get_by_role("textbox").first
        textbox.wait_for(state="visible", timeout=6_000)
        current = textbox.input_value().strip()

        if current == desired:
            print(f"  {field_name} → SKIP (already '{desired}')")
            try:
                page.get_by_role("button", name="Cancel").click()
            except Exception:
                page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            return SKIPPED

        print(f"  {field_name} → UPDATE '{current}' → '{desired}'")
        textbox.fill(desired)
        page.wait_for_timeout(200)
        page.get_by_role("button", name="Save Changes").click()
        page.wait_for_timeout(800)
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        try:
            page.get_by_role("button", name="Cancel").click()
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        return FAILED


def _update_batch_id(page, desired) -> str:
    return _update_labeled_field(page, "Batch ID", desired, "Batch ID")

def _update_hall_ticket_prefix(page, desired) -> str:
    return _update_labeled_field(page, "Hall Ticket Prefix", desired, "Hall Ticket Prefix")

def _update_student_prefix(page, desired) -> str:
    return _update_labeled_field(page, "Student Prefix", desired, "Student Prefix")


# ── Date fields ────────────────────────────────────────────────────────────────
def _update_date_field(page, row_label: str, desired_csv, field_name: str) -> str:
    blank      = is_empty(desired_csv)
    desired_dt = None if blank else parse_dt(str(desired_csv).strip())

    if not blank and not desired_dt:
        print(f"  {field_name} → FAILED (cannot parse date '{desired_csv}')")
        return FAILED

    try:
        row = page.locator("tr").filter(
            has=page.locator("td", has_text=re.compile(rf"^{re.escape(row_label)}$"))
        )
        row.wait_for(state="visible", timeout=6_000)

        dt_input = row.locator("input[type='datetime-local']")
        dt_input.wait_for(state="visible", timeout=6_000)
        current = dt_input.input_value().strip()

        if blank:
            if not current:
                print(f"  {field_name} → SKIP (already empty)")
                return SKIPPED
            print(f"  {field_name} → CLEAR (was '{dt_display(current)}')")
            row.locator("button[aria-label='Clear date']").wait_for(state="visible", timeout=6_000)
            row.locator("button[aria-label='Clear date']").click()
            page.wait_for_timeout(800)
            return CHANGED

        if current == desired_dt:
            print(f"  {field_name} → SKIP (already '{dt_display(current)}')")
            return SKIPPED

        print(f"  {field_name} → UPDATE "
              f"'{dt_display(current) if current else 'empty'}' → '{dt_display(desired_dt)}'")
        dt_input.evaluate(
            f"el => {{ el.value = '{desired_dt}'; "
            f"el.dispatchEvent(new Event('input', {{bubbles: true}})); "
            f"el.dispatchEvent(new Event('change', {{bubbles: true}})); }}"
        )
        page.wait_for_timeout(800)
        return CHANGED

    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        return FAILED


# ── LMS Settings ──────────────────────────────────────────────────────────────
def _update_lms_settings(page, row) -> dict:
    results = {"lms_batch_id": SKIPPED, "lms_section_ids": SKIPPED, "manager_id": SKIPPED}

    lms_batch    = str(row.get("lms_batch_id",   "")).strip() if not is_empty(row.get("lms_batch_id"))   else ""
    sections_raw = str(row.get("lms_section_ids","")).strip() if not is_empty(row.get("lms_section_ids")) else ""
    sections     = [s.strip() for s in sections_raw.split(",") if s.strip()]
    manager_id   = str(row.get("manager_id",     "")).strip() if not is_empty(row.get("manager_id"))     else ""

    if not lms_batch and not sections and not manager_id:
        print("  LMS Settings → SKIP (all blank in CSV)")
        return results

    if lms_batch:
        print(f"  LMS Batch ID → '{lms_batch}'")
        try:
            # Open dropdown — try the wrapper div first, fall back to any trigger button near "LMS Batch" text
            try:
                page.locator(".lms-batch-dropdown button").first.click()
            except Exception:
                page.locator("div, section").filter(
                    has=page.locator("text=/LMS Batch/i")
                ).get_by_role("button").first.click()
            page.wait_for_timeout(800)

            search = page.get_by_placeholder("Search batches...")
            search.wait_for(state="visible", timeout=8_000)
            search.fill(lms_batch)
            page.wait_for_timeout(1_200)

            candidate = page.get_by_role("button").filter(
                has_text=re.compile(re.escape(lms_batch), re.I)
            ).first
            candidate.wait_for(state="visible", timeout=6_000)
            candidate.click()
            page.wait_for_timeout(800)

            # Verify: search box should be gone (dropdown closed) or the selected label is visible
            selected_visible = page.locator(
                f"text={lms_batch}"
            ).count() > 0
            if selected_visible:
                print(f"    ✓ batch '{lms_batch}' selected")
                results["lms_batch_id"] = CHANGED
            else:
                print(f"    [WARN] batch selection could not be verified — marking FAILED")
                results["lms_batch_id"] = FAILED

        except Exception as e:
            print(f"    [ERROR] LMS Batch ID: {e}")
            results["lms_batch_id"] = FAILED
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(400)

    if sections:
        expected = len(sections)
        print(f"  LMS Section IDs → {expected} section(s): {sections}")
        try:
            removed = 0
            for _ in range(50):
                rm = page.locator("span.bg-green-50 button")
                if rm.count() == 0:
                    break
                rm.first.click()
                page.wait_for_timeout(400)
                removed += 1
            if removed:
                print(f"    Cleared {removed} existing section(s)")
                page.wait_for_timeout(600)
            else:
                print("    No existing sections to clear")

            def _section_dropdown_open() -> bool:
                return page.get_by_placeholder("Search sections...").is_visible()

            def _open_section_dropdown():
                try:
                    page.locator(".lms-section-dropdown button").first.click()
                except Exception:
                    page.locator("div, section").filter(
                        has=page.locator("text=/LMS Section/i")
                    ).get_by_role("button").first.click()
                page.wait_for_timeout(1_000)

            def _chip_count() -> int:
                return page.locator("span.bg-green-50").count()

            def _try_select_section(section: str) -> bool:
                if not _section_dropdown_open():
                    _open_section_dropdown()
                search = page.get_by_placeholder("Search sections...")
                search.wait_for(state="visible", timeout=6_000)
                before = _chip_count()
                search.fill(section)
                page.wait_for_timeout(1_500)

                pattern  = re.compile(re.escape(section), re.I)
                done_pat = re.compile(r"Done \(\d+ selected\)", re.I)

                def _best_candidate(locator):
                    for idx in range(locator.count()):
                        btn = locator.nth(idx)
                        try:
                            txt = btn.inner_text().strip()
                        except Exception:
                            continue
                        if done_pat.search(txt):
                            continue
                        if pattern.search(txt):
                            return btn
                    return None

                scoped    = page.locator(".lms-section-dropdown").get_by_role("button").filter(has_text=pattern)
                candidate = _best_candidate(scoped)
                if candidate is None:
                    candidate = _best_candidate(page.get_by_role("button").filter(has_text=pattern))
                if candidate is None:
                    print(f"      no result found for '{section}'")
                    return False

                candidate.click()
                page.wait_for_timeout(1_500)

                after = _chip_count()
                if after <= before:
                    print(f"      click registered but chip count unchanged ({before} → {after}) — retrying")
                    return False

                if _section_dropdown_open():
                    page.get_by_placeholder("Search sections...").fill("")
                    page.wait_for_timeout(800)
                return True

            _open_section_dropdown()
            ok_sections, fail_sections = [], []

            for i, section in enumerate(sections):
                print(f"    [{i+1}/{expected}] '{section}'")
                succeeded = False
                for attempt in range(1, 4):
                    try:
                        if _try_select_section(section):
                            print(f"      ✓ selected (attempt {attempt})")
                            ok_sections.append(section)
                            succeeded = True
                            break
                        else:
                            print(f"      attempt {attempt}: result not found, retrying…")
                            page.wait_for_timeout(800)
                    except Exception as ex:
                        print(f"      attempt {attempt} error: {ex}")
                        page.wait_for_timeout(800)
                if not succeeded:
                    print(f"      ✗ FAILED after 3 attempts")
                    fail_sections.append(section)

            print(f"    Summary: {len(ok_sections)}/{expected} selected "
                  f"— ok={ok_sections} fail={fail_sections}")

            done_btn = page.get_by_role("button").filter(has_text=re.compile(r"Done \(\d+ selected\)"))
            if done_btn.count() > 0:
                done_btn.first.click()
                page.wait_for_timeout(800)
            else:
                print("    [WARN] 'Done' button not found — changes may not be saved")

            results["lms_section_ids"] = CHANGED if ok_sections else FAILED

        except Exception as e:
            print(f"    [ERROR] LMS Section IDs: {e}")
            results["lms_section_ids"] = FAILED
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(400)

    if manager_id:
        print(f"  Manager ID → '{manager_id}'")
        try:
            mgr = page.get_by_placeholder("Enter manager ID")
            mgr.wait_for(state="visible", timeout=6_000)
            current = mgr.input_value().strip()
            if current == manager_id:
                print(f"    SKIP (already '{manager_id}')")
            else:
                mgr.fill(manager_id)
                mgr.press("Tab")
                page.wait_for_timeout(400)
                results["manager_id"] = CHANGED
        except Exception as e:
            print(f"    [ERROR] Manager ID: {e}")
            results["manager_id"] = FAILED

    # Save — only if the UI actually registered a change (save button appears)
    lms_attempted = any(results[k] == CHANGED for k in ("lms_batch_id", "lms_section_ids", "manager_id"))
    if lms_attempted:
        save_btn = page.locator("button").filter(has_text=re.compile(r"Save LMS", re.I))
        try:
            save_btn.wait_for(state="visible", timeout=3_000)
            save_btn.first.click()
            page.wait_for_timeout(1_500)
            print("  [LMS SAVED]")
        except Exception:
            # Save button not visible — UI detected no actual change; treat as SKIPPED
            print("  [LMS] Save button not present — values already match on page, treating as SKIPPED")
            for k in ("lms_batch_id", "lms_section_ids", "manager_id"):
                if results[k] == CHANGED:
                    results[k] = SKIPPED

    return results


# ── Kit toggles ────────────────────────────────────────────────────────────────
def _update_toggle(page, label_contains: str, desired, field_name: str) -> str:
    desired_bool = to_bool(desired)
    if desired_bool is None:
        print(f"  {field_name} → SKIP (blank in CSV)")
        return SKIPPED
    try:
        row = page.locator("div.p-3").filter(
            has=page.locator("span.text-gray-600", has_text=label_contains)
        )
        row.wait_for(state="visible", timeout=6_000)
        checkbox = row.locator("input[type='checkbox']")
        current  = checkbox.is_checked()
        if current == desired_bool:
            print(f"  {field_name} → SKIP (already {'ON' if desired_bool else 'OFF'})")
            return SKIPPED
        print(f"  {field_name} → UPDATE → {'ON' if desired_bool else 'OFF'}")
        row.locator("[data-part='control']").click()
        page.wait_for_timeout(600)
        if checkbox.is_checked() != desired_bool:
            print(f"    [WARN] Toggle verify failed")
            return FAILED
        return CHANGED
    except Exception as e:
        print(f"  {field_name} → FAILED: {e}")
        return FAILED


# ── Per-cohort processor ───────────────────────────────────────────────────────
def process_cohort(page, row, base_url: str = BASE_URL) -> dict:
    cohort_id = str(row["cohort_id"]).strip()
    s = {k: SKIPPED for k in RESULT_FIELDS}
    s["cohort_id"] = cohort_id
    s["notes"]     = ""

    print(f"  Loading: {base_url}/{cohort_id}")
    page.goto(f"{base_url}/{cohort_id}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3_000)
    _dismiss_dialog(page)

    print("  [Basic Details]")
    _go_to_tab(page, "Basic Details")
    s["batch_id"] = _update_batch_id(page, row.get("batch_id"))

    print("  [Identifiers]")
    _go_to_tab(page, "Identifiers")
    s["hall_ticket_prefix"] = _update_hall_ticket_prefix(page, row.get("hall_ticket_prefix"))
    s["student_prefix"]     = _update_student_prefix(page, row.get("student_prefix"))

    print("  [Dates]")
    _go_to_tab(page, "Dates")
    s["foundation_starts"] = _update_date_field(
        page, "Foundation Starts", row.get("foundation_starts"), "Foundation Starts"
    )
    s["batch_start_date"] = _update_date_field(
        page, "Batch Start Date", row.get("batch_start_date"), "Batch Start Date"
    )

    print("  [Course Onboarding]")
    _go_to_tab(page, "Course Onboarding")
    s.update(_update_lms_settings(page, row))
    s["enable_kit"] = _update_toggle(page, "Enable Kit", row.get("enable_kit"), "Enable Kit")
    s["disable_welcome_kit_tshirt"] = _update_toggle(
        page, "Disable Welcome Kit T-Shirt",
        row.get("disable_welcome_kit_tshirt"), "Disable Welcome Kit T-Shirt"
    )
    return s


# ── Shared internals ───────────────────────────────────────────────────────────
def _launch_context(p, profile_dir: str):
    return p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=False,
        slow_mo=300,
        args=["--start-maximized"],
        no_viewport=True,
    )


def _run_update_loop(page, df: pd.DataFrame, base_url: str) -> list:
    all_results = []
    total = len(df)
    for i, row in df.iterrows():
        cohort_id = str(row.get("cohort_id", "")).strip()
        print(f"{'─'*60}")
        print(f"[{i+1}/{total}] Cohort ID: {cohort_id}")
        try:
            result = process_cohort(page, row, base_url=base_url)
        except Exception as e:
            print(f"  [ERROR] {e}")
            result = {k: ERROR for k in RESULT_FIELDS}
            result["cohort_id"] = cohort_id
            result["notes"]     = str(e)
        all_results.append(result)
        print()
    return all_results


def _write_report(all_results: list, log_stem: str, src_csv: str) -> str:
    csv_out = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(csv_out, index=False)
    print(f"\nCSV report  → {csv_out}")

    archive_name = log_stem[len("run_"):]
    dest = os.path.join(ARCHIVE_DIR, f"{archive_name}.csv")
    shutil.copy2(src_csv, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_results)
    print("\n══ Summary ══════════════════════════════════════════════")
    for col in SUMMARY_FIELDS:
        if col in df_log.columns:
            print(f"  {col:35s}: {df_log[col].value_counts().to_dict()}")

    skip_keys = {"cohort_id", "notes"}
    failed = [s for s in all_results
              if any(v in (FAILED, ERROR) for k, v in s.items() if k not in skip_keys)]
    print(f"\n  Cohorts with failures/errors: {len(failed)}/{len(all_results)}")

    if failed:
        print("\n  ── Failed / Error cohort IDs ─────────────────────────")
        for s in failed:
            cid  = s.get("cohort_id", "?")
            bad  = {k: v for k, v in s.items() if k not in skip_keys and v in (FAILED, ERROR)}
            note = s.get("notes", "")
            line = f"    [{cid}]  {bad}"
            if note:
                line += f"  — {note}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    return csv_out


def _apply_start_cohort(df: pd.DataFrame, start_cohort: str) -> pd.DataFrame | None:
    ids  = df["cohort_id"].astype(str).str.strip()
    mask = ids == str(start_cohort).strip()
    if not mask.any():
        print(f"[ERROR] cohort_id '{start_cohort}' not found in CSV.")
        return None
    df = df[mask.cumsum() >= 1].reset_index(drop=True)
    print(f"Resuming from cohort {start_cohort} — {len(df)} row(s) remaining")
    return df


# ── Login helper ───────────────────────────────────────────────────────────────
def _ensure_logged_in(login_url: str, profile_dir: str):
    print("\n── Step 1 of 2: Login check ─────────────────────────────")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(login_url)
        page.wait_for_load_state("networkidle")

        if (login_url.rstrip("/") in page.url.rstrip("/")
                or "login" in page.url.lower()
                or "signup" in page.url.lower()):
            print("Session expired — please log in with OTP in the browser window.")
            input("Press ENTER once you are on the dashboard... ")
            page.wait_for_load_state("networkidle", timeout=60_000)
            print(f"Logged in. URL: {page.url}")
        else:
            print(f"Session active. URL: {page.url}")

        input("Press ENTER to start updating cohorts... ")
        context.close()
    print("Login confirmed. Opening browser for updates...\n")


# ── Entry point ────────────────────────────────────────────────────────────────
def run(base_url: str, login_url: str, profile_dir: str, start_cohort: str = ""):
    csv_files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.csv")))

    if not csv_files:
        print(f"[ERROR] No CSV files found in {INPUT_DIR}/")
        print("Place your input CSV in the input/ folder and re-run.")
        return

    print(f"Found {len(csv_files)} CSV file(s):")
    for i, f in enumerate(csv_files):
        print(f"  [{i}] {os.path.basename(f)}")

    if len(csv_files) == 1:
        chosen = csv_files[0]
        print(f"Auto-selecting: {os.path.basename(chosen)}")
    else:
        idx = input("\nEnter file number: ").strip()
        try:
            chosen = csv_files[int(idx)]
        except (ValueError, IndexError):
            print("[ERROR] Invalid selection.")
            return

    df = pd.read_csv(chosen, dtype=str)
    if "cohort_id" not in df.columns:
        print("[ERROR] CSV must have a 'cohort_id' column.")
        return

    if start_cohort:
        df = _apply_start_cohort(df, start_cohort)
        if df is None:
            return

    print(f"\nRows to process: {len(df)}")

    _ensure_logged_in(login_url=login_url, profile_dir=profile_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(chosen))[0]
    log_stem  = f"run_{base}_{timestamp}"

    _start_log(log_stem)
    print("── Step 2 of 2: Cohort updates ──────────────────────────")
    print("Starting cohort updates...\n")

    with sync_playwright() as p:
        context     = _launch_context(p, profile_dir)
        page        = context.pages[0] if context.pages else context.new_page()
        all_results = _run_update_loop(page, df, base_url)
        context.close()

    _write_report(all_results, log_stem, chosen)
    _stop_log()


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Masai cohort management updater")
    parser.add_argument("--platform",     choices=["masai", "prepleaf"], default=DEFAULT_PLATFORM)
    parser.add_argument("--base-url",     default=None)
    parser.add_argument("--login-url",    default=None)
    parser.add_argument("--profile-dir",  default=None)
    parser.add_argument("--start-cohort", default="", metavar="COHORT_ID",
                        help="Resume from this cohort_id")
    args = parser.parse_args()

    defaults    = PLATFORMS[args.platform]
    base_url    = args.base_url    or defaults["base_url"]
    login_url   = args.login_url   or defaults["login_url"]
    profile_dir = args.profile_dir or defaults["profile_dir"]

    run(base_url=base_url, login_url=login_url, profile_dir=profile_dir,
        start_cohort=args.start_cohort)
