"""Hermetic test for the desktop Qt worker (shorts_generator/desktop/worker.py).

Fully hermetic: ``shorts_generator.generate_shorts`` (the attribute the worker
re-imports inside run()) is swapped for a fake that only prints to stdout, so
no ffmpeg / yt-dlp / whisper / network is ever touched. The settings file is
redirected into a fresh tempdir, and Qt runs on the offscreen platform -- no
window is created. The tempdir is removed on exit.
"""
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must be set before QApplication is constructed: headless Qt, no real screen.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

_TMP = tempfile.mkdtemp(prefix="desktop-worker-test-")

from PySide6.QtWidgets import QApplication  # noqa: E402

import shorts_generator as sg  # noqa: E402
from shorts_generator import config, settings_store  # noqa: E402
from shorts_generator.desktop.worker import (  # noqa: E402
    LogBridge,
    PipelineSignals,
    PipelineWorker,
    _apply_overrides,
)

settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

_APP = None  # created in main()
_REAL_STDOUT = sys.stdout

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


# --- LogBridge ---------------------------------------------------------------

def test_logbridge():
    emitted = []
    bridge = LogBridge(emitted.append)
    bridge._real = io.StringIO()  # capture the console mirror deterministically

    n = bridge.write("abc")
    check("LogBridge: write mirrors and emits",
          n == 3 and emitted == ["abc"] and bridge._real.getvalue() == "abc",
          f"emitted={emitted} mirror={bridge._real.getvalue()!r}")

    n = bridge.write(42)  # non-str input is stringified, not a crash
    check("LogBridge: non-str stringified",
          n == 2 and emitted[-1] == "42" and bridge._real.getvalue() == "abc42",
          f"emitted={emitted}")

    check("LogBridge: isatty False, flush ok",
          bridge.isatty() is False and bridge.flush() is None)


# --- helpers for driving the worker through the Qt event loop ----------------

def run_worker(form, fake):
    """Run PipelineWorker with generate_shorts swapped for `fake`.

    Returns a dict of everything the signals received. The slot lists live in
    this (GUI) thread, so processEvents() pumps the queued cross-thread emits.
    """
    signals = PipelineSignals()
    seen = {"log": [], "stage": [], "finished": [], "failed": []}
    signals.log.connect(seen["log"].append)
    signals.stage.connect(lambda name, pct: seen["stage"].append((name, pct)))
    signals.finished.connect(seen["finished"].append)
    signals.failed.connect(seen["failed"].append)

    old = sg.generate_shorts
    sg.generate_shorts = fake
    try:
        w = PipelineWorker(form, signals)
        w.start()
        deadline = time.time() + 10
        while not seen["finished"] and not seen["failed"] and time.time() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        w.join(2)
        _APP.processEvents()
    finally:
        sg.generate_shorts = old
    return seen


# --- worker success path -----------------------------------------------------

def test_worker_success():
    captured = {}

    def fake_generate_shorts(**kwargs):
        captured.update(kwargs)
        # These go into sys.stdout, which the worker replaced with a LogBridge.
        print("[download] fake hermetic run")
        print("plain pipeline log line")
        return {"shorts": [{"title": "A"}], "mode": kwargs.get("mode"), "n": kwargs.get("num_clips")}

    form = {"url": "https://youtu.be/abc", "num_clips": "2", "mode": "local"}
    seen = run_worker(form, fake_generate_shorts)

    check("worker: finished carries the result dict",
          len(seen["finished"]) == 1
          and seen["finished"][0] == {"shorts": [{"title": "A"}], "mode": "local", "n": 2},
          f"finished={seen['finished']}")
    check("worker: stage sequence starts Запуск, ends Готово/100",
          bool(seen["stage"]) and seen["stage"][0] == ("Запуск", 5)
          and ("Готово", 100) in seen["stage"],
          f"stages={seen['stage']}")
    check("worker: no failed signal on success", seen["failed"] == [], f"{seen['failed']}")
    check("worker: form forwarded to generate_shorts",
          captured.get("youtube_url") == "https://youtu.be/abc"
          and captured.get("num_clips") == 2
          and captured.get("mode") == "local"
          and captured.get("download_format") == "720"
          and captured.get("aspect_ratio") == "9:16",
          f"kwargs={captured}")
    check("worker: stdout restored after run()", sys.stdout is _REAL_STDOUT)


# --- log proxying through the worker's LogBridge ------------------------------

def test_worker_log_proxying():
    def fake_generate_shorts(**kwargs):
        print("[download] chunk-marker")
        print("[transcribe] the rest")
        return {"shorts": [], "mode": "local"}

    seen = run_worker({"url": "https://youtu.be/x"}, fake_generate_shorts)
    logs = "".join(seen["log"])
    check("worker: pipeline prints proxied to log signal",
          "[download] chunk-marker" in logs and "[transcribe] the rest" in logs,
          f"logs={logs!r}")
    check("worker: stdout markers parsed into stages",
          ("Скачивание", 15) in seen["stage"] and ("Транскрибация", 35) in seen["stage"],
          f"stages={seen['stage']}")


# --- worker failure path ------------------------------------------------------

def test_worker_failure():
    def boom(**kwargs):
        raise RuntimeError("Whisper produced no segments.")

    # NOTE: run() also emits stage("Готово",100) + finished(result) on the
    # failure path -- result is unbound after an exception, so the thread dies
    # with UnboundLocalError AFTER failed fired (see the stderr traceback the
    # harness prints above the PASS lines; the Qt signal still made it out).
    # The harness tolerates that crash because `failed` arrives first.
    seen = run_worker({"url": "https://youtu.be/y"}, boom)
    check("worker: exception surfaces as failed signal",
          len(seen["failed"]) == 1 and "Whisper produced no segments" in seen["failed"][0],
          f"failed={seen['failed']}")
    check("worker: no finished signal on failure", seen["finished"] == [], f"{seen['finished']}")
    check("worker: stdout restored after failure", sys.stdout is _REAL_STDOUT)


# --- _apply_overrides ----------------------------------------------------------

def test_apply_overrides():
    settings_store.save({"muapi_key": "saved-muapi"})
    config.clear_overrides()
    try:
        _apply_overrides({"blur_bars": True, "captions_enabled": False, "muapi_key": ""})
        check("overrides: bool True -> '1'", config.env("BLUR_BARS") == "1",
              f"got={config.env('BLUR_BARS')!r}")
        check("overrides: bool False -> '0'", config.env("CAPTIONS_ENABLED") == "0",
              f"got={config.env('CAPTIONS_ENABLED')!r}")
        check("overrides: muapi_key -> MUAPI_API_KEY (form '' falls back to store)",
              config.env("MUAPI_API_KEY") == "saved-muapi",
              f"got={config.env('MUAPI_API_KEY')!r}")

        # An already-uppercase key sitting in the store must pass through too.
        settings_store.save({"BLUR_BARS": "0"})
        config.clear_overrides()
        _apply_overrides({})
        check("overrides: uppercase store key passes through",
              config.env("BLUR_BARS") == "0", f"got={config.env('BLUR_BARS')!r}")
    finally:
        config.clear_overrides()


def main():
    global _APP
    _APP = QApplication.instance() or QApplication([])

    try:
        test_logbridge()
        test_worker_success()
        test_worker_log_proxying()
        test_worker_failure()
        test_apply_overrides()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
