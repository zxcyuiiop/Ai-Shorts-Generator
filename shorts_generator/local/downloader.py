"""Local YouTube download via yt-dlp.

Returns a local mp4 path so the rest of the local pipeline can read it
directly off disk.
"""
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional

from ..config import LOCAL_OUTPUT_DIR

# Populated after each successful (or cached) download so the pipeline can
# learn the video title / chosen format without changing the return signature.
# _run_local consults it via get_last_download_info().
_LAST_DOWNLOAD_INFO: dict = {}


def _sanitize_folder_name(name: str, max_len: int = 40) -> str:
    """Turn a video title (or file stem) into a safe output subfolder name.

    Keeps only [A-Za-z0-9 _-], replaces spaces with '_', truncates to max_len.
    Returns "video" if nothing usable remains.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", name)
    cleaned = cleaned.replace(" ", "_").strip("_ ")
    cleaned = cleaned[:max_len].rstrip("_")
    return cleaned or "video"


def get_last_download_info() -> dict:
    """Return metadata recorded for the most recent download in this process.

    Keys: ``title`` (str), ``folder`` (sanitized subfolder name), ``path``
    (local file path), ``source`` ('local' / 'cached' / 'download').
    """
    return dict(_LAST_DOWNLOAD_INFO)


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e
    return yt_dlp


def _format_for(fmt: str) -> str:
    """Map our '720' / '1080' shorthand to a yt-dlp format selector.

    H.264 (avc1) is requested first on purpose. YouTube also serves the same
    resolutions as AV1, which older ffmpeg builds cannot decode -- they fail the
    merge with "could not find codec parameters". avc1 is decodable everywhere,
    and OpenCV reads it reliably too. The later fallbacks keep any codec rather
    than failing outright.

    Within a given height, several encodes of the same video are offered at
    different bitrates. We sort candidates by bitrate (``+best`` flips
    yt-dlp's default, favouring higher ``tbr``) so the highest-bitrate
    encode at/below the requested height wins. Audio follows the same idea:
    prefer m4a (needed for a clean mp4 merge) and require abr > 64 kbps.
    """
    try:
        height = int(fmt)
    except ValueError:
        height = 720
    return (
        f"bestvideo[height<={height}][vcodec^=avc1][tbr>0]+bestaudio[ext=m4a][abr>64]/"
        f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a][abr>64]/"
        f"bestvideo[height<={height}][ext=mp4][tbr>0]+bestaudio[ext=m4a][abr>64]/"
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={height}][vcodec^=avc1]/"
        f"best[height<={height}][ext=mp4]/best"
    )


def _ffmpeg_too_old_warning() -> Optional[str]:
    """Return a warning if ffmpeg is missing or predates AV1/modern muxing."""
    import shutil
    import subprocess

    exe = shutil.which("ffmpeg")
    if not exe:
        return (
            "ffmpeg was not found on PATH. yt-dlp needs it to merge video+audio. "
            "Install it from https://www.gyan.dev/ffmpeg/builds/ and reopen your terminal."
        )
    try:
        proc = subprocess.run(
            [exe, "-version"],
            capture_output=True, text=True, timeout=15,
        )
        out = f"{proc.stdout}\n{proc.stderr}"
    except Exception:
        return None

    # "ffmpeg version N-55702-g920046a" or "ffmpeg version 4.4.2-0ubuntu0.22.04.1"
    # "built on Aug 21 2013" or "built with gcc 9.4.0"
    match = re.search(r"built (?:on|with) .+?(\d{4})", out)
    year = int(match.group(1)) if match else None
    if year and year < 2019:
        return (
            f"Your ffmpeg build is from {year} ({exe}) and cannot decode AV1 or VP9 "
            "reliably. Downloads are pinned to H.264 to work around it, but installing "
            "a current build is strongly recommended: https://www.gyan.dev/ffmpeg/builds/"
        )
    return None


def _extract_youtube_video_id(source: str) -> Optional[str]:
    """Best-effort extraction of a YouTube video id from a URL."""
    parsed = urlparse(source)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return video_id or None

    if "youtube.com" in host:
        if parsed.path.startswith("/watch"):
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [""])[0]
            return video_id or None
        match = re.search(r"/(?:shorts|embed|live)/([^/?#&]+)", parsed.path)
        if match:
            return match.group(1)

    return None


def _resolve_local_path(source: str) -> Optional[str]:
    """Return a local filesystem path if the input already points at one."""
    parsed = urlparse(source)
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw_path = f"//{parsed.netloc}{raw_path}"
        candidate = Path(raw_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(f"Local file URL does not exist: {source}")

    if parsed.scheme in ("http", "https"):
        return None

    candidate = Path(source).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())

    if any(sep in source for sep in (os.sep, "/")) or source.startswith("~") or source.startswith("."):
        raise RuntimeError(f"Local file path does not exist: {source}")

    return None


def _existing_download(out_dir: str, video_id: str) -> Optional[str]:
    """Return a cached download path if we already have this YouTube id."""
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = os.path.join(out_dir, f"source_{video_id}{ext}")
        if os.path.exists(candidate):
            return candidate
    return None


def _clear_stale_fragments(out_dir: str, video_id: str) -> None:
    """Delete per-format leftovers from a previous failed merge.

    A failed merge leaves files like source_<id>.f398.mp4 and source_<id>.f140.m4a
    behind. They are never valid inputs on their own -- video-only or audio-only --
    and yt-dlp would otherwise try to resume from them, reproducing the failure with
    the same undecodable codec.
    """
    pattern = re.compile(rf"^source_{re.escape(video_id)}\.f\d+\.")
    for name in os.listdir(out_dir):
        if not pattern.match(name):
            continue
        path = os.path.join(out_dir, name)
        try:
            os.remove(path)
            print(f"[download/local] removed stale fragment: {name}", flush=True)
        except OSError as e:
            print(f"[download/local] could not remove {name}: {e}", flush=True)


def _chosen_format_summary(info: dict) -> str:
    """Build a '1920x1080 tbr=2500 abr=128' summary of what yt-dlp picked.

    After extract_info, ``info`` holds the *merged* result plus the individual
    selected streams in ``info['requested_formats']`` (video + audio). We dig
    the real values out of those instead of guessing from the request, so the
    log reflects what was ACTUALLY chosen. Bits-per-second values are reported
    in kbps, rounded.
    """
    def _kbps(rate):
        try:
            return str(int(round(float(rate))))
        except (TypeError, ValueError):
            return "?"

    vf = None
    af = None
    requested = info.get("requested_formats") or []
    for f in requested:
        if f.get("vcodec") not in (None, "none") and vf is None:
            vf = f
        if f.get("acodec") not in (None, "none") and af is None:
            af = f
    # Single progressive file (no separate streams): info itself has both.
    if vf is None and info.get("vcodec") not in (None, "none"):
        vf = info
    if af is None and info.get("acodec") not in (None, "none"):
        af = info

    w = (vf or {}).get("width") or info.get("width")
    h = (vf or {}).get("height") or info.get("height")
    tbr = _kbps((vf or {}).get("tbr") or info.get("tbr"))
    abr = _kbps((af or {}).get("abr") if af else None)
    res = f"{w}x{h}" if w and h else "?"
    return f"{res} tbr={tbr} abr={abr}"


def _record_download(title: Optional[str], folder_name: str, path: str, source: str) -> None:
    _LAST_DOWNLOAD_INFO.update(
        {"title": title, "folder": folder_name, "path": path, "source": source}
    )


def download_youtube_local(video_url: str, fmt: str = "720", out_dir: Optional[str] = None) -> str:
    """Download a remote URL or return a local file path unchanged."""
    local_path = _resolve_local_path(video_url)
    if local_path:
        print(f"[download/local] using local file: {local_path}", flush=True)
        stem = os.path.splitext(os.path.basename(local_path))[0]
        _record_download(stem, _sanitize_folder_name(stem), local_path, "local")
        return local_path

    yt_dlp = _import_ytdlp()
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    video_id = _extract_youtube_video_id(video_url)
    if video_id:
        cached = _existing_download(out_dir, video_id)
        if cached:
            print(f"[download/local] reusing cached download: {cached}", flush=True)
            stem = os.path.splitext(os.path.basename(cached))[0]
            if stem.startswith("source_"):
                stem = stem[len("source_"):]
            _record_download(None, _sanitize_folder_name(stem or "video"), cached, "cached")
            return cached
        _clear_stale_fragments(out_dir, video_id)

    ffmpeg_warning = _ffmpeg_too_old_warning()
    if ffmpeg_warning:
        print(f"[download/local] WARNING: {ffmpeg_warning}", flush=True)

    print(f"[download/local] {video_url} @ {fmt}p -> {out_dir}/", flush=True)
    ydl_opts = {
        "format": _format_for(fmt),
        # Among equal-selector candidates, prefer the highest total bitrate.
        "format_sort": ["tbr"],
        "outtmpl": os.path.join(out_dir, "source_%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        path = ydl.prepare_filename(info)
        # merge_output_format may rename the extension after merge
        if not os.path.exists(path):
            stem, _ = os.path.splitext(path)
            for ext in (".mp4", ".mkv", ".webm"):
                if os.path.exists(stem + ext):
                    path = stem + ext
                    break

    summary = _chosen_format_summary(info or {})
    print(f"[download/local] format: {summary}", flush=True)

    title = (info or {}).get("title") or "video"
    _record_download(title, _sanitize_folder_name(title), path, "download")
    print(f"[download/local] ready: {path}", flush=True)
    return path
