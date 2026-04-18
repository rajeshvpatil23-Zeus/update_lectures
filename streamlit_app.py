"""
streamlit_app.py — Lecture Updater (Streamlit UI)

Modes
  • Title Update   — updates the lecture title field only
  • Param Update   — updates category, module, tags, mandatory, show_feedback

Deploy:
  streamlit run streamlit_app.py
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
import queue
import os
import pandas as pd
import streamlit as st
from datetime import datetime


# ── Playwright browser bootstrap (runs once on cold start, cached) ─────────────
@st.cache_resource(show_spinner="Installing browser (first run only)...")
def _install_playwright_browser():
    """Install Playwright Chromium. No-op if already installed."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            st.warning(f"Browser install warning: {result.stderr[:300]}")
    except Exception as e:
        st.warning(f"Could not auto-install browser: {e}")


_install_playwright_browser()

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Lecture Updater",
    page_icon="📚",
    layout="wide",
)

# ── Constants ─────────────────────────────────────────────────────────────────
SKIPPED = "SKIPPED"
CHANGED = "CHANGED"
FAILED  = "FAILED"
ERROR   = "ERROR"

LOGIN_URL = "https://experience-admin.masaischool.com/"

TITLE_COLUMNS  = ["lecture_url", "updated_title"]
PARAMS_COLUMNS = [
    "lecture_url", "updated_category", "updated_module",
    "updated_tags", "updated_mandatory", "updated_show_feedback",
]

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")


# ═════════════════════════════════════════════════════════════════════════════
# Low-level Playwright helpers (identical logic to update_lecture_v4 / update_title)
# ═════════════════════════════════════════════════════════════════════════════

def _wait_for_form(page):
    try:
        page.wait_for_selector('button:has-text("Edit Lecture")', state="visible", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(400)


def to_bool(val) -> bool:
    return str(val).strip().upper() in ("TRUE", "YES", "1")


def norm_tags(val) -> list[str]:
    return sorted(t.strip().lower() for t in str(val).split(",") if t.strip())


# ── Title helpers ─────────────────────────────────────────────────────────────

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
        return FAILED


# ── Dropdown helpers ──────────────────────────────────────────────────────────

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
        return FAILED


# ── Tag helpers ───────────────────────────────────────────────────────────────

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
    page.evaluate("""() => {
        const container = document.querySelector('.react-select__value-container--is-multi');
        if (!container) return false;
        const control  = container.parentElement;
        const clearBtn = control && control.querySelector('.react-select__clear-indicator');
        if (!clearBtn) return false;
        clearBtn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
        clearBtn.click();
        return true;
    }""")
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
        except Exception:
            failed.append(tag)
    return failed


# ── Toggle helpers ────────────────────────────────────────────────────────────

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
        return FAILED


# ═════════════════════════════════════════════════════════════════════════════
# Per-lecture processors
# ═════════════════════════════════════════════════════════════════════════════

def process_title(page, row) -> dict:
    url     = row["lecture_url"]
    desired = str(row.get("updated_title", "")).strip()
    result  = {"lecture_url": url, "title": SKIPPED, "save": SKIPPED, "notes": ""}

    if not desired:
        result["notes"] = "updated_title is empty"
        return result

    page.goto(url)
    page.wait_for_load_state("networkidle")
    _wait_for_form(page)

    current = _read_title(page)

    if current == desired:
        result["title"] = SKIPPED
    else:
        result["title"] = _set_title(page, desired)
        actual = _read_title(page)
        if actual != desired:
            result["title"] = FAILED
            result["notes"] = f"Verify failed: dom='{actual}'"

    if result["title"] != SKIPPED:
        try:
            page.get_by_role("button", name="Edit Lecture").click()
            page.wait_for_timeout(500)
            result["save"] = CHANGED
        except Exception as e:
            result["save"] = FAILED
            result["notes"] += f" | Save error: {e}"

    return result


def _apply_params(page, row) -> dict:
    s = {}

    cat_des = str(row.get("updated_category", "")).strip().lower()
    cat_dom = _read_dropdown(page, "Category")
    if cat_dom == cat_des:
        s["category"] = SKIPPED
    else:
        s["category"] = _apply_dropdown(page, "Category", row.get("updated_category"))

    mod_des = str(row.get("updated_module", "")).strip().lower()
    mod_dom = _read_dropdown(page, "Module")
    if mod_dom == mod_des:
        s["module"] = SKIPPED
    else:
        s["module"] = _apply_dropdown(page, "Module", row.get("updated_module"))

    desired_tags = norm_tags(row.get("updated_tags", ""))
    current_tags = sorted(_read_tags(page))
    if current_tags == desired_tags:
        s["tags"] = SKIPPED
    else:
        _clear_tags(page)
        page.wait_for_timeout(200)
        failed = _add_tags(page, desired_tags) if desired_tags else []
        s["tags"] = FAILED if failed else CHANGED

    desired_toggle = to_bool(row.get("updated_mandatory", ""))
    current_mand   = _read_mandatory(page)
    if current_mand is None:
        s["mandatory"] = FAILED
    elif current_mand == desired_toggle:
        s["mandatory"] = SKIPPED
    else:
        _click_mandatory(page)
        s["mandatory"] = CHANGED

    fb_des     = to_bool(row.get("updated_show_feedback", ""))
    current_fb = _read_show_feedback(page)
    if current_fb is None:
        s["show_feedback"] = FAILED
    elif current_fb == fb_des:
        s["show_feedback"] = SKIPPED
    else:
        _click_show_feedback(page)
        s["show_feedback"] = CHANGED

    return s


def process_params(page, row) -> dict:
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
        "notes":         "",
    }

    page.goto(url)
    page.wait_for_load_state("networkidle")
    _wait_for_form(page)

    field_statuses = _apply_params(page, row)
    statuses.update(field_statuses)

    statuses["schedule"] = _set_schedule_defaults(page)

    try:
        page.get_by_role("button", name="Edit Lecture").click()
        page.wait_for_timeout(500)
        statuses["save"] = CHANGED
    except Exception as e:
        statuses["save"] = FAILED
        statuses["notes"] += f" | Save error: {e}"

    return statuses


# ═════════════════════════════════════════════════════════════════════════════
# Background runner — communicates via a queue so Streamlit can poll progress
# ═════════════════════════════════════════════════════════════════════════════

def _run_in_thread(mode: str, df: pd.DataFrame, email: str, password: str,
                   headless: bool, result_queue: queue.Queue):
    """
    Runs Playwright in a background thread.
    Puts progress dicts onto result_queue:
      {"type": "progress", "done": int, "total": int, "status": dict}
      {"type": "done",     "results": list[dict]}
      {"type": "error",    "message": str}
    """
    from playwright.sync_api import sync_playwright

    total = len(df)
    results = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"] if headless else [],
            )
            context = browser.new_context()
            page    = context.new_page()

            # ── Login ─────────────────────────────────────────────────────────
            page.goto(LOGIN_URL)
            page.wait_for_load_state("networkidle")
            page.get_by_role("textbox", name="Your email").fill(email)
            page.get_by_role("textbox", name="Your email").press("Tab")
            page.get_by_role("textbox", name="Your password").fill(password)
            page.locator("svg").click()
            page.get_by_role("button", name="Sign In").click()
            page.wait_for_load_state("networkidle", timeout=20_000)

            if "login" in page.url.lower() or page.url.rstrip("/") == LOGIN_URL.rstrip("/"):
                result_queue.put({"type": "error", "message": "Login failed — check your credentials."})
                browser.close()
                return

            # ── Process each row ──────────────────────────────────────────────
            processor = process_title if mode == "title" else process_params

            for i, row in df.iterrows():
                try:
                    status = processor(page, row)
                except Exception as e:
                    url = row.get("lecture_url", "?")
                    status = {"lecture_url": url, "notes": str(e)}
                    for col in (TITLE_COLUMNS if mode == "title" else PARAMS_COLUMNS):
                        if col not in status:
                            status[col] = ERROR

                results.append(status)
                result_queue.put({
                    "type":   "progress",
                    "done":   i + 1,
                    "total":  total,
                    "status": status,
                })

            browser.close()

    except Exception as e:
        result_queue.put({"type": "error", "message": str(e)})
        return

    result_queue.put({"type": "done", "results": results})


# ═════════════════════════════════════════════════════════════════════════════
# Streamlit UI
# ═════════════════════════════════════════════════════════════════════════════

def _status_color(val: str) -> str:
    colors = {CHANGED: "green", SKIPPED: "grey", FAILED: "red", ERROR: "red"}
    return f"color: {colors.get(str(val).upper(), 'black')}"


def _df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()


def main():
    # ── Session state init ────────────────────────────────────────────────────
    if "running"  not in st.session_state: st.session_state.running  = False
    if "results"  not in st.session_state: st.session_state.results  = []
    if "log_lines" not in st.session_state: st.session_state.log_lines = []
    if "q"        not in st.session_state: st.session_state.q        = None

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("📚 Lecture Updater")
        st.divider()

        mode = st.radio(
            "Update mode",
            options=["title", "params"],
            format_func=lambda x: "Title Only" if x == "title" else "Full Parameters",
            index=0,
        )

        st.divider()
        st.subheader("Sample CSV")
        if mode == "title":
            sample_path = os.path.join(SAMPLE_DIR, "sample_title.csv")
            sample_name = "sample_title.csv"
        else:
            sample_path = os.path.join(SAMPLE_DIR, "sample_params.csv")
            sample_name = "sample_params.csv"

        if os.path.exists(sample_path):
            with open(sample_path, "rb") as f:
                st.download_button(
                    label="Download sample CSV",
                    data=f.read(),
                    file_name=sample_name,
                    mime="text/csv",
                )
        else:
            st.warning("Sample file not found.")

        st.divider()
        st.subheader("Browser")
        headless = st.toggle("Headless (no visible browser)", value=True)

    # ── Main area ─────────────────────────────────────────────────────────────
    st.header("Lecture Updater")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader(
            "Title Update" if mode == "title" else "Parameter Update"
        )
        if mode == "title":
            st.caption("Required columns: `lecture_url`, `updated_title`")
        else:
            st.caption(
                "Required columns: `lecture_url`, `updated_category`, "
                "`updated_module`, `updated_tags`, `updated_mandatory`, `updated_show_feedback`"
            )

    with col2:
        with st.expander("Credentials", expanded=False):
            email    = st.text_input("Email",    value="ravi.kiran@masaischool.com")
            password = st.text_input("Password", value="AgentMarley@2", type="password")

    # ── File upload ───────────────────────────────────────────────────────────
    uploaded = st.file_uploader("Upload CSV", type=["csv"])

    df_preview = None
    if uploaded is not None:
        try:
            df_preview = pd.read_csv(uploaded)
            st.success(f"Loaded **{len(df_preview)} rows** from `{uploaded.name}`")

            required = set(TITLE_COLUMNS if mode == "title" else PARAMS_COLUMNS)
            missing  = required - set(df_preview.columns)
            if missing:
                st.error(f"Missing required columns: `{missing}`")
                df_preview = None
            else:
                with st.expander("Preview (first 5 rows)"):
                    st.dataframe(df_preview.head())
        except Exception as e:
            st.error(f"Could not read CSV: {e}")

    # ── Run button ────────────────────────────────────────────────────────────
    run_disabled = (df_preview is None or st.session_state.running)
    run_btn = st.button("▶ Run", disabled=run_disabled, type="primary")

    if run_btn and df_preview is not None:
        st.session_state.running   = True
        st.session_state.results   = []
        st.session_state.log_lines = []
        q = queue.Queue()
        st.session_state.q = q

        thread = threading.Thread(
            target=_run_in_thread,
            args=(mode, df_preview.copy(), email, password, headless, q),
            daemon=True,
        )
        thread.start()
        st.rerun()

    # ── Live progress ─────────────────────────────────────────────────────────
    if st.session_state.running and st.session_state.q is not None:
        q     = st.session_state.q
        total = len(df_preview) if df_preview is not None else 1

        progress_bar = st.progress(0.0, text="Starting...")
        status_box   = st.empty()
        log_area     = st.empty()

        while st.session_state.running:
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                # Re-render log without new message
                if st.session_state.log_lines:
                    log_area.text_area(
                        "Live log", "\n".join(st.session_state.log_lines[-40:]),
                        height=200, disabled=True
                    )
                continue

            if msg["type"] == "progress":
                done       = msg["done"]
                row_status = msg["status"]
                pct        = done / total
                pct_label  = f"{int(pct*100)}% — {done}/{total} lectures processed"
                progress_bar.progress(pct, text=pct_label)

                url = row_status.get("lecture_url", "?")
                flags = {k: v for k, v in row_status.items()
                         if k not in ("lecture_url", "notes")}
                flag_str = "  ".join(f"{k}={v}" for k, v in flags.items())
                log_line = f"[{done}/{total}] {url[-60:]}  {flag_str}"
                st.session_state.log_lines.append(log_line)
                st.session_state.results.append(row_status)

                status_box.info(f"Processing {done}/{total}: `{url[-60:]}`")
                log_area.text_area(
                    "Live log", "\n".join(st.session_state.log_lines[-40:]),
                    height=200, disabled=True
                )

            elif msg["type"] == "error":
                st.error(f"Error: {msg['message']}")
                st.session_state.running = False
                break

            elif msg["type"] == "done":
                st.session_state.results = msg["results"]
                st.session_state.running = False
                progress_bar.progress(1.0, text="100% — Done!")
                status_box.success("All lectures processed.")
                break

        st.rerun()

    # ── Results ───────────────────────────────────────────────────────────────
    if st.session_state.results and not st.session_state.running:
        st.divider()
        st.subheader("Results")

        df_res = pd.DataFrame(st.session_state.results)

        # Summary counts
        status_cols = [c for c in df_res.columns if c not in ("lecture_url", "notes", "attempts")]
        summary_rows = []
        for col in status_cols:
            vc = df_res[col].value_counts().to_dict()
            summary_rows.append({
                "Field":   col,
                "CHANGED": vc.get(CHANGED, 0),
                "SKIPPED": vc.get(SKIPPED, 0),
                "FAILED":  vc.get(FAILED,  0),
                "ERROR":   vc.get(ERROR,   0),
            })
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        st.divider()

        # Full results table (colour-coded)
        def _highlight(val):
            return _status_color(val)

        style_cols = [c for c in status_cols if c in df_res.columns]
        styled = df_res.style.applymap(_highlight, subset=style_cols)
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # Download
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_name = f"lecture_report_{mode}_{timestamp}.csv"
        st.download_button(
            label="⬇ Download Report CSV",
            data=_df_to_csv_bytes(df_res),
            file_name=report_name,
            mime="text/csv",
            type="primary",
        )

        if st.button("Clear results"):
            st.session_state.results   = []
            st.session_state.log_lines = []
            st.rerun()


if __name__ == "__main__":
    main()
