# -*- coding: utf-8 -*-
"""Run every root ``test_*.py`` suite in a fresh subprocess.

Discovery is automatic: every ``test_*.py`` file in the repository root
(except this runner itself) is executed with the current Python interpreter,
one file per subprocess, so each suite gets a clean import state. Results are
reported per file followed by a total summary; the exit code is non-zero if
any test file fails or times out. The per-file timeout can be overridden with
the ``TEST_TIMEOUT`` environment variable (default 300 seconds).
"""
import glob
import os
import subprocess
import sys

_CHECK = "[check]"


def run_test(path, timeout=300):
    """Run a single test file in a subprocess.

    Returns ``(status, returncode, output)`` where *status* is one of
    ``"PASS"``, ``"SKIP"``, ``"TIMEOUT"`` or ``"FAIL"``.
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
    # Convention: suites print a line starting with "SKIP" (e.g. "SKIP: ffmpeg
    # not installed") when optional deps are missing. Match such a line rather
    # than the bare substring, so prose like "skipped gracefully" in a passing
    # check name is not misread as a skip.
    for line in out.splitlines():
        if line.lstrip().startswith("SKIP"):
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
        print(f"{_CHECK} RUN  {name} ...", flush=True)
        status, rc, out = run_test(path, timeout=timeout)
        tail = ""
        if out:
            lines = [ln for ln in out.strip().splitlines() if ln.strip()]
            if not short:
                tail = lines[-1] if lines else ""
            elif status != "PASS":
                markers = [ln for ln in lines if "FAIL" in ln or "SKIP" in ln]
                tail = markers[0] if markers else lines[-1]
        results.append((name, status))
        print(f"{_CHECK} {status} {name}" + (f" | {tail}" if tail else ""), flush=True)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "TIMEOUT": 0}
    for _, status in results:
        counts[status] = counts.get(status, 0) + 1
    print(f"{_CHECK} Summary: {len(results)} file(s)")
    for name, status in results:
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
