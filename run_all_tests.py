# -*- coding: utf-8 -*-
"""Run every root ``test_*.py`` suite in a fresh subprocess.

Discovery is fully automatic: every ``test_*.py`` file in the repository root
(except this runner itself) is executed with the current Python interpreter,
one file per subprocess, so each suite gets a clean import state. Suites that
cover other branches (their test files land alongside this runner on their own
branches) are listed in ``SELF_TEST_EXCLUDE`` — they are discovered and
reported but skipped, so this file never conflicts at merge time on lists of
its own tests. Results are reported per file followed by a total summary; the
exit code is non-zero if any test file fails. Supports short summary via the
``--short`` flag and a per-file timeout via the ``TEST_TIMEOUT`` env var
(default 300s).
"""
import glob
import os
import subprocess
import sys

_CHECK = "[check]"

# Test suites owned by other feature branches; discovered but skipped here so
# this file never carries a conflicting list of its own tests at merge time.
SELF_TEST_EXCLUDE = {
    "test_blurpad.py",
    "test_captions.py",
    "test_downloader_selector.py",
    "test_ffmpeg_ops.py",
    "test_frameops.py",
    "test_highlights.py",
    "test_thumbgen.py",
    "test_transcriber.py",
}


def run_test(path, timeout=300):
    """Run a single test file in a subprocess.

    Returns (status, returncode, output) where status is one of
    "PASS", "SKIP", "TIMEOUT" or "FAIL".
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        res = subprocess.run(
            [sys.executable, "-X", "utf8", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None, ""
    except Exception as exc:  # pragma: no cover - unexpected runner failure
        return "FAIL", None, f"runner error: {exc}"
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        return "FAIL", res.returncode, out
    # Convention: suites print a SKIP note when optional deps are missing.
    if "SKIP" in out:
        return "SKIP", res.returncode, out
    return "PASS", res.returncode, out


def main():
    short = "--short" in sys.argv[1:]
    try:
        timeout = int(os.environ.get("TEST_TIMEOUT", "300"))
    except ValueError:
        timeout = 300
    root = os.path.dirname(os.path.abspath(__file__))
    self_name = os.path.basename(os.path.abspath(__file__))
    tests = sorted(
        p
        for p in glob.glob(os.path.join(root, "test_*.py"))
        if os.path.basename(p) != self_name
    )
    results = []
    for path in tests:
        name = os.path.basename(path)
        if name in SELF_TEST_EXCLUDE:
            print(f"{_CHECK} SKIP {name}: other task coverage (excluded)", flush=True)
            results.append((name, "SKIP", 0.0))
            continue
        print(f"{_CHECK} RUN  {name} ...", flush=True)
        status, rc, out = run_test(path, timeout=timeout)
        tail = ""
        if not short:
            tail = (out or "").strip().splitlines()
            tail = tail[-1] if tail else ""
        elif status != "PASS" and out:
            lines = [ln for ln in out.strip().splitlines() if "FAIL" in ln or "SKIP" in ln]
            tail = lines[0] if lines else out.strip().splitlines()[-1]
        results.append((name, status, rc))
        print(f"{_CHECK} {status} {name}" + (f" | {tail}" if tail else ""), flush=True)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "TIMEOUT": 0}
    for _, status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    print(f"{_CHECK} Summary: {len(results)} file(s)")
    for name, status, rc in results:
        print(f"  {status:7s} {name}")
    print(
        f"{_CHECK} PASS={counts['PASS']} FAIL={counts['FAIL']} "
        f"SKIP={counts['SKIP']} TIMEOUT={counts['TIMEOUT']}"
    )
    bad = counts["FAIL"] + counts["TIMEOUT"]
    if bad:
        print(f"[FAIL] {bad} test file(s) failed")
        return 1
    print("[OK] all tests green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
