"""Karaoke / classic ASS subtitles for rendered shorts, all local via ffmpeg.

The pipeline keeps words with timestamps in the transcript (faster-whisper
``word_timestamps``); per rendered clip we slice the window, rebase it onto
the cut clip's timeline, optionally remap it past jump-cut silence gaps, and
write a sidecar ``<clip>.mp4.ass`` next to the DRAFT. The draft itself stays
clean — subtitles are burned in only by ``finalize_clip_local`` on save, on
top of the final 1080x1920 canvas.

Karaoke semantics in ASS: a line renders in ``SecondaryColour`` and each word
sweeps to ``PrimaryColour`` while it is being "sung", so base=white lives in
SecondaryColour and the ACTIVE accent lives in PrimaryColour — the opposite of
the classic style, where PrimaryColour is simply the text colour.

Configuration is env-driven — see caption_settings_from_env().
"""
import bisect
import os
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import env

# Same house style as clipper._run_ffmpeg: error-only output, bounded runtime.
FFMPEG_TIMEOUT = 180  # seconds

# ASS coordinate space the PlayRes refers to. finalize burns after blurpad,
# whose canvas is exactly this; for other aspects libass scales by the actual
# frame size, so a mismatch only shifts size/margins slightly.
PLAY_RES_X = 1080
PLAY_RES_Y = 1920

# A word shorter than this after gap removal is editing noise, not speech.
_MIN_WORD_SEC = 0.04
# A pause in speech longer than this always starts a new caption line, even if
# the word budget isn't spent — otherwise the tail word of a line would linger
# on screen through the whole pause.
_GAP_BREAK_SEC = 0.8


def _log(msg: str) -> None:
    """print() that survives the Cyrillic working paths this repo lives in."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", errors="backslashreplace").decode("utf-8"),
              flush=True)


def captions_enabled() -> bool:
    """CAPTIONS_ENABLED ('0' default) master switch — read at use time."""
    return str(env("CAPTIONS_ENABLED", "0") or "").strip().lower() not in (
        "0", "false", "no", "")


def caption_settings_from_env() -> Dict:
    """Resolve the caption knobs. Read at use time so GUI overrides apply."""

    def _color(name: str, default_hex: str) -> str:
        """RRGGBB (or AARRGGBB) hex -> ASS &HAABBGGRR (channels reversed)."""
        raw = str(env(name, default_hex) or "").strip().lstrip("#")
        if len(raw) == 8:  # leading alpha supplied: RRGGBB follows it
            alpha, rgb = raw[:2], raw[2:]
        else:
            alpha, rgb = "00", raw
        if len(rgb) != 6:
            rgb = default_hex[-6:]
        try:
            int(rgb, 16)
        except ValueError:
            rgb = default_hex[-6:]
        r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
        return f"&H{alpha.upper()}{b.upper()}{g.upper()}{r.upper()}"

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(float(env(name, str(default))))))
        except (TypeError, ValueError):
            return default

    style = str(env("CAPTION_STYLE", "karaoke") or "").strip().lower()
    if style not in ("karaoke", "classic"):
        style = "karaoke"
    return {
        "enabled": captions_enabled(),
        "style": style,
        "font": str(env("CAPTION_FONT", "Arial") or "Arial").strip() or "Arial",
        "font_size": _int("CAPTION_FONT_SIZE", 72, 8, 300),
        "max_words": _int("CAPTION_MAX_WORDS", 4, 1, 20),
        "margin_v": _int("CAPTION_MARGIN_V", 150, 0, 1200),
        "text_color": _color("CAPTION_TEXT_COLOR", "FFFFFF"),
        # #FFD700 gold. NOTE: _color reads its DEFAULT as RRGGBB too, so the
        # default is authored as FFD700, not the final ASS order.
        "active_color": _color("CAPTION_ACTIVE_COLOR", "FFD700"),
        "outline_color": _color("CAPTION_OUTLINE_COLOR", "000000"),
        # 80 = ~50% opacity shadow; dark halo keeps captions readable over
        # bright gradients without a full box behind the text.
        "shadow_color": _color("CAPTION_SHADOW_COLOR", "80000000"),
    }


def remap_words(words: Sequence[Dict], kept_segments: Sequence[Tuple[float, float]]
                ) -> List[Dict]:
    """Re-time `words` (given in the ORIGINAL cut-clip timeline) onto the
    jump-cut timeline produced by removing everything outside `kept_segments`.

    Removed intervals are [kept[i].end -> kept[i+1].start]; a word that lands in
    one is dropped, a word that straddles one is clamped to the kept part, and
    every surviving timestamp shifts left by the cumulative removed duration
    before it. `kept_segments` must be sorted, non-overlapping (s,e) pairs —
    exactly what silence.build_keep_segments() returns.
    """
    kept = sorted(
        (float(s), float(e)) for s, e in (kept_segments or []) if e > s
    )
    if not kept:
        return []
    starts = [s for s, _ in kept]
    removed = []  # removed[i] = seconds deleted before kept[i][0]
    acc = 0.0
    prev_end = kept[0][0]
    for s, _e in kept:
        acc += max(0.0, s - prev_end)
        removed.append(acc)
        prev_end = max(prev_end, _e)

    def _map_point(t: float) -> float:
        i = bisect.bisect_right(starts, t) - 1
        if i < 0:
            return t  # before the first kept segment: nothing removed yet
        s, e = kept[i]
        # Inside a removed gap after segment i, the point collapses onto
        # segment i's remapped end; inside segment i it just shifts left.
        return min(t, e) - removed[i]

    out = []
    for w in words:
        ws, we = float(w["start"]), float(w["end"])
        ns, ne = _map_point(ws), _map_point(we)
        if ne - ns >= _MIN_WORD_SEC:
            out.append({**w, "start": ns, "end": ne})
    return out


def _ass_time(seconds: float) -> str:
    """0.0 -> '0:00:00.00' — ASS wants centiseconds, not milliseconds."""
    cs = max(0.0, round(float(seconds) * 100.0))
    cs = int(cs) if isinstance(cs, float) and cs.is_integer() else int(round(cs))
    h = cs // 360000
    m = (cs // 6000) % 60
    s = (cs // 100) % 60
    c = cs % 100
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _clean_word(word: str) -> str:
    """Drop characters that are ASS override-block syntax.

    '{' or '}' would be parsed as an inline override and could corrupt the
    whole line; newlines are meaningless inside a word-level token.
    """
    return (word or "").replace("{", "").replace("}", "") \
        .replace("\r", " ").replace("\n", " ")


def _collect_words(transcript: Dict, clip_start: float, clip_end: float
                   ) -> List[Dict]:
    """Words overlapping [clip_start, clip_end], clipped to the window and
    rebased so 0 == clip_start. Transcript times are source-video seconds; the
    returned list is cut-clip seconds — the timeline silence-cut then edits."""
    words: List[Dict] = []
    if not transcript:
        return words
    for seg in transcript.get("segments") or []:
        for w in seg.get("words") or []:
            if w.get("start") is None or w.get("end") is None:
                continue  # faster-whisper emits None timings on empty tokens
            try:
                ws = max(float(clip_start), float(w["start"]))
                we = min(float(clip_end), float(w["end"]))
            except (TypeError, ValueError):
                continue
            if we - ws < _MIN_WORD_SEC:
                continue
            text = _clean_word(str(w.get("word", ""))).strip()
            if not text:
                continue
            words.append({"start": ws - clip_start, "end": we - clip_start,
                          "word": text})
    words.sort(key=lambda x: x["start"])
    return words


def _group_words(words: Sequence[Dict], max_words: int) -> List[Tuple[float, float, List[Dict]]]:
    """Pack words into caption lines of at most `max_words`, also breaking on
    long speech pauses so a stale line never lingers through dead air.
    Returns (line_start, line_end, words) triples."""
    lines: List[List[Dict]] = []
    cur: List[Dict] = []
    for w in words:
        if cur and (len(cur) >= max_words
                    or w["start"] - cur[-1]["end"] > _GAP_BREAK_SEC):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return [(ln[0]["start"], ln[-1]["end"], ln) for ln in lines]


def _header(settings: Dict) -> str:
    font = settings["font"].replace(",", " ")  # ',' would split the style line
    size = settings["font_size"]
    margin_v = settings["margin_v"]
    text = settings["text_color"]
    active = settings["active_color"]
    outline = settings["outline_color"]
    shadow = settings["shadow_color"]
    karaoke_style = (
        f"Style: Karaoke,{font},{size},{active},{text},{outline},{shadow},"
        "-1,0,0,0,100,100,1,0,1,3,1,2,60,60,"
        f"{margin_v},1"
    )
    classic_style = (
        f"Style: Classic,{font},{size},{text},{text},{outline},{shadow},"
        "-1,0,0,0,100,100,0,0,1,3,1,2,60,60,"
        f"{margin_v},1"
    )
    return (
        "[Script Info]\n"
        "Title: AI-Youtube-Shorts-Generator captions\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        f"PlayResX: {PLAY_RES_X}\n"
        f"PlayResY: {PLAY_RES_Y}\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{karaoke_style}\n"
        f"{classic_style}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


def _karaoke_text(line_words: Sequence[Dict], line_start: float) -> str:
    """'\\k40word \\k252word' — durations in centiseconds, relative to the
    line's own start. Padded with min 1cs so a zero-length syllable never
    trips strict ASS parsers."""
    parts = []
    for w in line_words:
        dur_cs = max(1, int(round((w["end"] - w["start"]) * 100.0)))
        parts.append(f"{{\\k{dur_cs}}}{w['word']}")
    return " ".join(parts)


def write_caption_ass(transcript: Optional[Dict],
                      clip_start: float,
                      clip_end: float,
                      out_path: str,
                      style: Optional[str] = None,
                      max_words: Optional[int] = None,
                      kept_segments: Optional[Sequence[Tuple[float, float]]] = None
                      ) -> Optional[str]:
    """Write the ASS sidecar for one rendered clip. Returns the path, or None
    when there is nothing to caption (no word timestamps in the window).

    `clip_start`/`clip_end` are source-video seconds (the crop window); word
    timings come from the source-relative transcript. When `kept_segments`
    (already in cut-clip seconds) is given, words are additionally remapped
    past the silence gaps that were cut out of the clip.
    """
    settings = caption_settings_from_env()
    style = (style or settings["style"]).strip().lower()
    if style not in ("karaoke", "classic"):
        style = settings["style"]
    max_words = max(1, int(max_words or settings["max_words"]))

    words = _collect_words(transcript, float(clip_start), float(clip_end))
    if kept_segments:
        before = len(words)
        words = remap_words(words, kept_segments)
        dropped = before - len(words)
        if dropped:
            _log(f"[captions/local] {dropped} word(s) fell into cut silence gaps")
    if not words:
        _log("[captions/local] no word timings in clip window — sidecar not written "
             "(re-transcribe to regenerate the .srt cache with words)")
        return None

    events = []
    for line_start, line_end, line_words in _group_words(words, max_words):
        if style == "karaoke":
            text = _karaoke_text(line_words, line_start)
            style_name = "Karaoke"
        else:
            text = " ".join(w["word"] for w in line_words)
            style_name = "Classic"
        events.append(
            f"Dialogue: 0,{_ass_time(line_start)},{_ass_time(line_end)},"
            f"{style_name},,0,0,0,,{text}"
        )

    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(_header(settings))
            fh.write("\n".join(events))
            fh.write("\n")
    except OSError as e:
        _log(f"[captions/local] could not write {out_path}: {e}")
        return None
    _log(f"[captions/local] wrote caption sidecar: {out_path} "
         f"({len(events)} lines, style={style})")
    return out_path


def burn_captions(video_path: str, ass_path: str) -> str:
    """Burn the ASS sidecar into the clip, replacing the file in place.

    The filter gets the ASS as a bare filename with cwd pointed at its
    directory, because the subtitles/ass filter eats absolute Windows paths
    alive: 'C:' loses the colon to filter-option parsing and backslashes get
    re-escaped at every layer. A relative name sidesteps all of it — the only
    character class it can never contain is filter separators themselves.
    """
    video_path = os.path.abspath(video_path)
    ass_path = os.path.abspath(ass_path)
    work_dir, ass_name = os.path.dirname(ass_path), os.path.basename(ass_path)
    for sep in (":", "=", "'", "\\"):
        if sep in ass_name:
            raise RuntimeError(
                f"caption burn: sidecar name {ass_name!r} contains {sep!r}, "
                "which the ffmpeg ass filter cannot parse even relatively; "
                "rename the sidecar to plain ASCII")

    tmp_out = video_path + ".captions.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path,
        "-vf", f"ass={ass_name}",
        "-c:a", "copy",
        tmp_out,
    ]
    _log(f"[captions/local] burning subtitles: {ass_name} -> "
         f"{os.path.basename(video_path)}")
    start = time.time()
    try:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=FFMPEG_TIMEOUT, cwd=work_dir,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"ffmpeg (burning captions) timed out after {FFMPEG_TIMEOUT}s")
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()
            tail = "\n".join(detail.splitlines()[-6:]) \
                or f"exit status {proc.returncode}"
            raise RuntimeError(
                f"ffmpeg failed while burning captions:\n{tail}")
        os.replace(tmp_out, video_path)
    finally:
        # tmp outlives its usefulness the moment os.replace wins; leaving it
        # on error would look like a second draft file in the output dir.
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
    _log(f"[captions/local] captions burned in {time.time() - start:.2f}s")
    return video_path
