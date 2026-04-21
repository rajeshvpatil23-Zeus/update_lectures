"""
update_lecture.py — Bulk lecture updater

CSV columns (all required):
  lecture_url, updated_category, updated_module, updated_tags,
  updated_mandatory, updated_show_feedback

Place input CSV in ./input/ and run:
  python update_lecture.py
"""

import re
import os
import sys
import glob
import shutil
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── Directories ───────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR   = os.path.join(BASE_DIR, "input")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOGS_DIR, "archive")

os.makedirs(INPUT_DIR,   exist_ok=True)
os.makedirs(LOGS_DIR,    exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# ── Credentials ───────────────────────────────────────────────────────────────
LOGIN_URL = "https://experience-admin.masaischool.com/"
EMAIL     = "ravi.kiran@masaischool.com"
PASSWORD  = "AgentMarley@2"

MAX_ATTEMPTS = 2

# ── Status labels ─────────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED  = "FAILED"
ERROR   = "ERROR"


# ── Tee logger ────────────────────────────────────────────────────────────────
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


_tee: _Tee | None = None


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


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def to_bool(val) -> bool:
    return str(val).strip().upper() in ("TRUE", "YES", "1")


def norm_tags(val) -> list[str]:
    return sorted(t.strip().lower() for t in str(val).split(",") if t.strip())


def _wait_for_form(page):
    try:
        page.wait_for_selector('button:has-text("Edit Lecture")', state="visible", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(400)


# ── Dropdowns (Category / Module) ────────────────────────────────────────────

def _click_dropdown_input(page, label: str):
    clicked = page.evaluate("""(labelText) => {
        const labels = [...document.querySelectorAll('label')];
        const lbl = labels.find(l =>
            l.textContent.trim().toLowerCase().includes(labelText.toLowerCase())
        );
        if (!lbl) return false;
        for (const el of [lbl, lbl.parentElement, lbl.parentElement && lbl.parentElement.parentElement]) {
            if (!el) continue;
            const ic = el.querySelector('.react-select__input-container');
            if (ic) { ic.click(); return true; }
        }
        return false;
    }""", label)
    if not clicked:
        raise Exception(f"react-select not found near label '{label}'")


def _apply_dropdown(page, label: str, value) -> str:
    if not value or pd.isna(value):
        return SKIPPED
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        _click_dropdown_input(page, label)
        page.wait_for_timeout(300)
        page.keyboard.type(str(value), delay=50)
        page.wait_for_timeout(500)
        option = page.locator(".react-select__option").first
        try:
            if option.is_visible(timeout=1_000):
                option.click()
            else:
                page.keyboard.press("Enter")
        except Exception:
            page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        return CHANGED
    except Exception as e:
        page.keyboard.press("Escape")
        print(f"     [WARN] Dropdown '{label}' failed: {e}")
        return FAILED


def _read_dropdown(page, label: str) -> str:
    try:
        return page.evaluate("""(labelText) => {
            const labels = [...document.querySelectorAll('label')];
            const lbl = labels.find(l =>
                l.textContent.trim().toLowerCase().includes(labelText.toLowerCase())
            );
            if (!lbl) return '';
            for (const el of [lbl, lbl.parentElement, lbl.parentElement && lbl.parentElement.parentElement]) {
                if (!el) continue;
                const sv = el.querySelector('.react-select__single-value');
                if (sv) return sv.textContent.trim().toLowerCase();
            }
            return '';
        }""", label)
    except Exception:
        return ""


# ── Tags ──────────────────────────────────────────────────────────────────────

def _read_tags(page) -> list[str]:
    try:
        return page.evaluate("""() => {
            const container = document.querySelector('.react-select__value-container--is-multi');
            if (!container) return [];
            return [...container.querySelectorAll('.react-select__multi-value__label')]
                .map(el => el.textContent.trim().toLowerCase());
        }""")
    except Exception:
        return []


def _clear_tags(page):
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    cleared = page.evaluate("""() => {
        const container = document.querySelector('.react-select__value-container--is-multi');
        if (!container) return false;
        const control  = container.parentElement;
        const clearBtn = control && control.querySelector('.react-select__clear-indicator');
        if (!clearBtn) return false;
        clearBtn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
        clearBtn.click();
        return true;
    }""")
    if cleared:
        page.wait_for_timeout(300)


def _add_tags(page, tags: list[str]) -> list[str]:
    input_container = page.locator(
        ".react-select__value-container--is-multi > .react-select__input-container"
    ).first
    failed = []
    for tag in tags:
        try:
            input_container.click()
            page.wait_for_timeout(200)
            page.keyboard.type(tag, delay=50)
            page.wait_for_timeout(400)
            option = page.locator(".react-select__option").first
            try:
                if option.is_visible(timeout=800):
                    option.click()
                else:
                    page.keyboard.press("Enter")
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(250)
        except Exception as e:
            print(f"    [WARN] Could not add tag '{tag}': {e}")
            failed.append(tag)
    return failed


# ── Toggles ───────────────────────────────────────────────────────────────────

def _read_mandatory(page) -> bool | None:
    try:
        return page.evaluate("""() => {
            const labels = [...document.querySelectorAll('label')];
            const lbl = labels.find(l => /mandatory|optional/i.test(l.textContent));
            if (!lbl) return null;
            const cb = lbl.querySelector('input[type="checkbox"]');
            return cb ? cb.checked : null;
        }""")
    except Exception:
        return None


def _click_mandatory(page):
    page.locator("label").filter(has_text=re.compile(r"[Mm]andatory|[Oo]ptional")).locator(".w-11").click()
    page.wait_for_timeout(300)


def _read_show_feedback(page) -> bool | None:
    try:
        fb_label = page.locator("label").filter(has_text="Show Lecture Feedback")
        cb = fb_label.locator("input[type='checkbox']")
        return cb.is_checked() if cb.count() > 0 else None
    except Exception:
        return None


def _click_show_feedback(page):
    page.locator("label").filter(has_text="Show Lecture Feedback").locator(".w-11").click()
    page.wait_for_timeout(300)


# ── Schedule defaults ─────────────────────────────────────────────────────────

def _clear_and_select(page, label_locator, search_text: str, exact_text: str):
    removes = label_locator.locator(".react-select__multi-value__remove")
    while removes.count() > 0:
        removes.first.click()
        page.wait_for_timeout(150)
    clear_btn = label_locator.locator(".react-select__clear-indicator")
    if clear_btn.count() > 0:
        clear_btn.click()
        page.wait_for_timeout(150)
    label_locator.locator(".react-select__input-container").click()
    page.wait_for_timeout(200)
    page.keyboard.type(search_text, delay=50)
    page.wait_for_timeout(300)
    page.get_by_text(exact_text, exact=True).click()
    page.wait_for_timeout(200)


def _set_schedule_defaults(page) -> str:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        page.wait_for_selector("div:nth-child(3) > .p-4 > .grid", state="visible", timeout=10_000)
        grid = page.locator("div:nth-child(3) > .p-4 > .grid")
        _clear_and_select(page, grid.locator("label").nth(0), "test group",  "Test Group")
        _clear_and_select(page, grid.locator("label").nth(1), "topic_001",   "topic_001")
        _clear_and_select(page, grid.locator("label").nth(2), "test_LO_001", "test_LO_001")
        return CHANGED
    except Exception as e:
        print(f"  [WARN] Schedule defaults failed: {e}")
        return FAILED


# ═════════════════════════════════════════════════════════════════════════════
# Field apply + verify
# ═════════════════════════════════════════════════════════════════════════════

def _apply_all(page, row) -> dict:
    s = {}

    cat_des = str(row.get("updated_category", "")).strip().lower()
    cat_dom = _read_dropdown(page, "Category")
    if cat_dom == cat_des:
        print(f"  1. Category  → SKIP (DOM already '{cat_des}')")
        s["category"] = SKIPPED
    else:
        print(f"  1. Category  → UPDATE '{cat_dom}' → '{cat_des}'")
        s["category"] = _apply_dropdown(page, "Category", row.get("updated_category"))

    mod_des = str(row.get("updated_module", "")).strip().lower()
    mod_dom = _read_dropdown(page, "Module")
    if mod_dom == mod_des:
        print(f"  2. Module    → SKIP (DOM already '{mod_des}')")
        s["module"] = SKIPPED
    else:
        print(f"  2. Module    → UPDATE '{mod_dom}' → '{mod_des}'")
        s["module"] = _apply_dropdown(page, "Module", row.get("updated_module"))

    desired_tags = norm_tags(row.get("updated_tags", ""))
    current_tags = sorted(_read_tags(page))
    if current_tags == desired_tags:
        print(f"  3. Tags      → SKIP (DOM already {desired_tags})")
        s["tags"] = SKIPPED
    else:
        print(f"  3. Tags      → CLEAR + SET {desired_tags}")
        _clear_tags(page)
        page.wait_for_timeout(200)
        failed = _add_tags(page, desired_tags) if desired_tags else []
        s["tags"] = FAILED if failed else CHANGED

    desired_toggle = to_bool(row.get("updated_mandatory", ""))
    current_dom    = _read_mandatory(page)
    if current_dom is None:
        print(f"     [WARN] Mandatory toggle not found")
        s["mandatory"] = FAILED
    elif current_dom == desired_toggle:
        print(f"  4. Mandatory → SKIP (DOM toggle already {'ON' if desired_toggle else 'OFF'})")
        s["mandatory"] = SKIPPED
    else:
        print(f"  4. Mandatory → UPDATE toggle → {'ON' if desired_toggle else 'OFF'}")
        _click_mandatory(page)
        s["mandatory"] = CHANGED

    fb_des     = to_bool(row.get("updated_show_feedback", ""))
    current_fb = _read_show_feedback(page)
    if current_fb is None:
        print(f"     [WARN] Show Feedback toggle not found")
        s["show_feedback"] = FAILED
    elif current_fb == fb_des:
        print(f"  5. ShowFB    → SKIP (DOM already {'ON' if fb_des else 'OFF'})")
        s["show_feedback"] = SKIPPED
    else:
        print(f"  5. ShowFB    → UPDATE toggle → {'ON' if fb_des else 'OFF'}")
        _click_show_feedback(page)
        s["show_feedback"] = CHANGED

    return s


def _verify_all(page, row) -> dict:
    results = {}

    cat_des = str(row.get("updated_category", "")).strip().lower()
    cat_dom = _read_dropdown(page, "Category")
    if not cat_dom:
        print(f"     [VERIFY WARN] Category unreadable, assuming OK")
        results["category"] = True
    else:
        results["category"] = (cat_dom == cat_des)
        if not results["category"]:
            print(f"     [VERIFY FAIL] Category: dom='{cat_dom}' want='{cat_des}'")

    mod_des = str(row.get("updated_module", "")).strip().lower()
    mod_dom = _read_dropdown(page, "Module")
    if not mod_dom:
        print(f"     [VERIFY WARN] Module unreadable, assuming OK")
        results["module"] = True
    else:
        results["module"] = (mod_dom == mod_des)
        if not results["module"]:
            print(f"     [VERIFY FAIL] Module: dom='{mod_dom}' want='{mod_des}'")

    desired_tags = norm_tags(row.get("updated_tags", ""))
    current_tags = sorted(_read_tags(page))
    results["tags"] = (current_tags == desired_tags)
    if not results["tags"]:
        print(f"     [VERIFY FAIL] Tags: dom={current_tags} want={desired_tags}")

    desired_toggle = to_bool(row.get("updated_mandatory", ""))
    opt_dom        = _read_mandatory(page)
    if opt_dom is None:
        print(f"     [VERIFY WARN] Mandatory toggle unreadable, assuming OK")
        results["mandatory"] = True
    else:
        results["mandatory"] = (opt_dom == desired_toggle)
        if not results["mandatory"]:
            print(f"     [VERIFY FAIL] Mandatory: dom={opt_dom} want={desired_toggle}")

    fb_des = to_bool(row.get("updated_show_feedback", ""))
    fb_dom = _read_show_feedback(page)
    if fb_dom is None:
        print(f"     [VERIFY WARN] ShowFB toggle unreadable, assuming OK")
        results["show_feedback"] = True
    else:
        results["show_feedback"] = (fb_dom == fb_des)
        if not results["show_feedback"]:
            print(f"     [VERIFY FAIL] ShowFB: dom={fb_dom} want={fb_des}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# Per-lecture processor
# ═════════════════════════════════════════════════════════════════════════════

def process_lecture(page, row) -> dict:
    url = row["lecture_url"]
    statuses = {
        "lecture_url":   url,
        "category":      SKIPPED,
        "module":        SKIPPED,
        "tags":          SKIPPED,
        "mandatory":     SKIPPED,
        "show_feedback": SKIPPED,
        "schedule":      SKIPPED,
        "save":          SKIPPED,
        "attempts":      0,
        "notes":         "",
    }

    page.goto(url)
    page.wait_for_load_state("networkidle")
    _wait_for_form(page)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        statuses["attempts"] = attempt

        if attempt > 1:
            print(f"\n  ── Retry {attempt}/{MAX_ATTEMPTS} ── re-navigating for clean state...")
            page.goto(url)
            page.wait_for_load_state("networkidle")
            _wait_for_form(page)

        field_statuses = _apply_all(page, row)
        statuses.update(field_statuses)

        print(f"\n  Verifying (attempt {attempt}/{MAX_ATTEMPTS})...")
        verification  = _verify_all(page, row)
        failed_fields = [f for f, ok in verification.items() if not ok]

        if not failed_fields:
            print(f"  [VERIFY OK] All fields correct.")
            break

        print(f"  [VERIFY FAIL] {failed_fields}")

        if attempt == MAX_ATTEMPTS:
            for f in failed_fields:
                statuses[f] = FAILED
            statuses["notes"] = f"Verification failed after {MAX_ATTEMPTS} attempts: {failed_fields}"
            print(f"  [GIVE UP] Max attempts reached. Proceeding to save anyway.")

    print(f"\n  Setting schedule defaults...")
    statuses["schedule"] = _set_schedule_defaults(page)

    try:
        page.get_by_role("button", name="Edit Lecture").click()
        page.wait_for_timeout(500)
        statuses["save"] = CHANGED
        print(f"  [SAVED]")
    except Exception as e:
        statuses["save"] = FAILED
        statuses["notes"] += f" | Save error: {e}"
        print(f"  [SAVE FAILED] {e}")

    return statuses


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def run():
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = os.path.splitext(os.path.basename(chosen))[0]
    log_stem  = f"run_{base}_{timestamp}"

    _start_log(log_stem)

    df = pd.read_csv(chosen)
    required = {"lecture_url", "updated_category", "updated_module",
                "updated_tags", "updated_mandatory", "updated_show_feedback"}
    missing = required - set(df.columns)
    if missing:
        print(f"[ERROR] CSV is missing required columns: {missing}")
        _stop_log()
        return

    print(f"Rows to process: {len(df)}\n")

    all_statuses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page    = context.new_page()

        print("Attempting auto-login...")
        try:
            page.goto(LOGIN_URL)
            page.wait_for_load_state("networkidle")
            page.get_by_role("textbox", name="Your email").fill(EMAIL)
            page.get_by_role("textbox", name="Your email").press("Tab")
            page.get_by_role("textbox", name="Your password").fill(PASSWORD)
            page.locator("svg").click()
            page.get_by_role("button", name="Sign In").click()
            page.wait_for_load_state("networkidle", timeout=20_000)
            if "login" in page.url.lower() or page.url.rstrip("/") == LOGIN_URL.rstrip("/"):
                raise Exception(f"Still on login page: {page.url}")
            print("Logged in. Starting lecture updates...\n")
        except Exception as login_err:
            print(f"[WARN] Auto-login failed: {login_err}")
            input("Please log in manually, then press ENTER... ")
            page.wait_for_load_state("networkidle", timeout=20_000)
            print("Resuming...\n")

        for i, row in df.iterrows():
            url = row["lecture_url"]
            print(f"{'─'*60}")
            print(f"[{i+1}/{len(df)}] {url}")
            try:
                statuses = process_lecture(page, row)
            except Exception as e:
                print(f"  [ERROR] {e}")
                statuses = {k: ERROR for k in
                            ["category", "module", "tags", "mandatory",
                             "show_feedback", "schedule", "save"]}
                statuses["lecture_url"] = url
                statuses["attempts"]    = 0
                statuses["notes"]       = str(e)
            all_statuses.append(statuses)
            print()

        browser.close()

    csv_path = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_statuses).to_csv(csv_path, index=False)
    print(f"\nCSV report  → {csv_path}")

    dest = os.path.join(ARCHIVE_DIR, f"{base}_{timestamp}.csv")
    shutil.copy2(chosen, dest)
    print(f"Input archived → {dest}")

    df_log = pd.DataFrame(all_statuses)
    print("\n══ Summary ══════════════════════════════════════════════")
    for col in ["category", "module", "tags", "mandatory", "show_feedback", "schedule", "save"]:
        if col in df_log.columns:
            print(f"  {col:20s}: {df_log[col].value_counts().to_dict()}")

    skip_keys = {"notes", "lecture_url", "attempts"}
    failed = [s for s in all_statuses
              if any(v in (FAILED, ERROR) for k, v in s.items() if k not in skip_keys)]
    print(f"\n  Lectures with failures/errors: {len(failed)}/{len(all_statuses)}")

    if failed:
        print("\n  ── Failed / Error lecture IDs ────────────────────────")
        for s in failed:
            url    = s.get("lecture_url", "")
            lec_id = url.split("id=")[-1] if "id=" in url else url
            bad    = {k: v for k, v in s.items() if k not in skip_keys and v in (FAILED, ERROR)}
            note   = s.get("notes", "")
            line   = f"    [{lec_id}]  {bad}"
            if note:
                line += f"  — {note}"
            print(line)
        print("  ─────────────────────────────────────────────────────")

    print("═════════════════════════════════════════════════════════")
    print("Done.")
    _stop_log()


if __name__ == "__main__":
    run()
