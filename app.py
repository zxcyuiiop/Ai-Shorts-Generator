"""Flask web GUI for AI YouTube Shorts Generator."""
import io
import json
import os
import queue
import re
import hmac
import logging
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

# Pipeline progress lines contain non-ASCII characters like →, which crash on
# Windows consoles defaulting to cp1252/cp1251. Same fix as main.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, render_template, request, jsonify, Response, send_from_directory
from werkzeug.utils import secure_filename

from shorts_generator import generate_shorts, history, settings_store
from shorts_generator.config import LOCAL_OUTPUT_DIR

app = Flask(__name__)

# Uploads are full-length source videos; the default 16MB cap would reject them.
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # 8 GB

UPLOAD_DIR = os.path.join(os.path.abspath(LOCAL_OUTPUT_DIR), "uploads")
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"}

# job_id -> {status, stage, progress, url, added_at, _queued, started_at,
#            finished_at, result, error, log}
jobs = {}
progress_queues = {}
jobs_lock = threading.Lock()

# Single worker thread drains this queue; POST /api/generate only enqueues.
# Envelope: {"job_id", "url", "params"} -- params are the pipeline/overlay
# arguments forwarded to background_task when the job starts.
job_queue = queue.Queue()

# Keep at most this many finished jobs; the browser only ever needs the latest.
MAX_FINISHED_JOBS = 20

log = logging.getLogger("aishorts.gui")

# TTL for live progress_queues entries (created when a job is queued, reaped on
# terminal state). Guards against a leak if both a worker crash and the
# terminal-event code path miss the cleanup.
PROGRESS_QUEUE_TTL = 6 * 3600

# Absolute lifetime cap for one SSE connection, even on an eternal keepalive
# diet -- a stuck browser tab must not pin a connection forever.
SSE_MAX_LIFETIME = 6 * 3600

# Streaming upload cap. MAX_CONTENT_LENGTH already rejects large requests up
# front, but that limit trusts the Content-Length header; chunked uploads can
# slide past it, so we also count bytes as they hit the disk.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES") or
                       app.config["MAX_CONTENT_LENGTH"])

_ASPECT_WHITELIST = {"9:16", "16:9", "1:1", "4:5", "4:3"}
_FORMAT_WHITELIST = {"2160", "1440", "1080", "720", "480", "360", "best", "audio"}

# Hosts the downloader is allowed to talk to (SSRF guard). Covers youtube.com
# itself, every subdomain (including music.youtube.com) and youtu.be links.
ALLOWED_URL_HOSTS = ("youtube.com", "youtu.be")


def _parse_url(source):
    """urlparse that returns (parsed, is_http_url); swallows garbage input.
    Windows paths parse as scheme="c" etc. and are correctly NOT http."""
    try:
        parsed = urlparse((source or "").strip())
    except (ValueError, TypeError):
        return None, False
    return parsed, parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _is_allowed_video_url(url):
    """True only for http(s) URLs on the YouTube allow-list.

    An explicit netloc check keeps payloads like ``https://evil.com/?q=``
    (SSRF into the LAN) from ever reaching yt-dlp.
    """
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_URL_HOSTS)


def _looks_like_local_path(value):
    """True when the saved/form value is a local filesystem path, not a URL."""
    v = (value or "").strip()
    if not v or "://" in v:
        return False
    return v.startswith(("/", "~")) or (len(v) > 1 and v[1] == ":") or \
        os.sep in v


def _parse_num_clips(value):
    """Parse num_clips, clamped to [1, 20]. Returns (int, None) or (None, err)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None, "num_clips должен быть целым числом от 1 до 20"
    if n < 1:
        log.info("num_clips=%s below range, clamped to 1", n)
        n = 1
    elif n > 20:
        log.info("num_clips=%s above range, clamped to 20", n)
        n = 20
    return n, None


def _is_terminal_job(job):
    return bool(job) and job.get("status") in (
        "completed", "error", "cancelled", "canceled")


def _finish_progress_queue(job_id):
    """Hook run once a job reaches a terminal state.

    Unblocks every SSE stream parked in q.get() on this queue (the sentinel
    index is dropped by the idx < next_idx dedup) and marks the queue for TTL
    reaping in _prune_finished_jobs.
    """
    q = progress_queues.get(job_id)
    if q is not None:
        if getattr(q, "_created_at", None) is None:
            q._created_at = time.time()
        q.put(-1)


def _reap_stale_progress_queues():
    """Drop queue entries for jobs that finished (or vanished) long ago."""
    now = time.time()
    for jid, q in list(progress_queues.items()):
        created = getattr(q, "_created_at", None)
        if created is None or now - created < PROGRESS_QUEUE_TTL:
            continue
        if _is_terminal_job(jobs.get(jid)) or jid not in jobs:
            progress_queues.pop(jid, None)


_error_pattern = re.compile(r"^[A-Za-z_][\w.]*$")


def _base_message(msg):
    """First line of str(e) with any absolute paths from this machine stripped
    out -- the client never learns the server's directory layout."""
    line = re.sub(r"(\w:\\[^\s\"']+|/(?:[\w.-]+/)+[\w.~-]+)", "…", (msg or "").strip())
    # URLs in errors can carry secrets in the query string (api keys, tokens) —
    # keep the origin+path, drop everything from the first '?'.
    line = re.sub(r"\?[^\s\"']*", "?…", line)
    return line.splitlines()[0][:300].strip()


def _humanize_error(e):
    """Map a pipeline exception to a safe Russian message for the browser.

    The full traceback goes to the server log; the client gets a sanitized
    message. Downstream-tool errors (yt-dlp / ffmpeg) keep their sanitized text
    -- users read those to fix cookies/codecs. Everything else collapses to the
    exception class so internal paths/state can't leak.
    """
    raw = str(e)
    low = raw.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube отклонил запрос (проверка «вы не бот»). "
                "Обновите cookies для yt-dlp в настройках.")
    if "this video is unavailable" in low or "video unavailable" in low:
        return "Видео недоступно на YouTube (удалено, приватное или регион-блок)."
    if "unsupported url" in low:
        return "Этот URL не поддерживается. Нужна ссылка на YouTube."
    if isinstance(e, (FileNotFoundError, PermissionError)):
        base = _base_message(raw)
        label = {"FileNotFoundError": "Файл не найден",
                 "PermissionError": "Нет доступа к файлу"}[type(e).__name__]
        return f"{label}: {base}" if base else label
    if isinstance(e, subprocess.TimeoutExpired):
        return "Ошибка пайплайна: внешняя команда зависла и была прервана по таймауту"
    base = _base_message(raw)
    if base and any(marker in low for marker in (
            "yt-dlp", "ytdlp", "codec", "ffmpeg", "http error",
            "failed to download", "unable to download",
            # Transcription errors are already user-safe and informative —
            # keep their (sanitized) text instead of a bare exception name.
            "whisper", "no segments", "no speech", "transcri")):
        return base  # yt-dlp/ffmpeg already speak to the user; paths sanitized
    name = type(e).__name__ if _error_pattern.match(type(e).__name__) else "InternalError"
    return f"Ошибка пайплайна: {name}"


def _gui_token():
    """GUI_TOKEN from env/settings/this-process override; empty means no auth."""
    for source in (os.environ.get("GUI_TOKEN"),
                   settings_store.load().get("GUI_TOKEN"),
                   getattr(_gui_token, "override", None)):
        if isinstance(source, str) and source.strip():
            return source.strip()
    return None


def _check_gui_token():
    """Bearer header or ?token= must match hmac-compared GUI_TOKEN."""
    import secrets as _secrets

    token = _gui_token()
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    candidate = None
    if auth.startswith("Bearer "):
        candidate = auth[7:].strip()
    if not candidate:
        candidate = (request.args.get("token") or "").strip()
    return bool(candidate) and _secrets.compare_digest(candidate, token)


@app.before_request
def _require_api_token():
    """Gate /api/* behind GUI_TOKEN when one is configured.

    Off by default: without a token the GUI works exactly as before. Static /
    template / /output routes stay open either way (same-machine UX assets).
    """
    if not request.path.startswith("/api/"):
        return None
    if _check_gui_token():
        return None
    return jsonify({"error": "Требуется авторизация: передайте токен как "
                             "'Authorization: Bearer <GUI_TOKEN>' или ?token="}), 401


# Map a pipeline log line to (stage, percent). The pipeline already prints its
# own progress; we read those markers instead of guessing with sleeps.
STAGE_MARKERS = [
    (re.compile(r"^\[download"), "downloading", 15),
    (re.compile(r"^\[transcribe"), "transcribing", 35),
    (re.compile(r"^\[highlights\] content="), "analyzing", 55),
    (re.compile(r"^\[highlights\] chunk"), "analyzing", 60),
    (re.compile(r"^\[pipeline.*cropping"), "rendering", 75),
    (re.compile(r"^\[clip"), "rendering", 85),
]


def _classify(line):
    """Return (stage, progress) for a pipeline line, or (None, None)."""
    for pattern, stage, pct in STAGE_MARKERS:
        if pattern.search(line):
            return stage, pct
    return None, None


class _JobLogStream(io.TextIOBase):
    """stdout replacement that mirrors the pipeline's prints into a job's queue.

    generate_shorts() reports progress by printing; capturing that is what makes
    the GUI's log and progress bar reflect what is actually happening rather than
    a canned sequence of percentages.
    """

    def __init__(self, job_id, passthrough):
        self.job_id = job_id
        self.passthrough = passthrough
        self._buffer = ""

    def write(self, chunk):
        self.passthrough.write(chunk)
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._emit(line.rstrip())
        return len(chunk)

    def flush(self):
        self.passthrough.flush()

    def _emit(self, line):
        stage, pct = _classify(line)
        with jobs_lock:
            job = jobs.get(self.job_id)
            if job is None:
                return
            if stage:
                job["stage"] = stage
                # Never let progress go backwards -- chunked videos revisit
                # earlier markers, and a jumping bar reads as a bug.
                job["progress"] = max(job.get("progress", 0), pct)
            payload = {
                "line": line,
                "stage": job.get("stage"),
                "progress": job.get("progress", 0),
                "elapsed": time.time() - job["started_at"],
            }
        _publish(self.job_id, payload)


class _RouterStream(io.TextIOBase):
    """Process-wide stdout that forwards each write to the job stream of the
    calling thread, falling back to the real stdout everywhere else.

    Used instead of ``contextlib.redirect_stdout`` so that the SSE generator's
    own ``print`` (or any other thread's output) is not swallowed -- and so
    concurrent jobs can't clobber each other's redirect, which a global
    ``redirect_stdout`` would do.
    """

    def __init__(self, real_stdout):
        self.real_stdout = real_stdout
        self._local = threading.local()

    def write(self, chunk):
        stream = getattr(self._local, "stream", None)
        if stream is not None:
            return stream.write(chunk)
        return self.real_stdout.write(chunk)

    def flush(self):
        stream = getattr(self._local, "stream", None)
        if stream is not None:
            stream.flush()
        self.real_stdout.flush()

    def attach(self, stream):
        self._local.stream = stream

    def detach(self):
        self._local.stream = None


_stdout_router = _RouterStream(sys.stdout)
sys.stdout = _stdout_router


def _publish(job_id, payload):
    """Publish an event: write to the job's log, then signal waiting SSE streams.

    The queue carries log indices, not full payloads, so a replay can deliver the
    exact same timestamped event that was recorded when the line actually arrived.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        log = job.setdefault("log", [])
        log.append(payload)
        idx = len(log) - 1
    q = progress_queues.get(job_id)
    if q is not None:
        q.put(idx)


def _worker():
    """Single job runner: take envelopes off the queue, run them via
    background_task. Serial on purpose -- one pipeline run already saturates
    CPU/GPU/whisper, so parallel jobs would just slow each other down."""
    while True:
        item = job_queue.get()
        if item is None:  # shutdown sentinel (not currently used)
            break
        job_id = item["job_id"]
        p = item["params"]
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                continue
            job["_queued"] = False
        try:
            background_task(
                job_id, item["url"], p["num_clips"], p["aspect_ratio"],
                p["format"], p["language"], p["mode"], p["llm_provider"],
                p["api_keys"],
                whisper_device=p["whisper_device"],
                whisper_model=p["whisper_model"],
                clip_length=p["clip_length"],
                overlay_position=p["overlay_position"],
                overlay_margin=p["overlay_margin"],
                overlay_scale=p["overlay_scale"],
                use_overlay_opencv=p["use_overlay_opencv"],
                overlay_vertical_pos=p["overlay_vertical_pos"],
                overlay_margin_bottom=p["overlay_margin_bottom"],
                overlay_margin_left=p["overlay_margin_left"],
                overlay_enabled=p["overlay_enabled"],
                overlay_x=p["overlay_x"],
                overlay_y=p["overlay_y"],
                music_enabled=p["music_enabled"],
                music_file=p["music_file"],
                music_volume=p["music_volume"],
                silence_cut=p["silence_cut"],
                blur_bars=p["blur_bars"],
                captions_enabled=p["captions_enabled"],
                caption_style=p["caption_style"],
                face_track=p["face_track"],
                caption_position=p["caption_position"],
                caption_margin_v=p["caption_margin_v"],
                title_enabled=p.get("title_enabled"),
                title_y_from_bottom=p.get("title_y_from_bottom"),
                title_font_size=p.get("title_font_size"),
                watermark_enabled=p.get("watermark_enabled"),
                watermark_at_sec=p.get("watermark_at_sec"),
                watermark_duration_sec=p.get("watermark_duration_sec"),
                watermark_scale=p.get("watermark_scale"),
                watermark_file=p.get("watermark_file"),
            )
        except Exception:  # background_task already records its own failures
            import traceback
            traceback.print_exc()
        finally:
            job_queue.task_done()


_worker_thread = threading.Thread(target=_worker, name="job-worker", daemon=True)
_worker_thread.start()


def _queue_position(job):
    """0 for running/finished jobs, otherwise the 1-based slot among jobs
    still waiting in the queue.

    The contract: a job that starts immediately reports 0, so 1 means
    "one job is ahead of you" -- the single worker is itself ahead of every
    queued job, hence len(ahead) + 1."""
    if job.get("status") in ("completed", "error"):
        return 0
    if not job.get("_queued"):
        return 0  # the worker has picked it up (or is about to)
    ahead = [
        j for j in jobs.values()
        if j.get("_queued") and j.get("added_at", 0) < job.get("added_at", 0)
    ]
    running = any(
        j.get("status") == "queued" and not j.get("_queued")
        for j in jobs.values()
    )
    return len(ahead) + (1 if running else 0)


def _prune_finished_jobs():
    """Drop the oldest finished jobs so long sessions don't grow unbounded."""
    finished = [
        (jid, j) for jid, j in jobs.items()
        if j.get("status") in ("completed", "error")
    ]
    if len(finished) <= MAX_FINISHED_JOBS:
        _reap_stale_progress_queues()
        return
    finished.sort(key=lambda kv: kv[1].get("finished_at") or 0)
    for jid, _ in finished[: len(finished) - MAX_FINISHED_JOBS]:
        jobs.pop(jid, None)
        progress_queues.pop(jid, None)
    _reap_stale_progress_queues()


def _valid_unit(value):
    """float(value) within [0,1], or None when missing/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if 0.0 <= f <= 1.0 else None


# Music fields accepted from the browser. music_volume is a percent, 0..100.
MUSIC_VOLUME_DEFAULT = 40


def _as_bool(value):
    """GUI truthiness for checkboxes that may arrive as "0"/"1" strings."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _music_volume_pct(value):
    """Clamp an incoming music_volume to an int percent 0..100 (default 40)."""
    try:
        pct = int(float(value))
    except (TypeError, ValueError):
        return MUSIC_VOLUME_DEFAULT
    return max(0, min(100, pct))


def _overrides_from(mode, api_keys, whisper_device=None, whisper_model=None,
                    overlay_position=None, overlay_margin=None, overlay_scale=None,
                    use_overlay_opencv=None,
                    overlay_vertical_pos=None, overlay_margin_bottom=None,
                    overlay_margin_left=None,
                    overlay_enabled=None, overlay_x=None, overlay_y=None,
                    music_enabled=None, music_file=None, music_volume=None,
                    silence_cut=None, blur_bars=None,
                    captions_enabled=None, caption_style=None, face_track=None,
                    caption_position=None, caption_margin_v=None,
                    title_enabled=None, title_y_from_bottom=None,
                    title_font_size=None,
                    watermark_enabled=None, watermark_at_sec=None,
                    watermark_duration_sec=None, watermark_scale=None,
                    watermark_file=None):
    """Translate browser field names into config setting names.

    Secrets arrive either as a real value or as the mask placeholder, which means
    "reuse what is on disk" -- resolve_secret handles both.

    The GUI sends the new 9-position grid (``overlay_position`` + a single
    ``overlay_margin``). The clipper consumes those directly; the legacy
    vertical-pos / margin-bottom / margin-left fields are still accepted for
    backwards compatibility with older saved payloads.
    """
    secret = settings_store.resolve_secret
    out = {}

    # Whisper runs only in local mode; GPU here is the single biggest speedup
    # available, so it is surfaced in the GUI rather than buried in .env.
    if mode == "local":
        if whisper_device:
            out["LOCAL_WHISPER_DEVICE"] = whisper_device
        if whisper_model:
            out["LOCAL_WHISPER_MODEL"] = whisper_model

    # Overlay settings (TikTok watermark) – apply in both API and local modes
    # because clipping/overlay is always done locally.
    if overlay_position is not None:
        out["OVERLAY_POSITION"] = str(overlay_position).strip().lower()
    if overlay_margin is not None:
        out["OVERLAY_MARGIN"] = overlay_margin
    # Legacy fields (old saved settings / direct API use) still map through.
    if overlay_vertical_pos is not None:
        # Convert percent (0‑100) from the GUI to a fraction (0‑1) for the clipper
        try:
            overlay_vertical_pos = float(overlay_vertical_pos) / 100.0
        except ValueError:
            pass  # leave as‑is; the clipper will fall back to its default
        out["OVERLAY_VERTICAL_POS"] = overlay_vertical_pos
    if overlay_margin_bottom is not None:
        out["OVERLAY_MARGIN_BOTTOM"] = overlay_margin_bottom
    if overlay_margin_left is not None:
        out["OVERLAY_MARGIN_LEFT"] = overlay_margin_left
    if overlay_scale is not None:
        out["OVERLAY_SCALE"] = overlay_scale
    if use_overlay_opencv is not None:
        out["USE_OVERLAY_OPENCV"] = use_overlay_opencv

    # Master switch + free-float position. "0"/"1" as strings: the clipper
    # reads them via config.env(), and the value must survive even when falsy
    # (config.set_overrides used to drop falsy values -- see config.env).
    if overlay_enabled is not None:
        if isinstance(overlay_enabled, str):
            truthy = overlay_enabled.strip().lower() not in (
                "", "0", "false", "no", "off")
        else:
            truthy = bool(overlay_enabled)
        out["OVERLAY_ENABLED"] = "1" if truthy else "0"
    fx, fy = _valid_unit(overlay_x), _valid_unit(overlay_y)
    # Only set BOTH when both are valid -- one without the other is
    # meaningless, and omitting lets the clipper's grid fallback apply.
    if fx is not None and fy is not None:
        out["OVERLAY_X"] = str(fx)
        out["OVERLAY_Y"] = str(fy)

    # Background music bed: mixed into clips in both API and local modes, so
    # it maps regardless of mode. MUSIC_FILE is forwarded only when it still
    # points at a real file inside the output dir -- a stale/renamed upload
    # must not crash the pipeline; music then just doesn't play.
    if music_enabled is not None:
        out["MUSIC_ENABLED"] = "1" if _as_bool(music_enabled) else "0"
    if music_file:
        candidate = os.path.realpath(str(music_file))
        output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
        if _same_or_parent(candidate, output_dir) and os.path.isfile(candidate):
            out["MUSIC_FILE"] = candidate
    if music_volume is not None:
        out["MUSIC_VOLUME"] = str(_music_volume_pct(music_volume))

    # Post-processing toggles: silence cut + blurred bars (9:16). Both are
    # read by the clipper via config.env(); defaults live there ("1" each), so
    # only an explicit GUI value is forwarded.
    if silence_cut is not None:
        out["SILENCE_CUT"] = "1" if _as_bool(silence_cut) else "0"
    if blur_bars is not None:
        out["BLUR_BARS"] = "1" if _as_bool(blur_bars) else "0"

    # Captions / face-track toggles. Captions are opt-in (clipper/transcriber
    # default off); face tracking defaults on and is a kill-switch here.
    if captions_enabled is not None:
        out["CAPTIONS_ENABLED"] = "1" if _as_bool(captions_enabled) else "0"
    if caption_style is not None:
        style = str(caption_style).strip().lower()
        if style in ("karaoke", "classic"):
            out["CAPTION_STYLE"] = style
    if caption_position is not None:
        pos = str(caption_position).strip().lower()
        if pos in ("bottom", "center", "top"):
            out["CAPTION_POSITION"] = pos
    if caption_margin_v is not None:
        try:
            out["CAPTION_MARGIN_V"] = str(max(0, min(1200, int(float(caption_margin_v)))))
        except (TypeError, ValueError):
            pass  # leave the env/default value in effect
    if face_track is not None:
        out["FACE_TRACK_ENABLED"] = "1" if _as_bool(face_track) else "0"

    # Highlight title drawn over the video near the bottom (see
    # shorts_generator/local/title_draw.py). Numbers are clamped server-side so
    # a junk form value cannot push the text off-canvas.
    if title_enabled is not None:
        out["TITLE_ENABLED"] = "1" if _as_bool(title_enabled) else "0"
    if title_y_from_bottom is not None:
        try:
            out["TITLE_Y_FROM_BOTTOM"] = str(
                max(100, min(1500, int(float(title_y_from_bottom)))))
        except (TypeError, ValueError):
            pass  # leave the env/default value in effect
    if title_font_size is not None:
        try:
            out["TITLE_FONT_SIZE"] = str(
                max(24, min(200, int(float(title_font_size)))))
        except (TypeError, ValueError):
            pass  # leave the env/default value in effect

    # Frame-pause watermark (see shorts_generator/local/watermark.py): at
    # WATERMARK_AT_SEC the picture freezes for WATERMARK_DURATION_SEC while the
    # uploaded PNG fades in/out. Numbers clamped here; the module re-clamps too
    # because the finalize thread may read the persisted file values directly.
    if watermark_enabled is not None:
        out["WATERMARK_ENABLED"] = "1" if _as_bool(watermark_enabled) else "0"
    if watermark_at_sec is not None:
        try:
            out["WATERMARK_AT_SEC"] = str(
                max(0.0, min(600.0, float(watermark_at_sec))))
        except (TypeError, ValueError):
            pass  # leave the env/default value in effect
    if watermark_duration_sec is not None:
        try:
            out["WATERMARK_DURATION_SEC"] = str(
                max(0.3, min(10.0, float(watermark_duration_sec))))
        except (TypeError, ValueError):
            pass  # leave the env/default value in effect
    if watermark_scale is not None:
        try:
            out["WATERMARK_SCALE"] = str(
                max(5, min(90, int(float(watermark_scale)))))
        except (TypeError, ValueError):
            pass  # leave the env/default value in effect
    # Same stale-file guard as MUSIC_FILE: only forward a path that still
    # points at a real file under the output dir; a renamed/deleted upload
    # must not crash the pipeline, the stage just sits out.
    if watermark_file:
        candidate = os.path.realpath(str(watermark_file))
        output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
        if _same_or_parent(candidate, output_dir) and os.path.isfile(candidate):
            out["WATERMARK_FILE"] = candidate

    if not api_keys:
        return out

    if mode == "api":
        if api_keys.get("muapi"):
            out["MUAPI_API_KEY"] = secret("muapi_key", api_keys["muapi"])
        return out

    pairs = [
        ("openai_key", "OPENAI_API_KEY", True),
        ("openai_model", "OPENAI_MODEL", False),
        ("gemini_key", "GEMINI_API_KEY", True),
        ("gemini_model", "GEMINI_MODEL", False),
        ("ollama_url", "OLLAMA_BASE_URL", False),
        ("ollama_model", "OLLAMA_MODEL", False),
        ("nim_key", "NIM_API_KEY", True),
        ("nim_url", "NIM_BASE_URL", False),
        ("nim_model", "NIM_MODEL", False),
    ]
    for field, setting, is_secret in pairs:
        value = api_keys.get(field)
        if not value:
            continue
        out[setting] = secret(field, value) if is_secret else value
    return out


def background_task(job_id, youtube_url, num_clips, aspect_ratio,
                    download_format, language, mode, llm_provider, api_keys,
                    whisper_device=None, whisper_model=None, clip_length=None,
                    overlay_position=None, overlay_margin=None, overlay_scale=None,
                    use_overlay_opencv=None,
                    overlay_vertical_pos=None, overlay_margin_bottom=None,
                    overlay_margin_left=None,
                    overlay_enabled=None, overlay_x=None, overlay_y=None,
                    music_enabled=None, music_file=None, music_volume=None,
                    silence_cut=None, blur_bars=None,
                    captions_enabled=None, caption_style=None, face_track=None,
                    caption_position=None, caption_margin_v=None,
                    title_enabled=None, title_y_from_bottom=None,
                    title_font_size=None,
                    watermark_enabled=None, watermark_at_sec=None,
                    watermark_duration_sec=None, watermark_scale=None,
                    watermark_file=None):
    """Run generate_shorts, streaming its own log output to the browser."""
    from shorts_generator.config import clear_overrides, set_overrides

    stream = _JobLogStream(job_id, _stdout_router.real_stdout)
    try:
        set_overrides(_overrides_from(mode, api_keys, whisper_device, whisper_model,
                                      overlay_position, overlay_margin, overlay_scale,
                                      use_overlay_opencv,
                                      overlay_vertical_pos, overlay_margin_bottom,
                                      overlay_margin_left,
                                      overlay_enabled, overlay_x, overlay_y,
                                      music_enabled, music_file, music_volume,
                                      silence_cut, blur_bars,
                                      captions_enabled, caption_style, face_track,
                                      caption_position, caption_margin_v,
                                      title_enabled, title_y_from_bottom,
                                      title_font_size,
                                      watermark_enabled, watermark_at_sec,
                                      watermark_duration_sec, watermark_scale,
                                      watermark_file))

        with jobs_lock:
            jobs[job_id]["status"] = "running"
            started_at = jobs[job_id]["started_at"]
        # Real elapsed, not 0: the SSE stream replays backlog before live events,
        # so a hardcoded 0 arriving after replayed lines rewinds the browser timer.
        _publish(job_id, {"stage": "starting", "progress": 5,
                          "elapsed": time.time() - started_at})

        # Route stdout only for this thread: the SSE generator's own prints must
        # not be captured as pipeline output, or they'd feedback-loop forever.
        _stdout_router.attach(stream)
        try:
            result = generate_shorts(
                youtube_url=youtube_url,
                num_clips=num_clips,
                aspect_ratio=aspect_ratio,
                download_format=download_format,
                language=language,
                mode=mode,
                llm_provider=llm_provider,
                clip_length=clip_length,
            )
        finally:
            _stdout_router.detach()

        # Local clips live on disk; rewrite to a URL this server can serve.
        # Keep any subdirectory (shorts land in output/<video_title>/), so the
        # URL matches where the file actually is. Output paths outside the
        # output dir (or already a URL) are left untouched.
        if mode == "local":
            output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
            for short in result.get("shorts") or []:
                path = short.get("clip_url")
                if not path or not os.path.isabs(path):
                    continue
                real = os.path.realpath(path)
                if _same_or_parent(real, output_dir):
                    rel = os.path.relpath(real, output_dir).replace("\\", "/")
                    short["clip_url"] = f"/output/{rel}"
                else:
                    short["clip_url"] = f"/output/{os.path.basename(path)}"

        with jobs_lock:
            job = jobs[job_id]
            job["finished_at"] = time.time()
            elapsed = job["finished_at"] - job["started_at"]
            job.update(status="completed", stage="done", progress=100,
                       result=result, elapsed=elapsed)
            _prune_finished_jobs()

        _publish(job_id, {"stage": "done", "progress": 100,
                          "elapsed": elapsed, "result": result})
        _finish_progress_queue(job_id)

    except Exception as e:
        # Full traceback stays in the server log; the browser only gets a
        # sanitized message -- raw str(e) can leak paths and internal state.
        log.exception("job %s failed", job_id)
        friendly = _humanize_error(e)
        with jobs_lock:
            job = jobs.get(job_id)
            if job is not None:
                job["finished_at"] = time.time()
                elapsed = job["finished_at"] - job["started_at"]
                job.update(status="error", error=friendly, elapsed=elapsed)
                _prune_finished_jobs()
            else:
                elapsed = 0
        _publish(job_id, {"status": "error", "error": friendly, "elapsed": elapsed})
        _finish_progress_queue(job_id)
    finally:
        clear_overrides()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Return saved settings, with secrets masked."""
    return jsonify(settings_store.mask_secrets(settings_store.load()))


@app.route("/api/settings", methods=["POST"])
def post_settings():
    """Persist settings so they survive a restart. Returns the masked result."""
    saved = settings_store.save(request.json or {})
    return jsonify(settings_store.mask_secrets(saved))


MAX_UPLOAD_CHUNK = 1024 * 1024  # read/write a file 1 MB at a time


def _save_upload_limited(file_storage, save_path):
    """Save an uploaded file while enforcing MAX_UPLOAD_BYTES, chunk by chunk.

    MAX_CONTENT_LENGTH relies on the Content-Length header, which a chunked
    upload never sends -- so we count the bytes ourselves and abort at the cap.
    Returns None on success, or "413" after removing the partial file.
    """
    written = 0
    try:
        with open(save_path, "wb") as out:
            while True:
                chunk = file_storage.stream.read(MAX_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise _UploadTooLarge()
                out.write(chunk)
    except _UploadTooLarge:
        try:
            os.remove(save_path)
        except OSError:
            pass
        return "413"
    return None


class _UploadTooLarge(Exception):
    pass


_PAYLOAD_TOO_LARGE_RU = ("Файл слишком большой (лимит "
                         f"{MAX_UPLOAD_BYTES // (1024 ** 3)} ГБ)")


@app.route("/api/upload", methods=["POST"])
def upload_video():
    """Accept a video file from the browser and save it to the uploads directory."""
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    file = request.files["video"]
    if not file or not file.filename:
        return jsonify({"error": "Empty file"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_name = secure_filename(file.filename)
    timestamp = int(time.time() * 1000)
    unique_name = f"{timestamp}_{safe_name}"
    save_path = os.path.join(UPLOAD_DIR, unique_name)

    if _save_upload_limited(file, save_path) is not None:
        return jsonify({"error": _PAYLOAD_TOO_LARGE_RU}), 413
    return jsonify({"path": save_path, "filename": unique_name}), 200


MUSIC_UPLOAD_DIR = os.path.join(os.path.abspath(LOCAL_OUTPUT_DIR), "music")
ALLOWED_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


@app.route("/api/upload/music", methods=["POST"])
def upload_music():
    """Accept a background-music audio file and save it under output/music/."""
    if "music" not in request.files:
        return jsonify({"error": "No music file provided"}), 400

    file = request.files["music"]
    if not file or not file.filename:
        return jsonify({"error": "Empty file"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_MUSIC_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    os.makedirs(MUSIC_UPLOAD_DIR, exist_ok=True)
    safe_name = secure_filename(file.filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400
    # Second-resolution timestamp keeps the name unique per upload, so one
    # user's track can't clobber another's already-referenced file.
    unique_name = f"music_{int(time.time())}_{safe_name}"
    save_path = os.path.join(MUSIC_UPLOAD_DIR, unique_name)

    if _save_upload_limited(file, save_path) is not None:
        return jsonify({"error": _PAYLOAD_TOO_LARGE_RU}), 413
    return jsonify({"ok": True, "filename": unique_name, "path": save_path}), 200


WATERMARK_UPLOAD_DIR = os.path.join(os.path.abspath(LOCAL_OUTPUT_DIR), "uploads")
# Must stay in sync with watermark.WATERMARK_EXTENSIONS -- the finalize stage
# is what reads this file, so a type the module rejects must not reach it.
ALLOWED_WATERMARK_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp",
                                ".mp4", ".mov", ".webm", ".mkv"}


@app.route("/api/upload/watermark", methods=["POST"])
def upload_watermark():
    """Accept a user watermark image or video banner under output/uploads/.

    Unlike music uploads the name is normalized to ``watermark.<ext>``: one
    GUI slot means one current image, and the persisted WATERMARK_FILE path
    must keep pointing at it across re-uploads (the frontend cache-busts the
    preview with ``?t=`` so a same-name overwrite still repaints).
    """
    if "watermark" not in request.files:
        return jsonify({"error": "No watermark file provided"}), 400

    file = request.files["watermark"]
    if not file or not file.filename:
        return jsonify({"error": "Empty file"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_WATERMARK_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    os.makedirs(WATERMARK_UPLOAD_DIR, exist_ok=True)
    filename = f"watermark{ext}"
    save_path = os.path.join(WATERMARK_UPLOAD_DIR, filename)

    if _save_upload_limited(file, save_path) is not None:
        return jsonify({"error": _PAYLOAD_TOO_LARGE_RU}), 413

    # The spec puts WATERMARK_FILE under the endpoint's own control (music
    # instead leaves persistence to the settings form): the finalize thread
    # reads the persisted value when no request overrides exist.
    settings_store.save({"watermark_file": save_path})
    rel = os.path.relpath(save_path, os.path.abspath(LOCAL_OUTPUT_DIR)) \
        .replace("\\", "/")
    return jsonify({"ok": True, "filename": filename, "path": save_path,
                    "url": f"/output/{rel}"}), 200


@app.route("/api/generate", methods=["POST"])
def generate():
    # Accept JSON or a classic form post -- either way `data` is a plain dict.
    data = request.get_json(silent=True) if request.is_json else None
    if data is None:
        data = request.form.to_dict()
    data = data or {}

    def _get(key, default=None):
        v = data.get(key, default)
        return default if v == "" else v

    youtube_url = (_get("url", "") or "").strip()
    source_type = _get("source_type", "url")
    mode = _get("mode", "api")
    llm_provider = _get("llm_provider") or None
    num_clips, err = _parse_num_clips(_get("num_clips", 3))
    if err:
        return jsonify({"error": err}), 400
    aspect_ratio = _get("aspect_ratio", "9:16")
    if aspect_ratio not in _ASPECT_WHITELIST:
        aspect_ratio = "9:16"
    download_format = _get("format", "720")
    if download_format not in _FORMAT_WHITELIST:
        download_format = "720"
    language = _get("language") or None
    whisper_device = _get("whisper_device", "auto")
    whisper_model = _get("whisper_model", "base")
    clip_length = _get("clip_length", "any")
    api_keys = data.get("api_keys", {})
    if not isinstance(api_keys, dict):
        api_keys = {}

    # Provider keys/models moved off the Generate page to /settings: the form
    # no longer sends them, so fill in whatever is persisted on disk. An
    # explicit non-empty form value still wins (direct API use); the settings
    # form already handles the mask placeholder itself.
    persisted = settings_store.load()
    if llm_provider is None and persisted.get("llm_provider"):
        llm_provider = persisted["llm_provider"]
    if whisper_device == "auto" and persisted.get("whisper_device"):
        whisper_device = persisted["whisper_device"]
    if whisper_model == "base" and persisted.get("whisper_model"):
        whisper_model = persisted["whisper_model"]
    if mode == "api":
        if not api_keys.get("muapi") and persisted.get("muapi_key"):
            api_keys = {**api_keys, "muapi": persisted["muapi_key"]}
    else:
        provider_keys = {
            "openai": ("openai_key", "openai_model"),
            "gemini": ("gemini_key", "gemini_model"),
            "ollama": ("ollama_url", "ollama_model"),
            "nim": ("nim_key", "nim_url", "nim_model"),
        }
        for field in provider_keys.get(llm_provider or "openai", ()):
            if not api_keys.get(field) and persisted.get(field):
                api_keys = {**api_keys, field: persisted[field]}

    # Overlay settings from GUI (new 9-position grid + margin + scale)
    overlay_position = _get("overlay_position")
    overlay_margin = _get("overlay_margin")
    overlay_scale = _get("overlay_scale")
    use_overlay_opencv = _get("use_overlay_opencv")
    # Legacy fields (kept so older saved settings still work if reused)
    overlay_vertical_pos = _get("overlay_vertical_pos")
    overlay_margin_bottom = _get("overlay_margin_bottom")
    overlay_margin_left = _get("overlay_margin_left")
    # Master switch + free-float center position (fractions 0..1).
    # JSON null clears the preference; absent means "leave default".
    overlay_enabled = data.get("overlay_enabled")
    overlay_x = data.get("overlay_x")
    overlay_y = data.get("overlay_y")
    # Background music bed: checkbox + uploaded file path + volume percent.
    music_enabled = data.get("music_enabled")
    music_file = _get("music_file")
    music_volume = data.get("music_volume")
    # Post-processing toggles: cut silences + blurred bars (9:16 only).
    silence_cut = data.get("silence_cut")
    blur_bars = data.get("blur_bars")
    # Captions (opt-in) + face tracking (defaults on; this is the kill-switch).
    captions_enabled = data.get("captions_enabled")
    caption_style = _get("caption_style")
    face_track = data.get("face_track")
    caption_position = _get("caption_position")
    caption_margin_v = data.get("caption_margin_v")

    if not youtube_url:
        return jsonify({"error": "Missing URL or file path"}), 400

    # Local files can only be processed by the local pipeline; API mode cannot
    # reach a path that lives on the user's machine.
    if source_type == "file" and mode != "local":
        return jsonify({"error": "Local files require Local mode"}), 400

    # SSRF guard. The allow-list applies to anything that IS a URL, regardless
    # of what source_type claims -- Windows paths come in as "C:/..." (which
    # urlparse reads as a "c:" scheme, not http), so an explicit-URL form value
    # with a pasted local path must stay usable. Only real http(s) URLs are
    # gated; everything else is treated as a local path (mode rules above
    # still apply). Runs BEFORE anything is dispatched to the downloader.
    parsed, is_http_url = _parse_url(youtube_url)
    if is_http_url:
        if not _is_allowed_video_url(youtube_url):
            log.warning("rejected non-allow-listed URL in /api/generate: %r",
                        youtube_url[:200])
            return jsonify({"error": "Поддерживаются только ссылки на YouTube "
                                     "(youtube.com, music.youtube.com, youtu.be)"}), 400
        if not source_type:
            source_type = "url"

    job_id = f"job_{int(time.time() * 1000)}"
    params = {
        "num_clips": num_clips,
        "aspect_ratio": aspect_ratio,
        "format": download_format,
        "language": language,
        "mode": mode,
        "llm_provider": llm_provider,
        "api_keys": api_keys,
        "whisper_device": whisper_device,
        "whisper_model": whisper_model,
        "clip_length": clip_length,
        "overlay_position": overlay_position,
        "overlay_margin": overlay_margin,
        "overlay_scale": overlay_scale,
        "use_overlay_opencv": use_overlay_opencv,
        "overlay_vertical_pos": overlay_vertical_pos,
        "overlay_margin_bottom": overlay_margin_bottom,
        "overlay_margin_left": overlay_margin_left,
        "overlay_enabled": overlay_enabled,
        "overlay_x": overlay_x,
        "overlay_y": overlay_y,
        "music_enabled": music_enabled,
        "music_file": music_file,
        "music_volume": music_volume,
        "silence_cut": silence_cut,
        "blur_bars": blur_bars,
        "captions_enabled": captions_enabled,
        "caption_style": caption_style,
        "face_track": face_track,
        "caption_position": caption_position,
        "caption_margin_v": caption_margin_v,
        "title_enabled": data.get("title_enabled"),
        "title_y_from_bottom": data.get("title_y_from_bottom"),
        "title_font_size": data.get("title_font_size"),
        "watermark_enabled": data.get("watermark_enabled"),
        "watermark_at_sec": data.get("watermark_at_sec"),
        "watermark_duration_sec": data.get("watermark_duration_sec"),
        "watermark_scale": data.get("watermark_scale"),
        "watermark_file": data.get("watermark_file"),
    }
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "url": youtube_url,
            "added_at": time.time(),
            "_queued": True,  # still sitting in job_queue (vs picked up)
            "started_at": time.time(),
            "log": [],
            "aspect_ratio": aspect_ratio,
            # Echo mode/llm_provider so /api/shorts/finalize can rebuild the
            # same per-request overrides as the background run without having
            # to guess them from settings.local.json. `params` already holds
            # the GUI fields; storing them here lets the finalize endpoint
            # re-apply exactly what the user saw on screen.
            "mode": mode,
            "llm_provider": llm_provider,
            "_params": params,
        }
        progress_queues[job_id] = queue.Queue()

    # Persist all settings from this form submission, including the new fields,
    # so they reload on the next page visit.
    settings_store.save({
        "url": youtube_url if source_type == "url" else "",
        "source_type": source_type,
        "mode": mode,
        "llm_provider": llm_provider or "",
        "num_clips": num_clips,
        "aspect_ratio": aspect_ratio,
        "format": download_format,
        "language": language or "",
        "whisper_device": whisper_device,
        "whisper_model": whisper_model,
        "clip_length": clip_length,
        "overlay_position": overlay_position,
        "overlay_margin": overlay_margin,
        "overlay_scale": overlay_scale,
        "use_overlay_opencv": use_overlay_opencv,
        "overlay_enabled": overlay_enabled,
        "overlay_x": overlay_x,
        "overlay_y": overlay_y,
        "music_enabled": music_enabled,
        "music_file": music_file,
        "music_volume": music_volume,
        "silence_cut": silence_cut,
        "blur_bars": blur_bars,
        "captions_enabled": captions_enabled,
        "caption_style": caption_style,
        "face_track": face_track,
        "caption_position": caption_position,
        "caption_margin_v": caption_margin_v,
        "title_enabled": data.get("title_enabled"),
        "title_y_from_bottom": data.get("title_y_from_bottom"),
        "title_font_size": data.get("title_font_size"),
        "watermark_enabled": data.get("watermark_enabled"),
        "watermark_at_sec": data.get("watermark_at_sec"),
        "watermark_duration_sec": data.get("watermark_duration_sec"),
        "watermark_scale": data.get("watermark_scale"),
        "watermark_file": data.get("watermark_file"),
        **{k: v for k, v in api_keys.items() if v},
    })

    job_queue.put({"job_id": job_id, "url": youtube_url, "params": params})

    # Read under the lock: the worker may already be rewriting this job.
    with jobs_lock:
        position = _queue_position(jobs[job_id])
    return jsonify({"job_id": job_id, "position": position}), 202


@app.route("/api/jobs")
def list_jobs():
    with jobs_lock:
        snapshot = [(jid, dict(j)) for jid, j in jobs.items()]

    def _sort_key(item):
        _jid, j = item
        rank = {"running": 0, "queued": 1}.get(j.get("status"), 2)
        added = j.get("added_at", 0)
        # finished: newest first
        return (rank, added if rank < 2 else -added)

    out = []
    for jid, j in sorted(snapshot, key=_sort_key):
        out.append({
            "job_id": jid,
            "url": j.get("url", ""),
            "status": j.get("status"),
            "stage": j.get("stage"),
            "position": _queue_position(j),
            "added_at": j.get("added_at"),
            "has_result": bool(j.get("result")),
        })
    return jsonify({"jobs": out})


@app.route("/api/jobs/<job_id>/rerun", methods=["POST"])
def rerun_job(job_id):
    """Re-enqueue a job with the exact _params it last ran with.

    The job's settings snapshot is kept under ``_params``; we clone it rather
    than re-derive from the settings file so a rerun is deterministic.
    """
    with jobs_lock:
        src = jobs.get(job_id)
        if src is None:
            return jsonify({"error": "Job not found"}), 404
        params = dict(src.get("_params") or {})
        url = src.get("url", "")
    if not url or not params:
        return jsonify({"error": "Job has no params snapshot"}), 400

    new_job_id = f"job_{int(time.time() * 1000)}"
    aspect = (params.get("aspect_ratio") or "9:16").strip() or "9:16"
    with jobs_lock:
        jobs[new_job_id] = {
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "url": url,
            "added_at": time.time(),
            "_queued": True,
            "started_at": time.time(),
            "log": [],
            "aspect_ratio": aspect,
            "mode": params.get("mode", src.get("mode", "local")),
            "llm_provider": params.get("llm_provider"),
            "_params": params,
        }
        progress_queues[new_job_id] = queue.Queue()
    job_queue.put({"job_id": new_job_id, "url": url, "params": params})
    with jobs_lock:
        position = _queue_position(jobs[new_job_id])
    return jsonify({"job_id": new_job_id, "position": position}), 202


@app.route("/api/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        snapshot = dict(job)
        if snapshot.get("status") in ("queued", "running"):
            snapshot["elapsed"] = time.time() - snapshot["started_at"]
    return jsonify(snapshot)


@app.route("/api/progress/<job_id>")
def progress_stream(job_id):
    """Server-Sent Events stream: one event per pipeline log line."""
    def events():
        q = progress_queues.get(job_id)
        if q is None:
            yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
            return

        # Replay everything already in the log, then wait for new indices from
        # the queue. The queue carries integers (log indices), not full payloads,
        # so replay delivers the exact timestamped events that were recorded when
        # the lines actually arrived — no clock skew, no duplicates.
        with jobs_lock:
            job = jobs.get(job_id, {})
            backlog = list(job.get("log", []))
            next_idx = len(backlog)

        for event in backlog:
            yield f"data: {json.dumps(event)}\n\n"

        # A terminal status at this point means everything already happened
        # before we connected -- replay was the whole story, close the stream.
        with jobs_lock:
            if _is_terminal_job(jobs.get(job_id)):
                return

        deadline = time.monotonic() + SSE_MAX_LIFETIME
        while True:
            try:
                idx = q.get(timeout=15)
            except queue.Empty:
                # Keepalive loop: a job stuck on a >15s stage (big uploads,
                # whisper cold start) must still close the stream once it
                # reaches a terminal state instead of hanging forever.
                with jobs_lock:
                    if _is_terminal_job(jobs.get(job_id)):
                        return
                if time.monotonic() > deadline:
                    return  # absolute cap, even if the job never finishes
                yield ": keepalive\n\n"
                continue

            # Skip indices already covered by the backlog replay above -- a
            # reconnecting browser would otherwise see those lines twice.
            if idx < next_idx:
                continue
            next_idx = idx + 1

            with jobs_lock:
                log = jobs.get(job_id, {}).get("log", [])
                event = log[idx] if idx < len(log) else None
            if event is None:
                continue

            yield f"data: {json.dumps(event)}\n\n"
            if event.get("stage") == "done" or event.get("status") == "error":
                break

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _resolve_output_safe(rel):
    """Resolve a /output/-relative path, refusing anything that escapes the
    output dir. Returns (abs_path, safe_relative_posix_path) or (None, reason).
    `rel` is relative to the caller, so absolute paths, drive letters and '..'
    components are all rejected before the filesystem is touched.
    """
    if not rel:
        return None, "empty path"
    rel = str(rel).replace("\\", "/").lstrip("/")
    if os.path.isabs(rel) or (len(rel) > 1 and rel[1] == ":"):
        return None, "absolute paths are not allowed"
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts or any(part.startswith("..") for part in parts):
        return None, "invalid path"
    safe_rel = "/".join(parts)
    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    abs_path = os.path.realpath(os.path.join(output_dir, *parts))
    if os.path.commonpath([output_dir, abs_path]) != output_dir:
        return None, "path escapes the output directory"
    return abs_path, safe_rel


def _url_to_output_path(url):
    """Map an /output/<...> URL to (abs_path, safe_rel). Rejects other URLs,
    wrong methods surface as 400s at the route layer."""
    if not isinstance(url, str):
        return None, None
    url = url.strip().split("?", 1)[0].split("#", 1)[0]
    if not url.startswith("/output/"):
        return None, None
    return _resolve_output_safe(url[len("/output/"):])


def _same_or_parent(path, parent):
    try:
        return os.path.commonpath([path, parent]) == parent
    except (ValueError, OSError):
        return False  # different drives, etc.


def _ffprobe_duration(path):
    """Duration in seconds (float) via ffprobe, or None when ffprobe is
    unavailable or the file can't be probed. Never raises."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return round(float(out.stdout.strip()), 3)
    except (TypeError, ValueError):
        return None


@app.route("/output/<path:filename>")
def serve_output(filename):
    # Nested paths (shorts live in output/<video_title>/ now) are fine, but
    # traversal outside the output dir must 404, not serve arbitrary files.
    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    if not os.path.isdir(output_dir):
        return jsonify({"error": "Output directory not found"}), 404
    abs_path, safe_rel = _resolve_output_safe(filename)
    if abs_path is None or not os.path.isfile(abs_path):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(output_dir, safe_rel)


def _job_short_files(job, output_dir):
    """Existing, in-output-dir absolute paths of clips referenced by a job's
    result (deduped, result order preserved)."""
    paths, seen = [], set()
    result = job.get("result") or {}
    for short in result.get("shorts") or []:
        clip_url = short.get("clip_url")
        if not isinstance(clip_url, str):
            continue
        # Results carry absolute on-disk paths; an /output/ URL is accepted
        # too, so a stored result from before a restart can still be listed.
        if clip_url.startswith("/output/"):
            abs_path, _ = _resolve_output_safe(clip_url[len("/output/"):])
        else:
            abs_path = os.path.realpath(clip_url) if clip_url else None
        if not abs_path or abs_path in seen:
            continue
        if not _same_or_parent(abs_path, output_dir):
            continue
        if not os.path.isfile(abs_path):
            continue
        seen.add(abs_path)
        paths.append(abs_path)
    return paths


@app.route("/api/jobs/<job_id>/shorts")
def job_shorts(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        result = dict(job.get("result") or {}) if job.get("result") else None
        snapshot = dict(job)
    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    shorts = []
    for abs_path in _job_short_files(snapshot, output_dir):
        rel = os.path.relpath(abs_path, output_dir).replace("\\", "/")
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0
        # Recover the highlight title for this clip so the review UI can offer
        # saving under it (same path-matching rules as _job_short_files).
        title = ""
        for short in (result.get("shorts") or []) if result else []:
            clip_url = short.get("clip_url")
            if not isinstance(clip_url, str):
                continue
            if clip_url.startswith("/output/"):
                short_abs, _ = _resolve_output_safe(clip_url[len("/output/"):])
            else:
                short_abs = os.path.realpath(clip_url) if clip_url else None
            if short_abs == abs_path:
                title = (short.get("title") or "").strip()
                break
        shorts.append({
            "name": os.path.basename(abs_path),
            "title": title,
            "url": f"/output/{rel}",
            "size_bytes": size,
            "duration_sec": _ffprobe_duration(abs_path),
            # Effects were already baked in if the finalize step left its backup.
            "finalized": os.path.exists(abs_path + ".draft.mp4"),
            # A clip already approved lives under output/saved/ — mark it so the
            # review panel keeps its «Сохранено» state across a page reload.
            "saved": rel == "saved" or rel.startswith("saved/"),
        })
    return jsonify({"shorts": shorts})


# Temp leftovers written next to the draft by the save flow / finalize.
# Swept once the final clip lands in output/saved/ so a crash never litters.
_SAVE_LEFTOVER_SUFFIXES = (".tmp_save.mp4", ".prerender.mp4", ".overlay.mp4",
                           ".music.mp4", ".silent.mp4")
_FINALIZE_LEFTOVER_SUFFIXES = (".prerender.mp4", ".overlay.mp4", ".music.mp4")


# Thumbnails for saved clips (persistent history) live here, served through
# the same /output/<path> route as the clips themselves.
THUMB_DIR_REL = "thumbs"


def _history_lookup_for(job, draft_abs):
    """Snapshot the job metadata the history entry needs, taken BEFORE the
    draft is moved away: score + duration from the matching short entry and
    the video's title from the job's params."""
    lookup = {"score": None, "duration_sec": None, "source_title": ""}
    if not job:
        return lookup
    draft_stem = os.path.splitext(os.path.basename(draft_abs))[0]
    for short in (job.get("result") or {}).get("shorts") or []:
        clip_url = short.get("clip_url")
        if not isinstance(clip_url, str):
            continue
        if clip_url.startswith("/output/"):
            short_abs, _ = _resolve_output_safe(clip_url[len("/output/"):])
        else:
            short_abs = os.path.realpath(clip_url) if clip_url else None
        if short_abs == draft_abs or \
                os.path.splitext(os.path.basename(short_abs or ""))[0] == draft_stem:
            lookup["score"] = short.get("score")
            lookup["duration_sec"] = short.get("duration")
            break
    p = job.get("_params") or {}
    lookup["source_title"] = (p.get("source_title") or p.get("video_title")
                              or "").strip()
    return lookup


def _history_for_saved_clip(lookup, final_path, aspect):
    """Record a freshly saved clip in the persistent history; never fails.

    ``lookup`` is the pre-removal snapshot from _history_lookup_for (the
    draft is already moved away by the time we run -- every job-derived
    value must come from it, not from disk). The thumbnail is generated from
    the SAVED file into output/thumbs/; any thumbnail/store hiccup is logged
    and swallowed so a save never turns into an error after the file already
    landed in saved/. Returns the stored entry (or None on failure).
    """
    from shorts_generator.local.thumbgen import make_thumbnail

    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    rel = os.path.relpath(os.path.realpath(final_path), output_dir).replace("\\", "/")
    stem = os.path.splitext(os.path.basename(final_path))[0]

    thumb_url = None
    try:
        thumb_dir = os.path.join(output_dir, THUMB_DIR_REL)
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_abs = make_thumbnail(
            final_path, out_path=os.path.join(thumb_dir, stem + ".jpg"),
            title=False)
        thumb_rel = os.path.relpath(os.path.realpath(thumb_abs), output_dir) \
            .replace("\\", "/")
        thumb_url = f"/output/{thumb_rel}"
    except Exception as e:  # a missing thumb never fails the save
        print(f"[history] thumbnail failed for {rel}: {e}", flush=True)

    entry = history.add_clip(
        title=stem,
        source_title=lookup.get("source_title") or "",
        saved_url=f"/output/{rel}",
        thumb_url=thumb_url,
        score=lookup.get("score"),
        duration_sec=lookup.get("duration_sec"),
        aspect_ratio=aspect,
    )
    print(f"[history] recorded {entry['id']} -> {rel}", flush=True)
    return entry


def _remove_thumb_file(thumb_url):
    """Best-effort: delete the history thumbnail behind an /output/ URL."""
    if not thumb_url or not isinstance(thumb_url, str):
        return
    if not thumb_url.startswith("/output/"):
        return
    abs_path, _ = _resolve_output_safe(thumb_url[len("/output/"):])
    if abs_path and os.path.isfile(abs_path):
        try:
            os.remove(abs_path)
        except OSError:
            pass


def _history_remove_by_saved_url(saved_url):
    """Drop history entries pointing at ``saved_url`` (a saved clip just got
    deleted on disk) and their thumbnail files. Non-fatal by design."""
    try:
        for entry in history.list_history():
            if entry.get("saved_url") == saved_url:
                if history.delete_clip(entry.get("id") or ""):
                    _remove_thumb_file(entry.get("thumb_url"))
                    print(f"[history] removed {entry.get('id')} (file deleted)",
                          flush=True)
    except Exception as e:
        print(f"[history] cleanup after delete failed: {e}", flush=True)


def _do_save(url, title=None, aspect_requested=None):
    """Approve one draft: reframe + effects, then move to output/saved/.

    Returns ``(response_dict, http_status)`` -- the same payload/status
    ``POST /api/shorts/save`` used to build inline. ``url`` must be an
    ``/output/...`` path of a draft that belongs to a known job; ``title``
    (optional highlight title) renames the saved file; ``aspect_requested``
    overrides the job aspect.

    On any failure the draft is left untouched so the user can retry.
    """
    from shorts_generator.config import clear_overrides, set_overrides
    from shorts_generator.local.blurpad import blurpad_enabled_for
    from shorts_generator.local.clipper import _reframe_vertical, finalize_clip_local
    from shorts_generator.naming import _safe_title_name

    abs_path, safe_rel = _url_to_output_path((url or "").strip())
    if abs_path is None:
        return {"error": "url must be an /output/... path"}, 400
    if safe_rel == "saved" or safe_rel.startswith("saved/"):
        return {"error": "clip is already in saved/"}, 400
    if not os.path.isfile(abs_path):
        return {"error": "File not found"}, 404

    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    job = None
    draft_target = ""
    with jobs_lock:
        snapshot = [dict(j) for j in jobs.values()]
    for _job in snapshot:
        if abs_path in _job_short_files(_job, output_dir):
            job = _job
            for short in (_job.get("result") or {}).get("shorts") or []:
                draft_target = (short.get("target_aspect") or "").strip()
                if draft_target:
                    break
            break
    # No job claims this file → it isn't a draft we produced (stale foreign
    # file in output/). Refuse: save applies effects only to known drafts,
    # and it deletes the source afterwards.
    if job is None:
        return {"error": "clip is not a draft of any known job"}, 404
    lookup = _history_lookup_for(job, abs_path)
    requested = (aspect_requested or "").strip()
    aspect = requested or ((job or {}).get("aspect_ratio") or "").strip() \
        or draft_target or "9:16"

    p = (job or {}).get("_params") or {}
    overrides = _overrides_from(
        (job or {}).get("mode") or p.get("mode") or "local",
        p.get("api_keys") or {},
        p.get("whisper_device"), p.get("whisper_model"),
        p.get("overlay_position"), p.get("overlay_margin"), p.get("overlay_scale"),
        p.get("use_overlay_opencv"), p.get("overlay_vertical_pos"),
        p.get("overlay_margin_bottom"), p.get("overlay_margin_left"),
        p.get("overlay_enabled"), p.get("overlay_x"), p.get("overlay_y"),
        p.get("music_enabled"), p.get("music_file"), p.get("music_volume"),
        p.get("silence_cut"), p.get("blur_bars"),
        p.get("captions_enabled"), p.get("caption_style"), p.get("face_track"),
        p.get("caption_position"), p.get("caption_margin_v"),
        p.get("title_enabled"), p.get("title_y_from_bottom"),
        p.get("title_font_size"),
        p.get("watermark_enabled"), p.get("watermark_at_sec"),
        p.get("watermark_duration_sec"), p.get("watermark_scale"),
        p.get("watermark_file"),
    )

    # Work on a temp sibling, never on the draft itself.
    tmp = abs_path + ".tmp_save.mp4"
    draft_backup = abs_path + ".draft.mp4"
    leftover = [abs_path + s for s in _SAVE_LEFTOVER_SUFFIXES]
    # Caption sidecar lives next to the DRAFT (<draft>.mp4.ass), but finalize
    # looks for <tmp>.ass by default -- pass it explicitly or captions are
    # silently skipped even when enabled.
    captions_ass = abs_path + ".ass"
    if not os.path.isfile(captions_ass):
        captions_ass = None

    set_overrides(overrides)
    try:
        # 9:16-with-blur-bars: the draft is ALREADY landscape 16:9 (drafts are
        # rendered horizontally on purpose). Reframing it to 602x1072 first and
        # THEN blur-padding would force the foreground to re-scale to the full
        # 1080x1920 canvas and leave no room for bars -- the bug where the blur
        # silently became an expensive plain re-encode. So in that one combo we
        # hand the landscape draft straight to finalize: blurpad scales
        # width-to-canvas (1920x1080 -> 1080x608), producing the TikTok bars.
        # Non-9:16 aspect + 9:16-without-bars still go through the classic
        # face-tracked reframe.
        skip_reframe = aspect == "9:16" and blurpad_enabled_for("9:16")
        if skip_reframe:
            print("[save] blur bars on: skipping reframe, blurpad takes the "
                  "landscape draft directly", flush=True)
            shutil.copy2(abs_path, tmp)
        else:
            _reframe_vertical(abs_path, tmp, aspect)
        # The optional payload title doubles as (a) the saved filename and
        # (b) the text burned into the video near the bottom.  Forward it here.
        finalize_clip_local(tmp, aspect, captions_ass=captions_ass,
                            title_text=(title or ""))
    except Exception as e:
        for path in leftover:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        return {"error": f"save failed: {e}"}, 500
    finally:
        clear_overrides()

    saved_dir = os.path.join(output_dir, "saved", os.path.dirname(safe_rel))
    try:
        os.makedirs(saved_dir, exist_ok=True)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"error": f"could not create saved dir: {e}"}, 500
    # Optional highlight title: when it sanitizes to something usable it
    # replaces the draft basename, and collisions get a _2/_3/... suffix.
    safe_title = _safe_title_name(title or "")
    ext = os.path.splitext(abs_path)[1] or ".mp4"
    if safe_title:
        final_name = safe_title + ext
        n = 1
        while os.path.exists(os.path.join(saved_dir, final_name)):
            n += 1
            final_name = f"{safe_title}_{n}{ext}"
    else:
        final_name = os.path.basename(abs_path)
    final_path = os.path.join(saved_dir, final_name)
    final_part = final_path + ".part"

    # The caption sidecar travels with its clip: delete on burn, move to saved/
    # otherwise (so the review panel still knows captions belong to this clip).
    # Renamed to match the final clip's basename when a title renamed the clip.
    final_ass = os.path.join(saved_dir, final_name + ".ass") \
        if captions_ass else None

    try:
        shutil.move(tmp, final_part)
        os.replace(final_part, final_path)
        os.remove(abs_path)
        if captions_ass and final_ass:
            try:
                shutil.move(captions_ass, final_ass)
            except OSError:
                pass
    except OSError as e:
        try:
            if not os.path.isfile(final_path) and not os.path.isfile(abs_path):
                if os.path.isfile(final_part):
                    shutil.move(final_part, abs_path)
                elif os.path.isfile(tmp):
                    shutil.move(tmp, abs_path)
        except OSError:
            pass
        return {"error": f"could not move into saved/: {e}"}, 500

    for path in leftover + [draft_backup, final_part]:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    rel = os.path.relpath(os.path.realpath(final_path), output_dir).replace("\\", "/")
    body = {"ok": True, "url": f"/output/{rel}", "saved": True,
            "aspect_ratio": aspect,
            "name": os.path.splitext(final_name)[0]}
    try:
        entry = _history_for_saved_clip(lookup, final_path, aspect)
        if entry:
            body["history_id"] = entry.get("id")
    except Exception as e:
        # The file is already in saved/ and the save contract is fulfilled;
        # history is best-effort. Never let it break the response.
        print(f"[history] could not record saved clip {rel}: {e}", flush=True)
    return body, 200


@app.route("/api/shorts/save", methods=["POST"])
def save_short():
    """Approve a draft: reframe + effects, then move to output/saved/.

    Thin wrapper around ``_do_save`` -- the payload fields are ``url``,
    optional ``title`` and optional ``aspect_ratio``.
    """
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    body, status = _do_save(data.get("url"), data.get("title"),
                            data.get("aspect_ratio"))
    return jsonify(body), status


# Cap for /api/shorts/save_batch -- the queue is a convenience over clicking
# «Сохранить» per card, not a bulk-export API; 50 is far above any review set.
_SAVE_BATCH_MAX = 50


@app.route("/api/shorts/save_batch", methods=["POST"])
def save_shorts_batch():
    """Save several drafts in one request, sequentially.

    Payload: ``{"items": [{"url", "title?", "aspect_ratio?"}, ...]}`` (max
    ``_SAVE_BATCH_MAX``). Each item goes through ``_do_save`` one at a time --
    ffmpeg encode is heavy, so never in parallel. A failed item does not abort
    the rest; the response is always HTTP 200 once the payload parsed, with a
    per-item ``{url, ok, url?, name?, error?}`` entry.
    """
    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict) \
            or not isinstance(data.get("items"), list):
        return jsonify({"error": "items must be a list of {url, title?, aspect_ratio?}"}), 400
    items = data["items"]
    if not items:
        return jsonify({"error": "items is empty"}), 400
    if len(items) > _SAVE_BATCH_MAX:
        return jsonify({"error": f"too many items (max {_SAVE_BATCH_MAX})"}), 400

    results = []
    for item in items:
        item = item if isinstance(item, dict) else {}
        url = item.get("url")
        body, _status = _do_save(url, item.get("title"), item.get("aspect_ratio"))
        entry = {"url": (url or ""), "ok": bool(body.get("ok"))}
        if entry["ok"]:
            entry["url"] = body.get("url")
            entry["name"] = body.get("name")
        else:
            entry["error"] = body.get("error") or "save failed"
        results.append(entry)
    return jsonify({"results": results}), 200


@app.route("/api/shorts/delete", methods=["POST"])
def delete_short():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    abs_path, _safe_rel = _url_to_output_path((data.get("url") or "").strip())
    if abs_path is None:
        return jsonify({"error": "url must be an /output/... path"}), 400
    if not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404
    try:
        os.remove(abs_path)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    _history_remove_by_saved_url(f"/output/{_safe_rel}")
    return jsonify({"ok": True})


@app.route("/api/history")
def get_history():
    """Persistent clip history, newest first. Lazily backfills entries for
    saved clips that predate the history file (no thumbnail regen for those
    -- thumb_url stays null until the normal save flow creates one)."""
    history.merge_disk_scan(os.path.realpath(LOCAL_OUTPUT_DIR))
    return jsonify({"clips": history.list_history()}), 200


@app.route("/api/history/favorite", methods=["POST"])
def favorite_history():
    """Toggle the favorite flag on one entry. Body: {id} -> entry or 404."""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    entry = history.toggle_favorite((data.get("id") or "").strip())
    if entry is None:
        return jsonify({"error": "Clip not found in history"}), 404
    return jsonify(entry), 200


@app.route("/api/history/delete", methods=["POST"])
def delete_history():
    """Delete a history entry AND its files (saved clip + thumbnail).

    The video path goes through the same safe output-dir resolution as
    /api/shorts/delete, so a stored URL can never point outside output/. A
    missing file just drops the entry (it was probably deleted already).
    """
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    clip_id = (data.get("id") or "").strip()
    entry = next((c for c in history.list_history() if c.get("id") == clip_id),
                 None)
    if entry is None:
        return jsonify({"error": "Clip not found in history"}), 404
    saved_url = entry.get("saved_url") or ""
    if saved_url.startswith("/output/"):
        abs_path, _ = _resolve_output_safe(saved_url[len("/output/"):])
        if abs_path and os.path.isfile(abs_path):
            try:
                os.remove(abs_path)
            except OSError as e:
                return jsonify({"error": str(e)}), 500
    _remove_thumb_file(entry.get("thumb_url"))
    history.delete_clip(clip_id)
    print(f"[history] deleted {clip_id}", flush=True)
    return jsonify({"ok": True}), 200


@app.route("/api/shorts/trim", methods=["POST"])
def trim_short():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    abs_path, _safe_rel = _url_to_output_path((data.get("url") or "").strip())
    if abs_path is None:
        return jsonify({"error": "url must be an /output/... path"}), 400
    if not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404

    try:
        start = float(data.get("start_offset"))
        end = float(data.get("end_offset"))
    except (TypeError, ValueError):
        return jsonify({"error": "start_offset/end_offset must be numbers"}), 400
    if not (start >= 0 and end > start):
        return jsonify({"error": "need 0 <= start_offset < end_offset"}), 400

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return jsonify({"error": "ffmpeg not found on PATH"}), 503

    directory, name = os.path.split(abs_path)
    stem, ext = os.path.splitext(name)
    if not ext or ext.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        ext = ".mp4"
    # Never overwrite the source: find the next free _trimmed[_N] name.
    candidate = os.path.join(directory, f"{stem}_trimmed{ext}")
    n = 1
    while os.path.exists(candidate):
        n += 1
        candidate = os.path.join(directory, f"{stem}_trimmed_{n}{ext}")

    # -ss/-to before -i: fast seek; re-encode so the cut starts on a keyframe
    # boundary instead of freezing until the next one.
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-ss", f"{start}", "-to", f"{end}", "-i", abs_path,
           "-c:v", "libx264", "-c:a", "aac", candidate]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "ffmpeg timed out"}), 500
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-400:]
        return jsonify({"error": f"ffmpeg failed: {tail}"}), 500

    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    rel = os.path.relpath(os.path.realpath(candidate), output_dir).replace("\\", "/")
    return jsonify({
        "ok": True,
        "url": f"/output/{rel}",
        "new_name": os.path.basename(candidate),
    })


@app.route("/api/shorts/thumbnail", methods=["POST"])
def thumbnail_short():
    """Generate a cover JPEG for a short (see local/thumbgen.py for the design).

    Request: {url: "/output/...", title?: text overlay, at_percent?: 1..90}.
    Response: {ok: true, url: "/output/.../name_thumb[_N].jpg"} — the frame is
    written next to the clip and served through the same /output/ route.
    Import is lazy and LOCAL_OUTPUT_DIR is read at request time so tests can
    redirect both after importing this module.
    """
    from shorts_generator.local.thumbgen import make_thumbnail

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    abs_path, _safe_rel = _url_to_output_path((data.get("url") or "").strip())
    if abs_path is None:
        return jsonify({"error": "url must be an /output/... path"}), 400
    if not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404

    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg not found on PATH"}), 503

    title = data.get("title")
    if title is not None and not isinstance(title, str):
        title = str(title)
    at_percent = data.get("at_percent")

    try:
        out_path = make_thumbnail(abs_path, title=title, at_percent=at_percent)
    except ValueError as e:  # bad at_percent etc.
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    rel = os.path.relpath(os.path.realpath(out_path), output_dir).replace("\\", "/")
    return jsonify({"ok": True, "url": f"/output/{rel}"})


@app.route("/api/shorts/finalize", methods=["POST"])
def finalize_short():
    """Apply the visual effects (blur bars / TikTok overlay / music) to a draft.

    Drafts are rendered WITHOUT effects so the GPU isn't spent on clips the
    user will reject. This runs only after explicit approval in the review
    panel. The draft is replaced in place (a ``.draft.mp4`` backup is kept so
    the operation is never destructive on failure).
    """
    from shorts_generator.config import clear_overrides, set_overrides
    from shorts_generator.local.clipper import finalize_clip_local

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    abs_path, safe_rel = _url_to_output_path((data.get("url") or "").strip())
    if abs_path is None:
        return jsonify({"error": "url must be an /output/... path"}), 400
    if not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404

    # The draft has to belong to a job this server produced (paths recorded in
    # its result). A known job wins, so its params drive per-request overrides.
    # With no job (private file like ../adds.txt never matches) we must avoid
    # leaking its existence -- return a generic 404 unless the path is plainly
    # not sensitive: dot-paths, hidden names, or anything outside a normal
    # clip/ drafts tree would let an enum confirm presence by response shape.
    job = None
    draft_aspect = ""
    with jobs_lock:
        snapshot = [dict(j) for j in jobs.values()]
    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    for _job in snapshot:
        if abs_path in _job_short_files(_job, output_dir):
            job = _job
            for short in (_job.get("result") or {}).get("shorts") or []:
                if (short.get("target_aspect") or "").strip():
                    draft_aspect = short["target_aspect"].strip()
                    break
            break
    if job is None:
        # File is known to exist (isfile check above). Public-looking names
        # carry no existence signal worth protecting, so a restarted GUI can
        # still finalize them with raw effects instead of being stuck at 404.
        # Any name containing a dotfile segment or a drive-relative traversal
        # is treated as private -> same 404 as a missing file.
        suspicious = any(part.startswith(".") for part in safe_rel.split("/"))
        if suspicious:
            return jsonify({"error": "Клип не найден ни в одном задании (job)"}), 404

    aspect_ratio = (data.get("aspect_ratio") or "").strip() \
        or ((job or {}).get("aspect_ratio") or "").strip() \
        or draft_aspect or "9:16"

    # Per-request overrides the browser submitted for this job. finalize runs
    # in a fresh thread, so without this the watermark toggle set on the review
    # panel is lost and the clip re-acquires its effects from the persisted
    # settings file instead of the user's GUI state. A clip with no owning job
    # (e.g. left over from before a restart) falls back to raw overrides +
    # persisted settings.
    p = (job or {}).get("_params") or {}
    overrides = _overrides_from(
        (job or {}).get("mode") or p.get("mode") or "local",
        p.get("api_keys") or {},
        p.get("whisper_device"), p.get("whisper_model"),
        p.get("overlay_position"), p.get("overlay_margin"), p.get("overlay_scale"),
        p.get("use_overlay_opencv"), p.get("overlay_vertical_pos"),
        p.get("overlay_margin_bottom"), p.get("overlay_margin_left"),
        p.get("overlay_enabled"), p.get("overlay_x"), p.get("overlay_y"),
        p.get("music_enabled"), p.get("music_file"), p.get("music_volume"),
        p.get("silence_cut"), p.get("blur_bars"),
        p.get("captions_enabled"), p.get("caption_style"), p.get("face_track"),
        p.get("caption_position"), p.get("caption_margin_v"),
        p.get("title_enabled"), p.get("title_y_from_bottom"),
        p.get("title_font_size"),
        p.get("watermark_enabled"), p.get("watermark_at_sec"),
        p.get("watermark_duration_sec"), p.get("watermark_scale"),
        p.get("watermark_file"),
    )

    # Backup so a mid-crash can't lose the approved-but-not-yet-deleted draft.
    backup = abs_path + ".draft.mp4"
    try:
        shutil.copy2(abs_path, backup)
    except OSError as e:
        return jsonify({"error": f"could not back up draft: {e}"}), 500
    try:
        set_overrides(overrides)
        finalize_clip_local(abs_path, aspect_ratio)
    except Exception as e:  # restore the draft on any effect failure
        try:
            shutil.copy2(backup, abs_path)
        except OSError:
            pass
        return jsonify({"error": f"finalize failed: {e}"}), 500
    finally:
        clear_overrides()

    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    rel = os.path.relpath(os.path.realpath(abs_path), output_dir).replace("\\", "/")
    return jsonify({
        "ok": True,
        "url": f"/output/{rel}",
        "aspect_ratio": aspect_ratio,
        "draft_backup": os.path.basename(backup),
    })


def _warn_unprotected_bind(host):
    """One-time loud warning when binding non-loopback without a GUI token:
    with host=0.0.0.0 anyone on the LAN can drive the pipeline (and upload
    gigabytes of video) unless the API is gated by a token."""
    is_loopback = host in ("127.0.0.1", "localhost", "::1")
    if is_loopback or _gui_token():
        return
    bar = "!" * 70
    print(f"\n{bar}\n"
          "  ВНИМАНИЕ: GUI слушает на всей сети (host={host}) БЕЗ токена.\n"
          "  Любой в локальной сети может вызывать /api/*. Задайте токен:\n"
          "    set GUI_TOKEN=<случайная-строка>   (или в settings.local.json)\n"
          "  или привяжите сервер к localhost:   set GUI_HOST=127.0.0.1\n"
          f"{bar}\n".replace("{host}", host), flush=True)


if __name__ == "__main__":
    # Default to loopback: a GUI that can download/encode arbitrary video must
    # not silently listen on the whole LAN. Set GUI_HOST=0.0.0.0 (ideally with
    # GUI_TOKEN) to expose it to the network on purpose.
    host = (os.getenv("GUI_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("GUI_PORT") or 5000)
    except ValueError:
        port = 5000
    _warn_unprotected_bind(host)
    # debug=False: the Werkzeug debugger allows arbitrary code execution, and
    # this binds to 0.0.0.0. use_reloader would also double-run the job threads.
    app.run(debug=False, host=host, port=port, threaded=True)
