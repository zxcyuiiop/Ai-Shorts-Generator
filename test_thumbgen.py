"""Checks for local/thumbgen.py and the /api/shorts/thumbnail endpoint.

Everything is hermetic: subprocess.run is stubbed (stubs create the output
file like real ffmpeg would), and the endpoint is exercised through the Flask
test client against a scratch dir INSIDE output/ (the resolver refuses paths
outside the output dir, so a system temp dir cannot be used).
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator.local import thumbgen
from shorts_generator import settings_store

# Scratch settings so a real settings.local.json is never read/touched.
settings_store.SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.thumbtest.json")

import app as webapp

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class StubRun:
    """subprocess.run stand-in: ffprobe reports a fixed duration, ffmpeg
    'extracts' by creating the output file. Records every argv."""

    def __init__(self, duration="30.0", fail_times=0):
        self.duration = duration
        self.fail_times = fail_times  # first N ffmpeg calls exit non-zero
        self.calls = []

    def __call__(self, cmd, capture_output=False, text=False, timeout=None, **kw):
        self.calls.append(list(cmd))
        exe = os.path.basename(str(cmd[0])).lower()
        if "ffprobe" in exe:
            return FakeProc(0, f"{self.duration}\n")
        if self.fail_times > 0:
            self.fail_times -= 1
            return FakeProc(1, "", "SimulatedFontError: drawtext blew up")
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fakejpeg")
        return FakeProc(0)


def ffmpeg_cmds(stub):
    return [c for c in stub.calls if "ffprobe" not in os.path.basename(str(c[0])).lower()]


def run_unit_checks():
    tmp = tempfile.mkdtemp(prefix="thumbgen-unit-")
    try:
        video = os.path.join(tmp, "clip_01.mp4")
        with open(video, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")

        # --- плоский кадр: сборка команды -------------------------------
        stub = StubRun(duration="30.0")
        orig = thumbgen.subprocess.run
        thumbgen.subprocess.run = stub
        try:
            out = thumbgen.make_thumbnail(video, title=False)
        finally:
            thumbgen.subprocess.run = orig

        check("no-title: output returned & exists",
              out == os.path.join(tmp, "clip_01_thumb.jpg") and os.path.isfile(out), out)
        cmds = ffmpeg_cmds(stub)
        check("no-title: exactly one ffmpeg call", len(cmds) == 1, f"calls={len(stub.calls)}")
        cmd = cmds[0] if cmds else []
        check("cmd: -ss before -i (fast seek)",
              cmd.index("-ss") < cmd.index("-i"), str(cmd[:8]))
        # 30s * 12% = 3.6s
        check("cmd: offset = duration * 12%", cmd[cmd.index("-ss") + 1] == "3.600", str(cmd))
        vf = cmd[cmd.index("-vf") + 1] if "-vf" in cmd else ""
        check("cmd: scale min(1080,iw):-2", "min(1080,iw)" in vf and ":-2" in vf, vf)
        check("cmd: no drawtext without title", "drawtext" not in vf, vf)
        check("cmd: -frames:v 1", "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1")
        check("cmd: -q:v 3", "-q:v" in cmd and cmd[cmd.index("-q:v") + 1] == "3")

        # --- клэмп процента ---------------------------------------------
        def offset_for(pct):
            stub2 = StubRun(duration="10.0")
            thumbgen.subprocess.run = stub2
            try:
                thumbgen.make_thumbnail(video, out_path=os.path.join(tmp, f"p{pct}.jpg"),
                                        title=False, at_percent=pct)
            finally:
                thumbgen.subprocess.run = orig
            c = ffmpeg_cmds(stub2)[0]
            return c[c.index("-ss") + 1]

        check("clamp: 500% -> 90%", offset_for(90 if False else 500) == "9.000", "10s*0.9")
        check("clamp: -5% -> 1%", offset_for(-5) == "0.100", "10s*0.01")
        check("clamp: garbage -> 12%", offset_for("nope") == "1.200", "10s*0.12")
        check("clamp helper direct", thumbgen._clamp_percent(45) == 45.0
              and thumbgen._clamp_percent(0) == 1.0 and thumbgen._clamp_percent(91) == 90.0)

        # --- заголовок: drawtext в фильтре -------------------------------
        stub = StubRun()
        thumbgen.subprocess.run = stub
        try:
            thumbgen.make_thumbnail(video, out_path=os.path.join(tmp, "titled.jpg"),
                                    title="Большой заголовок: тест, 100%")
        finally:
            thumbgen.subprocess.run = orig
        cmd = ffmpeg_cmds(stub)[0]
        vf = cmd[cmd.index("-vf") + 1]
        check("title: drawtext present", "drawtext=" in vf, vf[:120])
        check("title: ':' escaped", "\\:" in vf)
        check("title: '%' escaped", "\\%" in vf)
        check("title: ',' escaped", "\\," in vf)
        check("title: centered x", "x=(w-text_w)/2" in vf)
        check("title: ~28% from top", "y=h*0.28" in vf)

        # --- перенос строк заголовка --------------------------------------
        wrapped = thumbgen._wrap_title("слово " * 20)
        lines = wrapped.split("\n")
        check("wrap: <=3 lines", len(lines) <= 3, f"lines={len(lines)}")
        check("wrap: <=22 chars/line", all(len(l) <= 22 for l in lines),
              str([len(l) for l in lines]))
        check("escape: backslash first", thumbgen._drawtext_escape("a\\b") == "a\\\\b")
        check("escape: apostrophes dropped", "'" not in thumbgen._drawtext_escape("it's"))

        # --- drawtext падает -> фолбэк на чистый кадр ---------------------
        calls_before = None
        stub = StubRun(fail_times=3)  # DejaVu, Arial, default font — все падают
        thumbgen.subprocess.run = stub
        try:
            out = thumbgen.make_thumbnail(video, out_path=os.path.join(tmp, "fb.jpg"),
                                          title="Заголовок")
        finally:
            thumbgen.subprocess.run = orig
        cmds = ffmpeg_cmds(stub)
        check("fallback: 3 titled attempts + 1 plain", len(cmds) == 4, f"n={len(cmds)}")
        check("fallback: last call has no drawtext and produced file",
              "drawtext" not in cmds[-1][cmds[-1].index("-vf") + 1] and os.path.isfile(out))

        # --- нейминг: _thumb_N, ничего не перезаписываем -------------------
        # Use a fresh clip in a clean subdir so no earlier _thumb files exist.
        nsub = os.path.join(tmp, "naming")
        os.makedirs(nsub)
        nclip = os.path.join(nsub, "clip_01.mp4")
        with open(nclip, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")
        stub = StubRun()
        thumbgen.subprocess.run = stub
        try:
            first = thumbgen.make_thumbnail(nclip, title=False)
            second = thumbgen.make_thumbnail(nclip, title=False)
        finally:
            thumbgen.subprocess.run = orig
        check("naming: first is _thumb.jpg",
              first.endswith("clip_01_thumb.jpg"), first)
        check("naming: second is _thumb_2.jpg (no overwrite)",
              second.endswith("clip_01_thumb_2.jpg") and second != first, second)

        # --- env THUMB_AT_PERCENT / THUMB_TITLE ---------------------------
        from shorts_generator.config import set_overrides, clear_overrides
        set_overrides({"THUMB_AT_PERCENT": "50", "THUMB_TITLE": "Из настроек"})
        stub = StubRun(duration="10.0")
        thumbgen.subprocess.run = stub
        try:
            thumbgen.make_thumbnail(video, out_path=os.path.join(tmp, "env.jpg"))
        finally:
            thumbgen.subprocess.run = orig
            clear_overrides()
        cmd = ffmpeg_cmds(stub)[0]
        check("env: THUMB_AT_PERCENT honoured", cmd[cmd.index("-ss") + 1] == "5.000")
        check("env: THUMB_TITLE honoured",
              "drawtext" in cmd[cmd.index("-vf") + 1])
        check("env: no THUMB_TITLE env -> plain frame",
              thumbgen._resolve_title(None) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_endpoint_checks():
    c = webapp.app.test_client()

    # Scratch must live under output/ so _url_to_output_path can resolve it.
    base = os.path.abspath(webapp.LOCAL_OUTPUT_DIR)
    os.makedirs(base, exist_ok=True)  # a fresh checkout has no output/ yet
    tmp = tempfile.mkdtemp(prefix="thumbgen-api-", dir=os.path.join(base, "uploads") if os.path.isdir(os.path.join(base, "uploads")) else base)
    rel = os.path.relpath(tmp, base).replace("\\", "/")
    try:
        clip = os.path.join(tmp, "short_01.mp4")
        with open(clip, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")
        url = f"/output/{rel}/short_01.mp4"

        stub = StubRun(duration="20.0")
        orig_run = thumbgen.subprocess.run
        thumbgen.subprocess.run = stub
        try:
            r = c.post("/api/shorts/thumbnail", json={"url": url, "title": "Тайтл"})
            body = r.get_json() or {}
            check("endpoint: 200 + ok + url", r.status_code == 200 and body.get("ok") is True
                  and str(body.get("url", "")).startswith(f"/output/{rel}/short_01_thumb"),
                  f"status={r.status_code} body={body}")
            thumb_rel = str(body.get("url", ""))[len("/output/"):]
            check("endpoint: file on disk",
                  os.path.isfile(os.path.join(base, *thumb_rel.split("/"))), thumb_rel)
            cmds = ffmpeg_cmds(stub)
            check("endpoint: title reached drawtext",
                  bool(cmds) and "drawtext" in cmds[0][cmds[0].index("-vf") + 1])

            # 400: не /output/-путь и traversal.
            r = c.post("/api/shorts/thumbnail", json={"url": "https://cdn.example.com/x.mp4"})
            check("endpoint: non-/output url -> 400", r.status_code == 400, f"status={r.status_code}")
            r = c.post("/api/shorts/thumbnail", json={"url": "/output/../escape.mp4"})
            check("endpoint: traversal -> 400", r.status_code == 400, f"status={r.status_code}")

            # 404: файл отсутствует.
            r = c.post("/api/shorts/thumbnail", json={"url": f"/output/{rel}/nope.mp4"})
            check("endpoint: missing file -> 404", r.status_code == 404, f"status={r.status_code}")
        finally:
            thumbgen.subprocess.run = orig_run

        # 503: ffmpeg недоступен (stubbed which, restore right away).
        orig_which = shutil.which
        shutil.which = lambda name: None
        try:
            r = c.post("/api/shorts/thumbnail", json={"url": url})
            check("endpoint: ffmpeg missing -> 503", r.status_code == 503, f"status={r.status_code}")
        finally:
            shutil.which = orig_which
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.exists(settings_store.SETTINGS_PATH):
            os.remove(settings_store.SETTINGS_PATH)


def main():
    run_unit_checks()
    run_endpoint_checks()
    print("THUMBGEN TESTS OK" if not failures else f"THUMBGEN FAILURES: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
