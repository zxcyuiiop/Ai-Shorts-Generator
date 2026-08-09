"""Checks for the opt-in burned-in captions (karaoke/classic ASS) slice.

Covers:
  - captions.captions_enabled() env gating (default off, "1"/"true" on)
  - caption_settings_from_env(): style whitelist, colour channel flip, int clamps
  - remap_words(): silence-gap removal re-times words, words in gaps dropped
  - write_caption_ass(): karaoke \\k tags, classic style, no-words -> None,
    PlayRes/styles block present
  - transcriber: word_timestamps only requested when CAPTIONS_ENABLED is on
  - clipper.crop_clip_local: caption sidecar written when captions on + transcript
    given; skipped silently when off; FACE_TRACK_ENABLED=0 forces centre crop
  - app.py plumbing: generate() stores the 3 keys and _overrides_from maps them
    (CAPTIONS_ENABLED / CAPTION_STYLE / FACE_TRACK_ENABLED)

ffmpeg and faster-whisper are stubbed throughout: no real rendering happens.
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
from shorts_generator.local import captions as cap  # noqa: E402
from shorts_generator.local import clipper as clip  # noqa: E402
from shorts_generator.local import transcriber as tr  # noqa: E402
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


def run_env_checks():
    # Default is OFF with no overrides and no env -- opt-in by design.
    cfg.clear_overrides()
    check("captions default off", cap.captions_enabled() is False)
    cfg.set_overrides({"CAPTIONS_ENABLED": "1"})
    check("CAPTIONS_ENABLED=1 turns on", cap.captions_enabled() is True)
    cfg.set_overrides({"CAPTIONS_ENABLED": "0"})
    check("CAPTIONS_ENABLED=0 overrides win (falsy-but-present)",
          cap.captions_enabled() is False)
    cfg.clear_overrides()

    s = cap.caption_settings_from_env()
    check("default style is karaoke", s["style"] == "karaoke", s["style"])
    check("default font_size clamped sane", 8 <= s["font_size"] <= 300,
          str(s["font_size"]))
    # The default hex is already ASS-ordered BGR, so _color must pass it
    # through untouched (a misread here would double-reverse the channels).
    check("gold active colour kept as ASS BGR",
          s["active_color"] == "&H0000D7FF", s["active_color"])
    cfg.set_overrides({"CAPTION_TEXT_COLOR": "112233"})
    check("RRGGBB input is flipped to &HAABBGGRR",
          cap.caption_settings_from_env()["text_color"] == "&H00332211",
          cap.caption_settings_from_env()["text_color"])
    cfg.clear_overrides()

    cfg.set_overrides({"CAPTION_STYLE": "bogus", "CAPTION_FONT_SIZE": "9999"})
    s = cap.caption_settings_from_env()
    check("bogus style falls back to karaoke", s["style"] == "karaoke", s["style"])
    check("font_size clamped to 300", s["font_size"] == 300, str(s["font_size"]))
    cfg.set_overrides({"CAPTION_STYLE": "classic"})
    check("classic style accepted",
          cap.caption_settings_from_env()["style"] == "classic")
    cfg.clear_overrides()

    # Position knob: default bottom, whitelist, bogus falls back.
    check("default position is bottom",
          cap.caption_settings_from_env()["position"] == "bottom")
    cfg.set_overrides({"CAPTION_POSITION": "TOP"})
    check("position accepts top (case-insensitive)",
          cap.caption_settings_from_env()["position"] == "top")
    cfg.set_overrides({"CAPTION_POSITION": "bogus"})
    check("bogus position falls back to bottom",
          cap.caption_settings_from_env()["position"] == "bottom")
    cfg.clear_overrides()

    # The position drives the ASS Alignment field in both style lines
    # (2=bottom, 5=center, 8=top on the numpad layout).
    base = cap.caption_settings_from_env()
    for pos, align in (("bottom", 2), ("center", 5), ("top", 8)):
        header = cap._header({**base, "position": pos})
        check(f"position={pos} -> Alignment {align} in both styles",
              header.count(f"3,1,{align},60,60,") == 2,
              [ln for ln in header.splitlines() if ln.startswith("Style:")][0])


def run_remap_checks():
    # kept segments: [0..4] and [8..12] -- the [4..8] gap is removed.
    kept = [(0.0, 4.0), (8.0, 12.0)]
    words = [
        {"start": 1.0, "end": 2.0, "word": "hello"},   # inside seg 0 -> shift 0
        {"start": 5.0, "end": 6.0, "word": "noise"},   # inside the gap -> dropped
        {"start": 8.5, "end": 9.5, "word": "world"},   # seg 1 -> shift left by 4
    ]
    out = cap.remap_words(words, kept)
    check("gap word dropped", len(out) == 2, str(out))
    check("first word unmoved", out and out[0]["start"] == 1.0 and out[0]["end"] == 2.0,
          str(out[:1]))
    check("second word shifted left by removed duration",
          len(out) > 1 and abs(out[1]["start"] - 4.5) < 1e-9
          and abs(out[1]["end"] - 5.5) < 1e-9, str(out[1:]))
    check("empty kept segments -> no words", cap.remap_words(words, []) == [])


def run_ass_writer_checks():
    tmp = tempfile.mkdtemp(prefix="captions-ass-")
    try:
        transcript = {
            "segments": [{
                "start": 0.0, "end": 10.0, "text": "one two three four five",
                "words": [
                    {"start": 0.0, "end": 0.4, "word": "one"},
                    {"start": 0.5, "end": 0.9, "word": "two"},
                    {"start": 1.0, "end": 1.4, "word": "three"},
                    {"start": 1.5, "end": 1.9, "word": "four"},
                    {"start": 3.0, "end": 3.4, "word": "five"},
                ],
            }]
        }

        # Karaoke: \\k tags, max 4 words/line -> "five" lands on a second line
        # (the 1.1s pause before it would force a break anyway).
        ass_path = os.path.join(tmp, "clip.mp4.ass")
        cfg.set_overrides({"CAPTIONS_ENABLED": "1"})
        written = cap.write_caption_ass(transcript, 0.0, 10.0, ass_path)
        check("karaoke sidecar written", written == ass_path, str(written))
        with open(ass_path, "r", encoding="utf-8") as fh:
            body = fh.read()
        check("ASS has PlayRes 1080x1920", "PlayResX: 1080" in body and "PlayResY: 1920" in body)
        check("karaoke \\k tags present", "{\\k40}one" in body, body[body.find("Dialogue"):][:80])
        check("word budget splits lines", body.count("Dialogue:") == 2,
              str(body.count("Dialogue:")))
        check("Karaoke style referenced", "Dialogue: 0,0:00:00.00,0:00:01.90,Karaoke" in body)
        check("line two rebased to own start", "{\\k40}five" in body)

        # Classic: no \\k tags, plain joined words.
        classic_path = os.path.join(tmp, "clip2.mp4.ass")
        cap.write_caption_ass(transcript, 0.0, 10.0, classic_path, style="classic")
        with open(classic_path, "r", encoding="utf-8") as fh:
            cbody = fh.read()
        check("classic has no karaoke tags", "{\\k" not in cbody)
        check("classic line joins words", "one two three four" in cbody)

        # ASS override braces in words get stripped, not emitted.
        tr_evil = {"segments": [{"start": 0.0, "end": 1.0, "text": "{\\x}",
                                 "words": [{"start": 0.0, "end": 0.5,
                                            "word": "{\\i1}hack"}]}]}
        evil_path = os.path.join(tmp, "evil.mp4.ass")
        cap.write_caption_ass(tr_evil, 0.0, 1.0, evil_path)
        with open(evil_path, "r", encoding="utf-8") as fh:
            ebody = fh.read()
        check("override braces stripped from words", "Dialogue: 0,0:00:00.00,0:00:00.50,Karaoke,,0,0,0,,{\\k50}\\i1hack" in ebody.replace("{}\\i1", "\\i1"), ebody[ebody.find("Dialogue"):][:90] if "Dialogue" in ebody else "<no event>")

        # No words in the window -> None and no file.
        empty_path = os.path.join(tmp, "none.mp4.ass")
        res = cap.write_caption_ass({"segments": []}, 0.0, 5.0, empty_path)
        check("no words -> None, no sidecar",
              res is None and not os.path.exists(empty_path))

        # Sidecar timing rebased: word at source t=20..20.4 in a clip starting
        # at t=20 appears at 0.0 in the caption.
        tr2 = {"segments": [{"start": 0.0, "end": 99.0, "text": "late",
                             "words": [{"start": 20.0, "end": 20.4, "word": "late"}]}]}
        late_path = os.path.join(tmp, "late.mp4.ass")
        cap.write_caption_ass(tr2, 20.0, 30.0, late_path)
        with open(late_path, "r", encoding="utf-8") as fh:
            lbody = fh.read()
        check("clip window rebased to 0", "0:00:00.00,0:00:00.40" in lbody,
              lbody[lbody.find("Dialogue"):][:70] if "Dialogue" in lbody else "<none>")
    finally:
        cfg.clear_overrides()
        shutil.rmtree(tmp, ignore_errors=True)


def run_transcriber_checks():
    """word_timestamps must reach whisper only when captions are on; words get
    drained into plain dicts when present."""
    import types

    calls = []

    class FakeWord:
        def __init__(self, w, s, e):
            self.word, self.start, self.end = w, s, e

    class FakeSeg:
        start, end, text = 0.0, 1.0, "hello there"

        def __init__(self, with_words):
            self.words = [FakeWord("hello", 0.0, 0.4),
                          FakeWord(" there", 0.5, 0.9)] if with_words else None

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, **kwargs):
            calls.append(kwargs)
            want = kwargs.get("word_timestamps", False)
            return [FakeSeg(want)], type("I", (), {"language": "en"})()

    # WhisperModel is imported lazily inside transcribe_local, so stub the
    # faster_whisper module in sys.modules for the duration of the check.
    real_fw = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=FakeModel)
    try:
        # Force the model path (cache miss): fresh temp media + LOCAL_OUTPUT_DIR
        # pointed at the same temp dir so the .srt cache lives there too.
        tmp = tempfile.mkdtemp(prefix="captions-whisper-")
        media = os.path.join(tmp, "nope.mp4")
        with open(media, "wb") as f:
            f.write(b"fake")
        srt = os.path.join(tmp, "nope.srt")

        base = {"LOCAL_WHISPER_DEVICE": "cpu", "LOCAL_OUTPUT_DIR": tmp}
        cfg.set_overrides({**base, "CAPTIONS_ENABLED": "0"})
        tr.transcribe_local(media, language="en")
        check("captions off -> no word_timestamps",
              calls and not calls[-1].get("word_timestamps", False), str(calls[-1]))

        # Drop the fresh .srt cache so the next call actually runs the model.
        if os.path.exists(srt):
            os.remove(srt)

        cfg.set_overrides({**base, "CAPTIONS_ENABLED": "1"})
        result = tr.transcribe_local(media, language="en")
        check("captions on -> word_timestamps requested",
              calls and calls[-1].get("word_timestamps") is True, str(calls[-1]))
        segs = result.get("segments") or []
        check("words drained into segment dicts",
              bool(segs) and segs[0].get("words")
              and segs[0]["words"][0]["word"] == "hello"
              and abs(segs[0]["words"][0]["start"] - 0.0) < 1e-9,
              str(segs[:1]))
    finally:
        if real_fw is not None:
            sys.modules["faster_whisper"] = real_fw
        else:
            sys.modules.pop("faster_whisper", None)
        cfg.clear_overrides()
        shutil.rmtree(tmp, ignore_errors=True)


def run_clipper_checks():
    tmp = tempfile.mkdtemp(prefix="captions-clip-")
    try:
        src = os.path.join(tmp, "source.mp4")
        out_path = os.path.join(tmp, "short_01.mp4")
        with open(src, "wb") as f:
            f.write(b"fake")
        transcript = {"segments": [{"start": 0.0, "end": 5.0, "text": "hi",
                                    "words": [{"start": 0.0, "end": 0.4, "word": "hi"}]}]}

        reals = {name: getattr(clip, name) for name in (
            "_cut_subclip", "_reframe_vertical", "finalize_clip_local",
            "_reframe_with_ffmpeg", "_load_face_detector",
        )}
        try:
            clip._cut_subclip = Recorder()
            clip._reframe_vertical = Recorder()
            clip.finalize_clip_local = Recorder()
            cfg.set_overrides({"SILENCE_CUT": "0", "FORCE_CPU_FFMPEG": "1",
                               "CAPTIONS_ENABLED": "0"})

            # Captions off: no sidecar even with a transcript.
            clip.crop_clip_local(src, 0.0, 5.0, "9:16", out_path,
                                 finalize=False, transcript=transcript)
            check("captions off -> no sidecar", not os.path.exists(out_path + ".ass"))

            # Captions on + transcript: sidecar appears next to the draft.
            cfg.set_overrides({"SILENCE_CUT": "0", "FORCE_CPU_FFMPEG": "1",
                               "CAPTIONS_ENABLED": "1"})
            clip.crop_clip_local(src, 0.0, 5.0, "9:16", out_path,
                                 finalize=False, transcript=transcript)
            check("captions on + transcript -> sidecar written",
                  os.path.isfile(out_path + ".ass"))

            # Captions on but no transcript: skipped, no crash.
            out2 = os.path.join(tmp, "short_02.mp4")
            clip.crop_clip_local(src, 0.0, 5.0, "9:16", out2, finalize=False)
            check("captions on, no transcript -> no sidecar, no crash",
                  not os.path.exists(out2 + ".ass"))

            # FACE_TRACK_ENABLED=0 forces the static ffmpeg centre crop.
            centre = Recorder()
            clip._reframe_with_ffmpeg = centre
            clip._load_face_detector = Recorder(fn=lambda: (lambda *a, **k: []))
            real_reframe = clip._reframe_vertical
            clip._reframe_vertical = reals["_reframe_vertical"]
            real_cv2 = sys.modules.get("cv2")
            class _FakeCap:
                """cv2.VideoCapture stand-in: tracks opened(), reads nothing."""
                def __init__(self):
                    self.opened = []
                def VideoCapture(self, path):
                    self.opened.append(path)
                    return type("Cap", (), {"isOpened": lambda s: False,
                                            "release": lambda s: None})()
            fake_cv2 = _FakeCap()
            sys.modules["cv2"] = fake_cv2
            try:
                cfg.set_overrides({"FACE_TRACK_ENABLED": "0"})
                clip._reframe_vertical(src, out_path, "9:16")
                check("FACE_TRACK_ENABLED=0 -> centre crop",
                      len(centre.calls) == 1, str(centre.calls))
                check("FACE_TRACK_ENABLED=0 never opens the video",
                      fake_cv2.opened == [], str(fake_cv2.opened))

                cfg.set_overrides({"FACE_TRACK_ENABLED": "1"})
                try:
                    clip._reframe_vertical(src, out_path, "9:16")
                except RuntimeError:
                    pass  # fake bytes can't be opened; we only need the path taken
                check("FACE_TRACK_ENABLED=1 -> face path (not centre crop)",
                      len(centre.calls) == 1 and len(fake_cv2.opened) == 1,
                      f"centre={len(centre.calls)} opened={fake_cv2.opened}")
            finally:
                clip._reframe_vertical = real_reframe
                if real_cv2 is not None:
                    sys.modules["cv2"] = real_cv2
                else:
                    sys.modules.pop("cv2", None)
        finally:
            for name, fn in reals.items():
                setattr(clip, name, fn)
            cfg.clear_overrides()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_app_checks():
    # _overrides_from maps the 3 browser fields.
    built = webapp._overrides_from("local", {}, captions_enabled=True,
                                   caption_style="classic", face_track=False)
    check("overrides: CAPTIONS_ENABLED=1", built.get("CAPTIONS_ENABLED") == "1", str(built))
    check("overrides: CAPTION_STYLE=classic", built.get("CAPTION_STYLE") == "classic", str(built))
    check("overrides: FACE_TRACK_ENABLED=0", built.get("FACE_TRACK_ENABLED") == "0", str(built))

    built = webapp._overrides_from("local", {}, captions_enabled=False,
                                   caption_style="karaoke", face_track=True)
    check("overrides: captions off maps to 0", built.get("CAPTIONS_ENABLED") == "0", str(built))
    check("overrides: karaoke style", built.get("CAPTION_STYLE") == "karaoke", str(built))
    check("overrides: face track on maps to 1", built.get("FACE_TRACK_ENABLED") == "1")

    # Bogus style is dropped (config default keeps winning).
    built = webapp._overrides_from("local", {}, caption_style="explode")
    check("overrides: bogus caption_style dropped", "CAPTION_STYLE" not in built, str(built))

    # Position + margin overrides map through; bogus/invalid values dropped.
    built = webapp._overrides_from("local", {}, caption_position="TOP",
                                   caption_margin_v=200)
    check("overrides: CAPTION_POSITION=top",
          built.get("CAPTION_POSITION") == "top", str(built))
    check("overrides: CAPTION_MARGIN_V=200",
          built.get("CAPTION_MARGIN_V") == "200", str(built))
    built = webapp._overrides_from("local", {}, caption_position="sideways",
                                   caption_margin_v="not-a-number")
    check("overrides: bogus position/margin dropped",
          "CAPTION_POSITION" not in built and "CAPTION_MARGIN_V" not in built,
          str(built))
    built = webapp._overrides_from("local", {}, caption_margin_v=99999)
    check("overrides: margin clamped to 1200",
          built.get("CAPTION_MARGIN_V") == "1200", str(built))

    # field=None -> key absent (settings.local.json may supply it instead).
    built = webapp._overrides_from("local", {})
    check("overrides: untouched fields absent",
          not any(k in built for k in
                  ("CAPTIONS_ENABLED", "CAPTION_STYLE", "FACE_TRACK_ENABLED")),
          str(built))

    # generate() stores the keys in job params (worker -> background_task path)
    # and persists them to settings.
    webapp.generate_shorts = lambda **kw: {"shorts": []}
    c = webapp.app.test_client()
    r = c.post("/api/generate", json={
        "url": "https://youtu.be/captions", "mode": "local",
        "num_clips": 1, "captions_enabled": True,
        "caption_style": "classic", "face_track": False,
        "caption_position": "top", "caption_margin_v": 200,
    })
    check("generate with caption keys -> 202", r.status_code == 202, str(r.status_code))
    jid = r.get_json()["job_id"]
    import time as _t
    for _ in range(50):
        job = c.get(f"/api/status/{jid}").get_json()
        if job.get("status") in ("completed", "error"):
            break
        _t.sleep(0.05)
    saved = settings_store.load()
    check("generate persisted captions_enabled", saved.get("captions_enabled") is True,
          str(saved.get("captions_enabled")))
    check("generate persisted caption_style", saved.get("caption_style") == "classic",
          str(saved.get("caption_style")))
    check("generate persisted face_track", saved.get("face_track") is False,
          str(saved.get("face_track")))
    check("generate persisted caption_position", saved.get("caption_position") == "top",
          str(saved.get("caption_position")))
    check("generate persisted CAPTION_POSITION alias",
          saved.get("CAPTION_POSITION") == "top", str(saved.get("CAPTION_POSITION")))
    check("generate persisted caption_margin_v", saved.get("caption_margin_v") == 200,
          str(saved.get("caption_margin_v")))


def main():
    run_env_checks()
    run_remap_checks()
    run_ass_writer_checks()
    run_transcriber_checks()
    run_clipper_checks()
    run_app_checks()

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
