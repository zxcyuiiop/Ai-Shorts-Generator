"""Flask web GUI for AI YouTube Shorts Generator."""
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

# Pipeline progress lines contain non-ASCII characters like →, which crash on
# Windows consoles defaulting to cp1252/cp1251. Same fix as main.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, render_template, request, jsonify, Response, send_from_directory
from werkzeug.utils import secure_filename

from shorts_generator import generate_shorts, settings_store
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
        return
    finished.sort(key=lambda kv: kv[1].get("finished_at") or 0)
    for jid, _ in finished[: len(finished) - MAX_FINISHED_JOBS]:
        jobs.pop(jid, None)
        progress_queues.pop(jid, None)


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
                    silence_cut=None, blur_bars=None):
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
                    silence_cut=None, blur_bars=None):
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
                                      silence_cut, blur_bars))

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

    except Exception as e:
        with jobs_lock:
            job = jobs.get(job_id)
            if job is not None:
                job["finished_at"] = time.time()
                elapsed = job["finished_at"] - job["started_at"]
                job.update(status="error", error=str(e), elapsed=elapsed)
                _prune_finished_jobs()
            else:
                elapsed = 0
        _publish(job_id, {"status": "error", "error": str(e), "elapsed": elapsed})
    finally:
        clear_overrides()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Return saved settings, with secrets masked."""
    return jsonify(settings_store.mask_secrets(settings_store.load()))


@app.route("/api/settings", methods=["POST"])
def post_settings():
    """Persist settings so they survive a restart. Returns the masked result."""
    saved = settings_store.save(request.json or {})
    return jsonify(settings_store.mask_secrets(saved))


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

    file.save(save_path)
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

    file.save(save_path)
    return jsonify({"ok": True, "filename": unique_name, "path": save_path}), 200


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
    num_clips = int(_get("num_clips", 3))
    aspect_ratio = _get("aspect_ratio", "9:16")
    download_format = _get("format", "720")
    language = _get("language") or None
    whisper_device = _get("whisper_device", "auto")
    whisper_model = _get("whisper_model", "base")
    clip_length = _get("clip_length", "any")
    api_keys = data.get("api_keys", {})
    if not isinstance(api_keys, dict):
        api_keys = {}
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

    if not youtube_url:
        return jsonify({"error": "Missing URL or file path"}), 400

    # Local files can only be processed by the local pipeline; API mode cannot
    # reach a path that lives on the user's machine.
    if source_type == "file" and mode != "local":
        return jsonify({"error": "Local files require Local mode"}), 400

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
        **{k: v for k, v in api_keys.items() if v},
    })

    job_queue.put({"job_id": job_id, "url": youtube_url, "params": params})

    return jsonify({"job_id": job_id, "position": _queue_position(jobs[job_id])}), 202


@app.route("/api/jobs")
def list_jobs():
    """Queue overview for the UI: running first, then waiting (in order), then
    finished (newest first)."""
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

        while True:
            try:
                idx = q.get(timeout=15)
            except queue.Empty:
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
        shorts.append({
            "name": os.path.basename(abs_path),
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


@app.route("/api/shorts/save", methods=["POST"])
def save_short():
    """Approve a draft: reframe + effects, then move to output/saved/.

    Drafts are rendered horizontally (16:9, no crop) so nothing is lost before
    review. This reframes the draft to the job's target aspect with face
    tracking (``_reframe_vertical``) into a temp sibling, runs
    ``finalize_clip_local`` (blur bars / overlay / music) on it, moves the
    finished clip to ``output/saved/<same subfolder>/``, and deletes the draft.
    Effects run under the producing job's settings snapshot (``_params``), not
    the current settings file.

    On any failure the draft is left untouched so the user can retry.
    """
    from shorts_generator.config import clear_overrides, set_overrides
    from shorts_generator.local.clipper import _reframe_vertical, finalize_clip_local

    data = request.get_json(silent=True) or request.form.to_dict() or {}
    abs_path, safe_rel = _url_to_output_path((data.get("url") or "").strip())
    if abs_path is None:
        return jsonify({"error": "url must be an /output/... path"}), 400
    if safe_rel == "saved" or safe_rel.startswith("saved/"):
        return jsonify({"error": "clip is already in saved/"}), 400
    if not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404

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
    requested = (data.get("aspect_ratio") or "").strip()
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
    )

    # Work on a temp sibling, never on the draft itself.
    tmp = abs_path + ".tmp_save.mp4"
    draft_backup = abs_path + ".draft.mp4"
    leftover = [abs_path + s for s in _SAVE_LEFTOVER_SUFFIXES]

    set_overrides(overrides)
    try:
        _reframe_vertical(abs_path, tmp, aspect)
        finalize_clip_local(tmp, aspect)
    except Exception as e:
        for path in leftover:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        return jsonify({"error": f"save failed: {e}"}), 500
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
        return jsonify({"error": f"could not create saved dir: {e}"}), 500
    final_path = os.path.join(saved_dir, os.path.basename(abs_path))
    final_part = final_path + ".part"

    try:
        shutil.move(tmp, final_part)
        os.replace(final_part, final_path)
        os.remove(abs_path)
    except OSError as e:
        try:
            if not os.path.isfile(final_path) and not os.path.isfile(abs_path):
                if os.path.isfile(final_part):
                    shutil.move(final_part, abs_path)
                elif os.path.isfile(tmp):
                    shutil.move(tmp, abs_path)
        except OSError:
            pass
        return jsonify({"error": f"could not move into saved/: {e}"}), 500

    for path in leftover + [draft_backup, final_part]:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    rel = os.path.relpath(os.path.realpath(final_path), output_dir).replace("\\", "/")
    return jsonify({"ok": True, "url": f"/output/{rel}", "saved": True,
                    "aspect_ratio": aspect})


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
    return jsonify({"ok": True})


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

    # aspect_ratio: explicit in the request, else the job that produced the clip.
    # We capture that job here and reuse it both for aspect_ratio and for the
    # per-request override snapshot below.
    job = None
    aspect_ratio = (data.get("aspect_ratio") or "").strip()
    if not aspect_ratio:
        with jobs_lock:
            snapshot = [dict(j) for j in jobs.values()]
        output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
        for _job in snapshot:
            if abs_path in _job_short_files(_job, output_dir):
                aspect_ratio = (_job.get("aspect_ratio") or "").strip()
                job = _job
                if aspect_ratio:
                    break
    else:
        # Explicit aspect_ratio still benefits from the job's params when the
        # caller didn't pass one; only scan if we actually need to.
        with jobs_lock:
            snapshot = [dict(j) for j in jobs.values()]
        output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
        for _job in snapshot:
            if abs_path in _job_short_files(_job, output_dir):
                job = _job
                break
    aspect_ratio = aspect_ratio or "9:16"

    # Per-request overrides the browser submitted for this job. finalize runs
    # in a fresh thread, so without this the watermark toggle set on the review
    # panel is lost and the clip re-acquires its effects from the persisted
    # settings file instead of the user's GUI state. If no job is found we
    # fall back to the persisted lowercase settings keys (which the
    # settings-aliases fix makes visible to config.env too).
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


if __name__ == "__main__":
    # debug=False: the Werkzeug debugger allows arbitrary code execution, and
    # this binds to 0.0.0.0. use_reloader would also double-run the job threads.
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
