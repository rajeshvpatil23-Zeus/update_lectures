"""
update_title.py — Bulk lecture title updater

CSV columns (required):
  lecture_url, updated_title

Place input CSV in ./input/ and run:
  python update_title.py
"""

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

def _wait_for_form(page):
    try:
        page.wait_for_selector('button:has-text("Edit Lecture")', state="visible", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(400)


def _read_title(page) -> str:
    try:
        return page.get_by_placeholder("Enter Title").input_value().strip()
    except Exception:
        return ""


def _set_title(page, value: str) -> str:
    try:
        field = page.get_by_placeholder("Enter Title")
        field.click()
        field.select_all()
        field.fill(value)
        page.wait_for_timeout(200)
        return CHANGED
    except Exception as e:
        print(f"     [WARN] Title update failed: {e}")
        return FAILED


# ═════════════════════════════════════════════════════════════════════════════
# Per-lecture processor
# ═════════════════════════════════════════════════════════════════════════════

def process_lecture(page, row) -> dict:
    url     = row["lecture_url"]
    desired = str(row.get("updated_title", "")).strip()
    s = {"lecture_url": url, "title": SKIPPED, "save": SKIPPED, "notes": ""}

    if not desired:
        print(f"  [SKIP] updated_title is empty.")
        return s

    page.goto(url)
    page.wait_for_load_state("networkidle")
    _wait_for_form(page)

    current = _read_title(page)

    if current == desired:
        print(f"  1. Title → SKIP (DOM already '{desired}')")
    else:
        print(f"  1. Title → UPDATE '{current}' → '{desired}'")
        s["title"] = _set_title(page, desired)

        actual = _read_title(page)
        if actual != desired:
            print(f"     [VERIFY FAIL] got '{actual}', want '{desired}'")
            s["title"] = FAILED
            s["notes"] = f"Verify failed: dom='{actual}'"
        else:
            print(f"     [VERIFY OK]")

    if s["title"] != SKIPPED:
        try:
            page.get_by_role("button", name="Edit Lecture").click()
            page.wait_for_timeout(500)
            s["save"] = CHANGED
            print(f"  [SAVED]")
        except Exception as e:
            s["save"]   = FAILED
            s["notes"] += f" | Save error: {e}"
            print(f"  [SAVE FAILED] {e}")

    return s


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
    missing = {"lecture_url", "updated_title"} - set(df.columns)
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
            print("Logged in. Starting title updates...\n")
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
                statuses = {"lecture_url": url, "title": ERROR, "save": ERROR, "notes": str(e)}
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
    for col in ["title", "save"]:
        if col in df_log.columns:
            print(f"  {col:20s}: {df_log[col].value_counts().to_dict()}")

    skip_keys = {"notes", "lecture_url"}
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
