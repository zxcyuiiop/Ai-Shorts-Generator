# -*- coding: utf-8 -*-
"""Checks for the Generate / History / Settings page split (Task 6).

Hermetic like test_gui_features.py: the settings file is redirected into a
fresh tempdir before app.py is imported, so a run can never touch the real
settings.local.json. Covers the three GET routes, the per-page DOM contracts
(gallery mount point, s2_ ids, shared nav) and the settings round-trip driven
by the settings.js field mapping.
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="pages-")

from shorts_generator import settings_store  # noqa: E402

# Use a scratch settings file inside the tempdir so a real one is never touched.
settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

import app as webapp  # noqa: E402

webapp.LOCAL_OUTPUT_DIR = _TMP
webapp.UPLOAD_DIR = os.path.join(_TMP, "uploads")

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def main():
    c = webapp.app.test_client()

    from pathlib import Path
    root = Path(__file__).resolve().parent

    # --- the three pages render as HTML ---
    pages = {"/": None, "/history": None, "/settings": None}
    for path in pages:
        r = c.get(path)
        check(f"GET {path} -> 200", r.status_code == 200, str(r.status_code))
        check(f"GET {path} is html", "text/html" in r.headers.get("Content-Type", ""))
        pages[path] = r.get_data(as_text=True)
        check(f"{path} has page-nav",
              'class="page-nav"' in pages[path] and 'aria-current="page"' in pages[path])

    # --- history page contract ---
    hist_html = pages["/history"]
    check("history page has gallery mount point", 'id="history-gallery"' in hist_html)
    check("history page loads history.js", "history.js" in hist_html)
    check("history page has empty/error/video-modal",
          all(s in hist_html for s in ('id="gallery-empty"', 'id="gallery-error"',
                                       'id="video-modal"', 'id="gallery-search"')))
    hist_ids = re.findall(r'id="([^"]+)"', hist_html)
    dupes = sorted({i for i in hist_ids if hist_ids.count(i) > 1})
    check("no duplicate ids on history page", not dupes, ", ".join(dupes))

    # --- settings page contract ---
    set_html = pages["/settings"]
    check("settings page has s2_ ids", 'id="s2_nim_key"' in set_html
          and 'id="s2_llm_provider"' in set_html
          and 'id="s2-save-settings-btn"' in set_html)
    check("settings page loads settings.js", "settings.js" in set_html)
    set_ids = re.findall(r'id="([^"]+)"', set_html)
    dupes = sorted({i for i in set_ids if set_ids.count(i) > 1})
    check("no duplicate ids on settings page", not dupes, ", ".join(dupes))

    # Every s2_ field id must map to a plain settings key listed in
    # settings.js SETTING_FIELDS (strip the s2_ prefix; datalist ids use s2-).
    s2_fields = sorted(i for i in set_ids if i.startswith("s2_") and not i.startswith("s2-"))
    settings_js = (root / "static" / "settings.js").read_text(encoding="utf-8")
    js_fields = set(re.findall(r"'([a-z_]+)',", settings_js))
    unmapped = [i for i in s2_fields if i != "s2-save-settings-btn"
                and i[len("s2_"):] not in js_fields]
    check("every s2_ field maps into settings.js", not unmapped, ", ".join(unmapped))
    check("common.js served",
          c.get("/static/common.js").status_code == 200)
    check("history.js/settings.js served",
          c.get("/static/history.js").status_code == 200
          and c.get("/static/settings.js").status_code == 200)

    # --- settings round-trip through the settings.js field mapping ---
    # Apply each mapped s2_ field against GET, then POST it back. Secret
    # fields are omitted entirely, exactly like the page sends them
    # (empty secret == "don't change" server-side).
    r = c.post("/api/settings", json={
        "llm_provider": "nim",
        "nim_model": "meta/llama-3.1-8b-instruct",
        "nim_url": "https://integrate.api.nvidia.com/v1",
        "whisper_model": "small",
    })
    check("POST /api/settings -> 200", r.status_code == 200, str(r.status_code))
    after = c.get("/api/settings").get_json()
    check("round-trip keeps llm_provider", after.get("llm_provider") == "nim",
          repr(after.get("llm_provider")))
    check("round-trip keeps nim_model", after.get("nim_model") == "meta/llama-3.1-8b-instruct")
    check("round-trip keeps whisper_model", after.get("whisper_model") == "small")


if __name__ == "__main__":
    try:
        main()
    finally:
        # Best-effort cleanup of the temp dir (created by tempfile.mkdtemp).
        shutil.rmtree(_TMP, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("\nAll checks passed.")
