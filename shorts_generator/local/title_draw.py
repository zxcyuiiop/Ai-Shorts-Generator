"""Burn the highlight title into a clip near the bottom — one ffmpeg drawtext pass.

Why the temp-UTF-8-textfile approach for the filter string does NOT apply here:
``build_title_drawtext_filter()`` must return a ``drawtext=...`` string (used
by the save pipeline and directly by tests), so the text is delivered in argv
shell-less, the same way ``thumbgen._drawtext_filter`` already does it with
Cyrillic titles. ``_drawtext_escape`` handles the filter-level specials
(\\ : ' , [ ]). A literal ``%`` is disarmed by ``expansion=none`` in the
filter (needs ffmpeg >= 5.1): on ffmpeg 9 the classic ``\\%`` escape itself
trips "Stray %" inside a quoted ``text='...'`` value. drawtext needs
fontconfig/freetype; on Windows we point at a known present fontfile
(arialbd.ttf, ``isfile``-guarded) so the default font lookup cannot bite.

The pass itself (``apply_title_drawtext``) keeps the house style: same encoder
selection as the other finalize stages, same tempfile + os.replace pattern,
same FFMPEG timeout. A failure raises RuntimeError; the caller (finalize) wraps
the stage in try/except and never loses the clip.
"""
import os
import subprocess
import time
from typing import Optional, Tuple

from ..config import env

FFMPEG_TIMEOUT = 180  # seconds, same house style as captions/blurpad

# Wrap / truncate budgets. 38 chars is what 64px Arial Bold fits across a
# 1080px canvas; 2 lines max, then "…" after ~80 chars total.
_MAX_LINE_CHARS = 38
_MAX_TOTAL_CHARS = 80
_LINE_STEP_FROM_FONT = 1.15  # second line sits ~1.15*fontsize lower

# Pixel budget: a rendered line may use at most this share of the frame
# width. The char budgets above are calibrated for 64px across 1080px, but
# with nothing measuring pixels a wide line on a narrow canvas used to paint
# past the frame edge (x centers a negative offset = the line is clipped on
# both sides). build_title_drawtext_filter measures every wrapped line against
# the frame width and, when it would overflow, rewraps once by that width and
# then shrinks the font until it fits (down to _FONT_FLOOR).
_WIDTH_BUDGET_PCT = 0.92

# Never squeeze the title below a readable size — an unreadable one is not a
# failure mode the clip can survive, an off-canvas one is no better.
_FONT_FLOOR = 24

# Windows font directory. Guarded by isfile: on a bare/non-Windows box ffmpeg
# falls back to its built-in drawtext font instead of failing the whole pass.
_PREFERRED_FONT = r"C:/Windows/Fonts/arialbd.ttf"


def title_enabled() -> bool:
    """TITLE_ENABLED ('1' default) master switch — read at use time."""
    return str(env("TITLE_ENABLED", "1") or "").strip().lower() not in (
        "0", "false", "no", "")


def title_settings_from_env() -> dict:
    """Resolve the title knobs. Read at use time so GUI overrides apply."""

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(float(env(name, str(default))))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": title_enabled(),
        # Distance of the title BASELINE from the bottom edge, in px. Clamped
        # well inside the frame so a stray value can't push the text off-canvas.
        "y_from_bottom": _int("TITLE_Y_FROM_BOTTOM", 750, 100, 1500),
        "font_size": _int("TITLE_FONT_SIZE", 64, 24, 200),
    }


def _drawtext_escape(text: str) -> str:
    """Escape characters special inside a drawtext filter string.

    Backslash first (it is the escape character itself), then the filter-level
    separators. Apostrophes are dropped: argv is passed shell-less, and inside
    drawtext's own quoting a literal ' cannot be protected reliably across
    platforms (same reasoning as thumbgen._drawtext_escape). ``%`` is NOT
    escaped here — the filter opts out of text expansion via expansion=none,
    which is the only form ffmpeg 9 accepts a bare percent in (the old \\%
    escape dies there with "Stray %").
    """
    return (
        text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "")
            .replace(",", "\\,")
            .replace("[", "\\[")
            .replace("]", "\\]")
    )


def _wrap_title(title: str) -> list:
    """Split a long title over at most 2 lines, word-bounded, ~38 chars/line.

    Total delivered chars are capped at ~80 with a trailing "…" (carried by
    the first line, where ffmpeg 9 can draw it) so a multi-hundred-char
    highlight title never overflows the canvas. A title that fits one line is
    returned as a single-element list. Even pathological single-word input is
    hard-truncated so no line exceeds the ~38 char budget.

    Character counts are only a proxy — a 38-char line at 64px fits a 1080px
    canvas, but the same line overflows a narrow one. Callers that know the
    frame size should go through build_title_drawtext_filter(), which rewraps
    with _wrap_lines_px() whenever the char-wrapped lines measure too wide.
    """
    title = " ".join(str(title).split())  # collapse runs of whitespace/newlines
    ellipsis = len(title) > _MAX_TOTAL_CHARS
    body = title[: _MAX_TOTAL_CHARS - 1].rstrip() if ellipsis else title

    line1, line2 = [], []
    for word in body.split(" "):
        candidate = " ".join(line1 + [word])
        if len(candidate) <= _MAX_LINE_CHARS or not line1:
            line1.append(word)
        else:
            line2.append(word)

    if ellipsis:
        # Ellipsis belongs on line 1 ONLY if those words still leave room
        # for it there; drop the ones that would push the cap off-budget.
        while line1 and len(" ".join(line1) + "…") > _MAX_LINE_CHARS + 1:
            line1.pop()
        if not line1:
            line1 = body.split(" ")[:1]  # at least one truncated word
    first = " ".join(line1) + ("…" if ellipsis else "")
    second = " ".join(line2)
    if not second:
        return [first[: _MAX_LINE_CHARS + 1]]
    return [first[: _MAX_LINE_CHARS + 1], second[: _MAX_LINE_CHARS]]


def _measure_line_px(text: str, fontsize: int) -> int:
    """Width of `text` rendered at `fontsize` in the stage's font, in pixels.

    Precise when PIL and the preferred font are available: Arial Bold advance
    widths match libfreetype's closely enough for a budget check, and the box
    padding (+2*boxborderw) is added where the budget is consumed, not here.
    Fallback without either is a remark-a metrics heuristic (bold latin runs
    ~0.55 em); it errs wide, which is safe under the 92% budget.
    """
    if fontsize <= 0:
        return 0
    try:
        from PIL import ImageFont
        if os.path.isfile(_PREFERRED_FONT):
            font = ImageFont.truetype(_PREFERRED_FONT, int(fontsize))
            return int(round(float(font.getlength(text))))
    except Exception:
        pass
    return int(round(len(text) * fontsize * 0.55))


def _wrap_lines_px(title: str, fontsize: int, max_w: int) -> list:
    """Same 2-line word-bounded wrap as _wrap_title, budgeted in PIXELS.

    Used when a char-counted line proves too wide for the frame: the words
    are rebalanced so each line fits `max_w` at the requested `fontsize`
    whenever that is achievable (a lone word wider than the budget keeps its
    line and is left for the font-size squeeze). The ~80 char / "…" capping is
    inherited from _wrap_title so a multi-hundred-char title still truncates.
    """
    lines = _wrap_title(title)
    if not lines:
        return lines
    # Keep the truncation mark glued to line 1's last word while rebalancing —
    # split off on its own it would migrate to line 2 like a real word.
    ellipsis = lines[0].endswith("…")
    first_line = lines[0].rstrip("…")
    words = (first_line.split() + (lines[1].split() if len(lines) > 1 else []))
    if len(words) <= 1:
        return lines

    line1, line2 = [], []
    for word in words:
        # Line 1 carries the "…" only while it is still the LAST line.
        candidate = " ".join(line1 + [word])
        trial = candidate + "…" if ellipsis and not line2 else candidate
        if not line1 or _measure_line_px(trial, fontsize) <= max_w:
            line1.append(word)
        else:
            line2.append(word)

    # An over-wide lone first word steals the rest of the title unless one
    # peer migrates to line 2 — the squeeze can only shrink a uniform font.
    # A lone word that fits when measured WITHOUT the ellipsis mark is just a
    # squeezed final line though: the mark costs little, and migrating it
    # would evacuate line 1 entirely (only the "…" would stay behind). Guard
    # against that collapse.
    if len(line1) == 1 and line2 and \
            _measure_line_px(line1[0], fontsize) > max_w:
        line2.insert(0, line1.pop())

    if line2 and _measure_line_px(" ".join(line2), fontsize) > max_w:
        # Line 2 cannot be squeezed alone (one font for both lines): drop its
        # tail words toward a shape that fits, but only while at least TWO
        # words stay behind — a lone over-wide word leading line 2 would pass
        # everything after it to the squeeze anyway, so dropping to one word
        # just throws away most of the title for nothing. Mark a real
        # truncation with "…", same contract as _wrap_title's cap.
        while len(line2) > 2 and \
                _measure_line_px(" ".join(line2) + "…", fontsize) > max_w:
            line2.pop()
        if len(line2) > 1 and \
                _measure_line_px(" ".join(line2) + "…", fontsize) <= max_w:
            line2[-1] += "…"
    first = " ".join(line1) + ("…" if ellipsis else "")
    if not line2:
        return [first]
    return [first, " ".join(line2)]


def _fit_fontsize_px(lines: list, fontsize: int, budget: int) -> int:
    """Largest size <= `fontsize` (never below _FONT_FLOOR) at which every
    line — box padding included — fits the pixel budget.

    The proportional scale from `fontsize` is only a first guess: glyph
    advance does not shrink perfectly linearly (hinting/kerning at small
    sizes, and int() truncation lands on the wrong side of a true fit), so
    the result is verified at size and stepped down until it really fits.
    """
    if not lines:
        return max(_FONT_FLOOR, int(fontsize))
    box_pad = 2 * 18  # boxborderw=18 in _one_line_filter, on both sides

    def _overflows(size: int) -> bool:
        return any(_measure_line_px(l, size) + box_pad > budget for l in lines)

    size = max(_FONT_FLOOR, int(fontsize))
    if _overflows(size):
        size = max(_FONT_FLOOR, int(fontsize * budget /
                                      (max(_measure_line_px(l, int(fontsize))
                                           for l in lines) + box_pad)))
        while size > _FONT_FLOOR and _overflows(size):
            size -= 1
    return size


def build_title_drawtext_filter(title: str, y_from_bottom: int = 750,
                                fontsize: int = 64,
                                frame_width: int = 1080) -> str:
    """Return the ffmpeg filter string that draws `title` centered at
    ``h - y_from_bottom`` (the title baseline), white bold text on a subtle
    black border plus a semi-transparent box so bright videos keep contrast.

    Long titles wrap to a second line drawn one line-height lower; both lines
    are centered horizontally. Each wrapped line is measured in pixels against
    ``_WIDTH_BUDGET_PCT`` of ``frame_width``: a line that would overflow the
    canvas (narrow clips, large fonts — an ``(w-text_w)/2`` offset going
    negative clips text off-canvas) first triggers a width-budgeted rewrap,
    then an artist-style squeeze that shrinks the font, uniformly for both
    lines, until it fits. The returned string may contain a comma joining the
    two drawtext stages — that is still ONE -vf argument.
    """
    frame_width = max(2, int(frame_width))
    budget = int(frame_width * _WIDTH_BUDGET_PCT)
    lines = _wrap_title(title)
    if any(_measure_line_px(l, fontsize) + 36 > budget for l in lines):
        lines = _wrap_lines_px(title, fontsize, budget - 36)
    fontsize = _fit_fontsize_px(lines, fontsize, budget)

    if os.path.isfile(_PREFERRED_FONT):
        # C:/... → C\:/... so the drive colon survives filter-option parsing.
        font_escaped = _PREFERRED_FONT.replace(":", "\\:")
        font_opt = f"fontfile='{font_escaped}':"
    else:
        font_opt = ""

    def _one_line_filter(line_text: str, line_y_from_bottom: int) -> str:
        expr = _drawtext_escape(line_text)
        return (
            f"drawtext={font_opt}"
            # expansion=none: title is static; disables %{...} expansion, which
            # ffmpeg 9 enforces so strictly that even the classic \\% escape
            # is rejected ("Stray %"). Requires ffmpeg >= 5.1 (2022) — this
            # tool ships 9.0, and the e2e test guards the minimum.
            f"expansion=none:"
            f"text='{expr}'"
            f":fontsize={int(fontsize)}"
            f":fontcolor=white"
            f":borderw=2:bordercolor=black@0.7"
            f":box=1:boxcolor=black@0.35:boxborderw=18"
            f":x=(w-text_w)/2"
            # Baseline of this line = h - line_y_from_bottom, so the TOP of the
            # (padded) glyph box sits at h - line_y_from_bottom - text_h.
            f":y=h-{int(line_y_from_bottom)}-text_h"
        )

    filters = [_one_line_filter(lines[0], y_from_bottom)]
    if len(lines) == 2:
        # Second line one line-height lower (closer to the bottom edge).
        step = max(1, int(round(fontsize * _LINE_STEP_FROM_FONT)))
        filters.append(_one_line_filter(lines[1], y_from_bottom - step))
    return ",".join(filters)


def apply_title_drawtext(video_path: str, title: str) -> str:
    """Burn `title` into `video_path` in place. Returns the path.

    Reads TITLE_ENABLED / TITLE_Y_FROM_BOTTOM / TITLE_FONT_SIZE at call time
    through config.env so per-request GUI overrides apply. Uses the same
    outer-swap tempfile + os.replace pattern as the neighboring finalize
    stages; on failure the original file is left untouched and RuntimeError is
    raised for the caller to log-and-continue.
    """
    from . import clipper  # deferred: clipper imports this module lazily

    enabled = title_enabled()
    title = (title or "").strip()
    if not enabled or not title:
        return video_path

    settings = title_settings_from_env()

    video_path = os.path.abspath(video_path)
    work_dir = os.path.dirname(video_path)
    tmp_out = video_path + ".title.mp4"
    encoder = clipper._get_video_encoder()

    # The pixel budget must track the clip's real width: char-count wrapping
    # alone cannot know whether 38 glyphs fit a 606px- or a 1920px-wide frame.
    try:
        frame_w, _ = clipper._probe_dimensions(video_path)
    except Exception:
        frame_w = 1080
    vf = build_title_drawtext_filter(
        title,
        y_from_bottom=settings["y_from_bottom"],
        fontsize=settings["font_size"],
        frame_width=frame_w,
    )

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", video_path,
           "-vf", vf,
           *clipper._video_encoder_args(encoder),
           "-c:a", "copy",
           tmp_out]
    try:
        print(f"[title] burning title ({len(_wrap_title(title))} line(s), "
              f"y=-{settings['y_from_bottom']}px): "
              f"{os.path.basename(video_path)}", flush=True)
    except UnicodeEncodeError:
        pass  # cosmetic log only; never kill the stage over a print
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=FFMPEG_TIMEOUT,
                              cwd=work_dir)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:]) \
                or f"exit status {proc.returncode}"
            raise RuntimeError(f"ffmpeg failed while burning the title:\n{tail}")
        os.replace(tmp_out, video_path)
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
    try:
        print(f"[title] title burned in {time.time() - start:.2f}s", flush=True)
    except UnicodeEncodeError:
        pass
    return video_path
