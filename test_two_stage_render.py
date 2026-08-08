"""Checks for the two-stage render: draft clips skip effects, finalize applies them.

Covers:
  - crop_clip_local(finalize=False) never touches finalize_clip_local / blur / overlay / music
  - crop_clip_local(finalize=True) calls finalize_clip_local with (out_path, aspect_ratio)
  - crop_highlights_local(finalize=...) propagates the flag down
  - POST /api/shorts/finalize: 200 ok / 400 bad url / 404 missing file /
    500 + draft restored when finalize raises (all with a stubbed finalize_clip_local)

Everything is stubbed: no network, no ffmpeg, no real rendering.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator import settings_store  # noqa: E402

# Use a scratch settings file so a real one is never touched.
settings_store.SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.test.json"
)

from shorts_generator import config as cfg  # noqa: E402
from shorts_generator.local import clipper as clip  # noqa: E402
import app as webapp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


class Recorder:
    """Stand-in that records its calls."""

    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fn is not None:
            return self.fn(*args, **kwargs)
        return None


def run_clipper_checks():
    tmp = tempfile.mkdtemp(prefix="two-stage-render-")
    try:
        src = os.path.join(tmp, "source.mp4")
        out_path = os.path.join(tmp, "short_01.mp4")
        with open(src, "wb") as f:
            f.write(b"fake")

        # Keep the real ffmpeg/silence/encoder work out: we only care whether the
        # finalize stage fires, so every heavy step around it is stubbed.
        reals = {name: getattr(clip, name) for name in (
            "_cut_subclip", "_reframe_vertical", "finalize_clip_local",
            "_overlay_tiktok", "apply_blur_padding", "mix_music",
        )}
        try:
            clip._cut_subclip = Recorder()
            clip._reframe_vertical = Recorder()
            fin = Recorder()
            over = Recorder()
            blur = Recorder()
            music = Recorder()
            clip.finalize_clip_local = fin
            clip._overlay_tiktok = over
            clip.apply_blur_padding = blur
            clip.mix_music = music
            # Skip the silence-detection branch without touching ffmpeg, and keep
            # the encoder auto-detect from probing nvidia-smi/ffmpeg either.
            cfg.set_overrides({"SILENCE_CUT": "0", "FORCE_CPU_FFMPEG": "1"})

            # 1. Draft render: no effects at all.
            clip.crop_clip_local(src, 0.0, 5.0, "9:16", out_path, finalize=False)
            check("draft skips finalize_clip_local", fin.calls == [], f"calls={fin.calls}")
            check("draft skips overlay", over.calls == [], f"calls={over.calls}")
            check("draft skips blur padding", blur.calls == [], f"calls={blur.calls}")
            check("draft skips music mix", music.calls == [], f"calls={music.calls}")

            # 2. finalize=True runs the finalize stage with out_path + aspect.
            fin.calls.clear()
            clip.crop_clip_local(src, 0.0, 5.0, "9:16", out_path, finalize=True)
            check("finalize=True calls finalize_clip_local once", len(fin.calls) == 1,
                  f"calls={fin.calls}")
            check("finalize got (out_path, aspect_ratio)",
                  bool(fin.calls) and fin.calls[0][0] == (out_path, "9:16"),
                  f"calls={fin.calls}")
        finally:
            for name, fn in reals.items():
                setattr(clip, name, fn)
            cfg.clear_overrides()

        # 3. crop_highlights_local(finalize=False) propagates to every crop call.
        real_crop = clip.crop_clip_local
        try:
            rec = Recorder(fn=lambda *a, **k: a[4])
            clip.crop_clip_local = rec
            clip.crop_highlights_local(src, [{"title": "A", "start_time": 0, "end_time": 5}],
                                       aspect_ratio="1:1", out_dir=tmp, finalize=False)
            ok = len(rec.calls) == 1 and rec.calls[0][1].get("finalize") is False
            check("crop_highlights_local propagates finalize=False", ok, f"calls={rec.calls}")
        finally:
            clip.crop_clip_local = real_crop
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_finalize_endpoint_checks():
    c = webapp.app.test_client()

    real = clip.finalize_clip_local
    # Scratch must live under output/ so _resolve_output_safe can resolve it
    # (a system temp dir may sit on another drive than the output dir).
    tmp = tempfile.mkdtemp(prefix="two-stage-finalize-", dir=os.path.abspath(webapp.UPLOAD_DIR))
    try:
        # Keep the effects out of ffmpeg but make the failure case visible on disk:
        # the stub denies the draft bytes, so a successful restore is checkable.
        def raise_and_deface(path, aspect):
            with open(path, "wb") as f:
                f.write(b"defaced")
            raise RuntimeError("effects exploded")

        stub = Recorder(fn=raise_and_deface)
        clip.finalize_clip_local = stub

        # A real draft in the output dir to finalize hermetically.
        rel = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        draft_rel = f"{rel}/draft_01.mp4"
        draft_abs = os.path.join(tmp, "draft_01.mp4")
        with open(draft_abs, "wb") as f:
            f.write(b"draftbytes")

        # Bad url -> 400 (not an /output/ path), stub untouched.
        r = c.post("/api/shorts/finalize", json={"url": "https://cdn.example.com/x.mp4"})
        check("finalize: non-/output url rejected", r.status_code == 400, f"status={r.status_code}")
        r = c.post("/api/shorts/finalize", json={"url": "/output/../escape.mp4"})
        check("finalize: traversal rejected", r.status_code == 400, f"status={r.status_code}")

        # Missing file -> 404.
        r = c.post("/api/shorts/finalize", json={"url": f"/output/{rel}/nope.mp4"})
        check("finalize: missing file -> 404", r.status_code == 404, f"status={r.status_code}")
        check("finalize: stub untouched by 400/404", stub.calls == [], f"calls={stub.calls}")

        # Happy path -> 200 + {ok: true}, stub called with the resolved path/aspect.
        stub.fn = None
        r = c.post("/api/shorts/finalize",
                   json={"url": f"/output/{draft_rel}", "aspect_ratio": "1:1"})
        body = r.get_json() or {}
        check("finalize: valid draft -> 200 ok", r.status_code == 200 and body.get("ok") is True,
              f"status={r.status_code} body={body}")
        ok = (len(stub.calls) == 1 and stub.calls[0][0][0] == os.path.realpath(draft_abs)
              and stub.calls[0][0][1] == "1:1")
        check("finalize: stub called with (abs_path, aspect_ratio)", ok, f"calls={stub.calls}")
        check("finalize: draft backup kept",
              os.path.isfile(draft_abs + ".draft.mp4"),
              draft_abs + ".draft.mp4")

        # Failing finalize -> 500 {error}, and the draft bytes are restored.
        stub.calls.clear()
        stub.fn = raise_and_deface
        r = c.post("/api/shorts/finalize",
                   json={"url": f"/output/{draft_rel}", "aspect_ratio": "9:16"})
        body = r.get_json() or {}
        check("finalize: raising stub -> 500 {error}",
              r.status_code == 500 and "effects exploded" in (body.get("error") or ""),
              f"status={r.status_code} body={body}")
        with open(draft_abs, "rb") as f:
            restored = f.read()
        check("finalize: draft restored from backup on failure", restored == b"draftbytes",
              f"bytes={restored!r}")
    finally:
        clip.finalize_clip_local = real
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    run_clipper_checks()
    run_finalize_endpoint_checks()

    if os.path.exists(settings_store.SETTINGS_PATH):
        os.remove(settings_store.SETTINGS_PATH)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
