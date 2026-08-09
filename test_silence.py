"""Pure-logic coverage of shorts_generator.local.silence.

No real ffmpeg is used: ``detect_silences`` is driven through a stubbed
``subprocess.run`` that returns canned ``silence_start:`` / ``silence_end: ...
| silence_duration:`` lines on stderr, exactly the way ffmpeg's silencedetect
filter reports them. ``build_keep_segments`` needs no process at all.

Fully hermetic: only monkey-patches module state (restored in finally), writes
nothing to disk, touches no env vars.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator.local import silence as sil  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


class FakeProc:
    """CompletedProcess stand-in for _run / _has_audio_stream / get_duration."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- build_keep_segments: static analysis over (start, end) pairs -------------


def test_build_keep_segments():
    d = 10.0

    # No silences at all -> the whole thing is kept -> nothing worth cutting.
    check("keep: no silences -> None", sil.build_keep_segments(d, []) is None)
    # None is not a valid silences argument: as written it raises TypeError.
    try:
        sil.build_keep_segments(d, None)
        check("keep: None silences raises TypeError", False, "no raise")
    except TypeError:
        check("keep: None silences raises TypeError", True)

    # A long pause in the middle splits the clip into two kept speech pieces,
    # each padded by keep_extra on the cut boundary.
    segs = sil.build_keep_segments(d, [(5.0, 6.0)], keep_extra=0.15)
    check("keep: one pause -> two segments", segs == [(0.0, 5.15), (5.85, 10.0)], str(segs))

    # Segments clamp to the clip bounds and keep_extra windows overlap the cuts.
    segs = sil.build_keep_segments(d, [(0.5, 1.5), (8.5, 9.5)], keep_extra=0.15)
    check("keep: keep_extra clamps at 0 and duration",
          segs == [(0.0, 0.65), (1.35, 8.65), (9.35, 10.0)], str(segs))

    # keep_extra == 0: the cut is exact, no silence retained around speech.
    segs = sil.build_keep_segments(d, [(4.0, 6.0)], keep_extra=0.0)
    check("keep: keep_extra=0 gives exact speech bounds",
          segs == [(0.0, 4.0), (6.0, 10.0)], str(segs))

    # keep_extra=0 still cuts when the removed pause beats the 95%-cover gate.
    segs = sil.build_keep_segments(60.0, [(10.0, 20.0)], keep_extra=0.0)
    check("keep: keep_extra=0, removable pause -> exact speech bounds",
          segs == [(0.0, 10.0), (20.0, 60.0)], str(segs))

    # A pause shorter than 2*keep_extra is fully absorbed by the keep windows:
    # the two neighbouring pieces touch, merge back into the whole clip, and the
    # 95%-cover gate then folds it to None (nothing worth cutting).
    check("keep: sub-2*keep_extra pause -> None (not worth cutting)",
          sil.build_keep_segments(10.0, [(5.0, 5.2)], keep_extra=0.15) is None)

    # Full-clip silence is not "nothing": inverted to zero speech, so only the
    # keep_extra slivers at the two edges remain.
    segs = sil.build_keep_segments(d, [(0.0, 10.0)], keep_extra=0.15)
    check("keep: full-clip silence keeps edge slivers",
          segs == [(0.0, 0.15), (9.85, 10.0)], str(segs))
    check("keep: full-clip silence is not None", segs is not None)

    # Zero / negative duration can never be cut.
    check("keep: duration 0 -> None", sil.build_keep_segments(0.0, [(0.0, 1.0)]) is None)
    check("keep: negative duration -> None", sil.build_keep_segments(-3.0, []) is None)

    # Silences entirely outside [0, duration] are ignored / dropped.
    check("keep: out-of-range silences dropped",
          sil.build_keep_segments(d, [(-3.0, -1.0), (20.0, 30.0)]) is None)

    # Inverted or zero-length silence ranges contribute nothing.
    check("keep: inverted/empty ranges dropped",
          sil.build_keep_segments(d, [(5.0, 5.0), (6.0, 4.0)]) is None)

    # A single tiny kept piece under MIN_SEGMENT_SEC is dropped and, being the
    # only range, leaves nothing usable -> None.
    check("keep: only-fragment under MIN_SEGMENT_SEC -> None",
          sil.build_keep_segments(0.05, [(0.02, 0.03)], keep_extra=0.15) is None)

    # Adjacent/overlapping/partially-overlapping silence reports merge into one
    # pause before inversion: two nearby pauses become a single cut.
    segs = sil.build_keep_segments(60.0, [(10.0, 13.0), (12.0, 14.0)], keep_extra=0.15)
    check("keep: overlapping silences merge into one cut",
          segs == [(0.0, 10.15), (13.85, 60.0)], str(segs))

    # Monotonic, ordered, within-bounds output across several pauses.
    segs = sil.build_keep_segments(20.0, [(3.0, 4.0), (8.0, 11.0), (15.0, 16.0)],
                                   keep_extra=0.15)
    ok = (segs is not None
          and all(segs[i][1] <= segs[i + 1][0] for i in range(len(segs) - 1))
          and all(0.0 <= s <= e <= 20.0 for s, e in segs))
    check("keep: monotonic, ordered, within bounds", ok, str(segs))
    check("keep: covers expected boundaries",
          segs == [(0.0, 3.15), (3.85, 8.15), (10.85, 15.15), (15.85, 20.0)], str(segs))


# --- detect_silences: parse canned silencedetect stderr -----------------------


def _stub_run_factory(stderr_feed, duration_map):
    """Return a fake subprocess.run dispatching on the tool being invoked.

    ffmpeg -> canned silencedetect output on stderr (returncode 0).
    ffprobe duration -> lookup in duration_map; ffprobe audio probe -> success.
    Anything else -> success with empty output.
    """

    def fake_run(cmd, *args, **kwargs):
        joined = " ".join(cmd)
        if cmd[0] == "ffmpeg":
            return FakeProc(returncode=0, stderr=stderr_feed)
        if "format=duration" in joined:  # silence.get_duration
            label = cmd[-1]
            out = duration_map.get(label)
            if out is None:
                return FakeProc(returncode=1, stderr="no duration")
            return FakeProc(returncode=0, stdout=out)
        if "stream=index" in joined:  # _has_audio_stream
            return FakeProc(returncode=0, stdout="1\n")
        return FakeProc(returncode=0, stdout="")

    return fake_run


def test_detect_silences():
    real_run = subprocess.run
    label = "clip.mp4"

    def run_with(stderr_feed, duration_map=None):
        subprocess.run = _stub_run_factory(stderr_feed, duration_map or {})
        try:
            return sil.detect_silences(label, noise_db=-35.0, min_silence=0.45,
                                       log=lambda *a, **k: None)
        finally:
            subprocess.run = real_run

    # Classic start/end pair with the duration echoed on the end line.
    stderr = (
        "[silencedetect @ 0x0] silence_start: 1.25\n"
        "[silencedetect @ 0x0] silence_end: 3.5 | silence_duration: 2.25\n"
    )
    check("detect: start/end pair parsed",
          run_with(stderr) == [(1.25, 3.5)], "pair")

    # Multiple pauses come back in order.
    stderr = (
        "silence_start: 0.5\n"
        "silence_end: 1.0 | silence_duration: 0.5\n"
        "silence_start: 4.0\n"
        "silence_end: 5.25 | silence_duration: 1.25\n"
    )
    check("detect: multiple pauses in order",
          run_with(stderr) == [(0.5, 1.0), (4.0, 5.25)], "multi")

    # A trailing silence_start with no silence_end closes at the media duration.
    stderr = (
        "silence_start: 2.0\n"
        "silence_end: 3.0 | silence_duration: 1.0\n"
        "silence_start: 8.0\n"
    )
    check("detect: open start closes at duration",
          run_with(stderr, {label: "12.0"}) == [(2.0, 3.0), (8.0, 12.0)], "trailing")

    # Overlapping / duplicate reports merge into one range.
    stderr = (
        "silence_start: 2.0\n"
        "silence_end: 3.0 | silence_duration: 1.0\n"
        "silence_start: 2.5\n"
        "silence_end: 4.0 | silence_duration: 1.5\n"
    )
    check("detect: overlapping reports merged",
          run_with(stderr) == [(2.0, 4.0)], "merged")

    # Unparsable / empty output -> no pauses.
    check("detect: unparsable stderr -> []", run_with("garbage\nno markers here\n") == [])

    # ffmpeg exit != 0 raises RuntimeError (stderr tail included).
    subprocess.run = lambda cmd, *a, **k: FakeProc(returncode=1, stderr="boom: bad input")
    try:
        try:
            sil.detect_silences(label, log=lambda *a, **k: None)
            check("detect: ffmpeg failure raises", False, "no raise")
        except RuntimeError as e:
            check("detect: ffmpeg failure raises", "detecting silence" in str(e),
                  str(e).splitlines()[0])
    finally:
        subprocess.run = real_run


# --- cut_pauses argument validation -------------------------------------------


def test_cut_pauses_validation():
    real_run = subprocess.run
    try:
        try:
            sil.cut_pauses("in.mp4", "out.mp4", [], log=lambda *a, **k: None)
            check("cut: empty segments rejected", False, "no raise")
        except ValueError as e:
            check("cut: empty segments rejected", "no segments" in str(e), str(e))
        # All non-positive-length segments collapse to nothing -> same guard.
        try:
            sil.cut_pauses("in.mp4", "out.mp4", [(5.0, 5.0), (6.0, 3.0)],
                           log=lambda *a, **k: None)
            check("cut: only-empty segments rejected", False, "no raise")
        except ValueError as e:
            check("cut: only-empty segments rejected", "no segments" in str(e), str(e))
    finally:
        subprocess.run = real_run


def main():
    test_build_keep_segments()
    test_detect_silences()
    test_cut_pauses_validation()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
