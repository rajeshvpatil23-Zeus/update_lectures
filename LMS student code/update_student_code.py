"""
Bulk updater for LMS student code (UserName) on:
https://experience-admin.masaischool.com/Users/

Expected CSV columns (flexible header matching):
  Name, email, Old Student code, new student code

Preferred search key is email; old student code is used as fallback.
"""

import os
import re
import sys
import glob
import shutil
from datetime import datetime

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


USERS_URL = "https://experience-admin.masaischool.com/Users/"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE_DIR, "input")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
ARCHIVE_DIR = os.path.join(LOGS_DIR, "archive")
PROFILE_DIR = os.path.join(BASE_DIR, "browser_profile")

for d in (INPUT_DIR, LOGS_DIR, ARCHIVE_DIR, PROFILE_DIR):
    os.makedirs(d, exist_ok=True)

SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED = "FAILED"
ERROR = "ERROR"

RESULT_FIELDS = [
    "name",
    "email",
    "old_student_code",
    "new_student_code",
    "username_update",
    "notes",
]


try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class _Tee:
    def __init__(self, filepath: str):
        self._file = open(filepath, "w", buffering=1, encoding="utf-8")
        self._stdout = sys.stdout
        self._pending = ""

    def write(self, data: str):
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
    print(f"Log -> {path}")


def _stop_log():
    global _tee
    if _tee:
        sys.stdout = _tee._stdout
        _tee.close()
        _tee = None


def _safe_input(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        print("\n[WARN] No interactive stdin available; cannot pause for ENTER.")
        return None


def _canon(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).strip().lower())


def _pick_column(df: pd.DataFrame, aliases: list[str]) -> str | None:
    wanted = {_canon(a) for a in aliases}
    for col in df.columns:
        if _canon(col) in wanted:
            return col
    return None


def _load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    old_col = _pick_column(df, ["Old Student code", "old_student_code", "old code", "username"])
    new_col = _pick_column(df, ["new student code", "new_student_code", "new code", "updated username"])
    if old_col and new_col:
        return df

    # Fallback for files separated by "||"
    df2 = pd.read_csv(path, dtype=str, sep=r"\s*\|\|\s*", engine="python")
    old_col2 = _pick_column(df2, ["Old Student code", "old_student_code", "old code", "username"])
    new_col2 = _pick_column(df2, ["new student code", "new_student_code", "new code", "updated username"])
    if old_col2 and new_col2:
        return df2
    return df


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    col_name = _pick_column(df, ["Name"])
    col_email = _pick_column(df, ["email", "email id", "mail"])
    col_old = _pick_column(df, ["Old Student code", "old_student_code", "old code", "username"])
    col_new = _pick_column(df, ["new student code", "new_student_code", "new code", "updated username"])

    if not col_old or not col_new:
        raise ValueError(
            "CSV must contain columns for old and new student code "
            "(e.g. 'Old Student code' and 'new student code')."
        )

    out = pd.DataFrame()
    out["name"] = df[col_name] if col_name else ""
    out["email"] = df[col_email] if col_email else ""
    out["old_student_code"] = df[col_old]
    out["new_student_code"] = df[col_new]
    for c in out.columns:
        out[c] = out[c].fillna("").astype(str).str.strip()
    return out


def _close_modal_if_open(page):
    try:
        close_btn = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^x$", re.I))
        if close_btn.count() > 0 and close_btn.first.is_visible():
            close_btn.first.click()
            page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


def _find_search_box(page):
    return page.get_by_placeholder("Search by code, name, email & mobile...")


def _open_edit_for_user(page, email: str, old_code: str):
    query = email.strip() if email.strip() else old_code.strip()
    if not query:
        raise ValueError("Both email and old student code are blank.")

    search = _find_search_box(page)
    search.wait_for(state="visible", timeout=12_000)
    search.click()
    search.fill("")
    search.fill(query)
    page.wait_for_timeout(1200)

    # Prefer exact row match by email when provided; otherwise use old student code.
    row_key = email.strip() if email.strip() else old_code.strip()
    row = page.locator("tr").filter(has_text=re.compile(re.escape(row_key), re.I))
    row.wait_for(state="visible", timeout=10_000)
    edit_btn = row.get_by_role("button", name=re.compile(r"edit", re.I)).first
    edit_btn.wait_for(state="visible", timeout=8_000)
    edit_btn.click()
    page.wait_for_timeout(700)


def _set_username_and_update(page, old_code: str, new_code: str) -> str:
    dialog = page.get_by_role("dialog").filter(has_text=re.compile(r"Edit User", re.I))
    dialog.wait_for(state="visible", timeout=10_000)

    # Prefer explicit label "UserName"
    username_input = dialog.locator("label", has_text=re.compile(r"^UserName$", re.I)).locator(
        "xpath=following::input[1]"
    ).first
    if username_input.count() == 0:
        # Fallback to first visible text input inside modal.
        username_input = dialog.locator("input[type='text']").first

    username_input.wait_for(state="visible", timeout=8_000)
    current = username_input.input_value().strip()

    if current == new_code:
        print(f"    SKIP (already '{new_code}')")
        _close_modal_if_open(page)
        return SKIPPED

    print(f"    UPDATE '{current}' -> '{new_code}'")
    username_input.click()
    username_input.fill(new_code)
    page.wait_for_timeout(250)
    update_btn = dialog.get_by_role("button", name=re.compile(r"^Update$", re.I)).first
    update_btn.wait_for(state="visible", timeout=8_000)
    update_btn.click()

    # Wait for modal close; if still visible, treat as failure.
    try:
        dialog.wait_for(state="hidden", timeout=10_000)
    except PlaywrightTimeoutError as e:
        raise RuntimeError(f"Update clicked but modal did not close: {e}") from e

    return CHANGED


def process_row(page, row: pd.Series) -> dict:
    result = {k: "" for k in RESULT_FIELDS}
    result["name"] = row.get("name", "")
    result["email"] = row.get("email", "")
    result["old_student_code"] = row.get("old_student_code", "")
    result["new_student_code"] = row.get("new_student_code", "")
    result["username_update"] = SKIPPED
    result["notes"] = ""

    old_code = result["old_student_code"].strip()
    email = result["email"].strip()
    new_code = result["new_student_code"].strip()
    if not new_code or (not email and not old_code):
        result["username_update"] = FAILED
        result["notes"] = "new student code missing, or both email and old student code are blank"
        return result

    if email:
        print(f"  Search email: {email}")
    else:
        print(f"  Search old code (fallback): {old_code}")

    _open_edit_for_user(page, email=email, old_code=old_code)
    result["username_update"] = _set_username_and_update(page, old_code, new_code)
    return result


def _write_report(all_results: list[dict], log_stem: str, src_csv: str):
    out_csv = os.path.join(LOGS_DIR, f"{log_stem}.csv")
    pd.DataFrame(all_results).to_csv(out_csv, index=False)
    print(f"\nCSV report -> {out_csv}")

    archive_name = log_stem[len("run_"):]
    dest = os.path.join(ARCHIVE_DIR, f"{archive_name}.csv")
    shutil.copy2(src_csv, dest)
    print(f"Input archived -> {dest}")

    df = pd.DataFrame(all_results)
    print("\n== Summary ==")
    if "username_update" in df.columns:
        print(f"  username_update: {df['username_update'].value_counts().to_dict()}")
    print("Done.")


def _select_csv() -> str | None:
    csv_files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.csv")))
    if not csv_files:
        print(f"[ERROR] No CSV files found in {INPUT_DIR}/")
        return None

    print(f"Found {len(csv_files)} CSV file(s):")
    for i, f in enumerate(csv_files):
        print(f"  [{i}] {os.path.basename(f)}")

    if len(csv_files) == 1:
        chosen = csv_files[0]
        print(f"Auto-selecting: {os.path.basename(chosen)}")
        return chosen

    idx = input("\nEnter file number: ").strip()
    try:
        return csv_files[int(idx)]
    except (ValueError, IndexError):
        print("[ERROR] Invalid selection.")
        return None


def run():
    chosen = _select_csv()
    if not chosen:
        return

    try:
        raw_df = _load_csv(chosen)
        df = _normalize_df(raw_df)
    except Exception as e:
        print(f"[ERROR] {e}")
        return

    print(f"Rows to process: {len(df)}")

    print("\nStep 1/2: Login check")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(USERS_URL)
        page.wait_for_load_state("networkidle")
        print(f"Current URL: {page.url}")
        _safe_input("Login if needed, open Users page, then press ENTER... ")
        context.close()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.splitext(os.path.basename(chosen))[0]
    log_stem = f"run_{base}_{timestamp}"
    _start_log(log_stem)
    print("Step 2/2: Bulk username update\n")

    all_results = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            slow_mo=200,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(USERS_URL)
        page.wait_for_load_state("networkidle")

        total = len(df)
        for i, row in df.iterrows():
            print("-" * 60)
            print(f"[{i+1}/{total}] old='{row.get('old_student_code', '')}' new='{row.get('new_student_code', '')}'")
            try:
                result = process_row(page, row)
            except Exception as e:
                result = {
                    "name": row.get("name", ""),
                    "email": row.get("email", ""),
                    "old_student_code": row.get("old_student_code", ""),
                    "new_student_code": row.get("new_student_code", ""),
                    "username_update": ERROR,
                    "notes": str(e),
                }
                _close_modal_if_open(page)
                print(f"  [ERROR] {e}")
            all_results.append(result)
            page.wait_for_timeout(500)

        context.close()

    _write_report(all_results, log_stem, chosen)
    _stop_log()


if __name__ == "__main__":
    run()
