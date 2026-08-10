"""Local clipping: ffmpeg subclip + OpenCV face-aware vertical crop.

Two stages per highlight:
  1. Cut the source video to [start, end] with ffmpeg (re-encoded, audio kept).
  2. Reframe the cut to the target aspect ratio. For 9:16 we slide a vertical
     window horizontally across the frame to keep faces centred (Haar
     cascade — same approach as the original repo, no external models).
"""
import os
import subprocess
import time
import ctypes
import numpy as np
import shutil
from typing import Dict, List, Optional, Tuple

from ..config import LOCAL_OUTPUT_DIR, env
from .silence import (
    MIN_KEPT_TOTAL_SEC,
    build_keep_segments,
    cut_pauses,
    detect_silences,
    get_duration,
)
from .blurpad import apply_blur_padding, blurpad_enabled_for
from .music import mix_music, music_file_valid, music_settings_from_env
from .captions import burn_captions, captions_enabled, write_caption_ass


def _get_priority_class():
    if os.name != "nt":
        return None
    PROCESS_QUERY_INFORMATION = 0x0400
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, os.getpid())
    if not handle:
        return None
    try:
        lpPriorityClass = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetPriorityClass(handle, ctypes.byref(lpPriorityClass)):
            return lpPriorityClass.value
        return None
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _set_priority_class(priority_class):
    if os.name != "nt":
        return
    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_INFORMATION = 0x0400
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, os.getpid())
    if not handle:
        return
    try:
        ctypes.windll.kernel32.SetPriorityClass(handle, priority_class)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _get_video_encoder() -> str:
    """
    Возвращает имя видео‑энкодера, который следует использовать.
    Приоритет:
      1. Переменная окружения FFMPEG_ENCODER (если установлена) – принудительно задаёт энкодер.
      2. FORCE_CPU_FFMPEG=1 – отключает GPU полностью.
      3. Обнаружение NVIDIA GPU через nvidia-smi + проверка ffmpeg на h264_nvenc/hevc_nvenc.
      4. Иначе fallback на libx264.
    """
    # 1. Direct override via env var
    env_enc = env("FFMPEG_ENCODER")
    if env_enc:
        # Validate it's a known encoder
        if env_enc in ("libx264", "h264_nvenc", "hevc_nvenc"):
            print(f"[clip/local] FFMPEG_ENCODER set to {env_enc} – using it.")
            return env_enc
        else:
            print(f"[clip/local] Warning: FFMPEG_ENCODER={env_enc} not recognised, falling back to auto‑detect.")

    # 2. Force CPU if requested
    if env("FORCE_CPU_FFMPEG") == "1":
        encoder = "libx264"
        print(f"[clip/local] FORCE_CPU_FFMPEG set, using encoder: {encoder}")
        return encoder

    # 3. Detect NVIDIA GPU via nvidia-smi (quick check)
    gpu_available = False
    try:
        proc = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            gpu_available = True
            print("[clip/local] nvidia-smi succeeded – GPU detected.")
        else:
            print("[clip/local] nvidia-smi returned non‑zero exit code.")
    except FileNotFoundError:
        print("[clip/local] nvidia-smi not found – assuming no NVIDIA GPU or driver issue.")
    except Exception as e:
        try:
            print(f"[clip/local] nvidia-smi check failed: {e}")
        except UnicodeEncodeError:
            # Fallback: print with escaped non-ASCII characters
            safe_e = str(e).encode('utf-8', errors='backslashreplace').decode('utf-8')
            print(f"[clip/local] nvidia-smi check failed: {safe_e}")

    # Check ffmpeg for nvenc encoders
    encoder = "libx264"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = proc.stdout
        if proc.returncode == 0:
            if ("h264_nvenc" in out) or ("hevc_nvenc" in out):
                encoder = "h264_nvenc"
                print("[clip/local] ffmpeg reports h264_nvenc/hevc_nvenc support.")
            else:
                print("[clip/local] ffmpeg does NOT list h264_nvenc/hevc_nvenc encoders.")
        else:
            print("[clip/local] ffmpeg -encoders failed.")
    except Exception as e:
        print(f"[clip/local] ffmpeg encoder check failed: {e}")

    # Use GPU only if both GPU present and ffmpeg supports nvenc
    if gpu_available and encoder == "h264_nvenc":
        print(f"[clip/local] NVIDIA GPU detected and ffmpeg supports {encoder} – using hardware encoding.")
    else:
        if not gpu_available:
            print("[clip/local] No NVIDIA GPU found via nvidia-smi, falling back to libx264.")
        else:
            print("[clip/local] FFmpeg does not appear to support h264_nvenc/hevc_nvenc, falling back to libx264.")
        encoder = "libx264"

    print(f"[clip/local] Selected video encoder: {encoder}")
    return encoder


def _video_encoder_args(encoder: str) -> list:
    """
    Возвращает список аргументов ffmpeg для выбранного энкодера.
    Для libx264 используем preset medium и crf=16 для лучшего качества.
    Для h264_nvenc используем preset p2 (более медленный, высокое качество) и cq=16.
    """
    if encoder.startswith("h264_nvenc") or encoder.startswith("hevc_nvenc"):
        # -preset p1‑p7 (p1 — самый быстрый, p7 — 최고 качества). p2 — хороший компромисс качества/скорости.
        # -cq 16‑20 эквивалентен crf 16‑20 для libx264 (меньше — лучше качество).
        args = [
            "-c:v", encoder,
            "-preset", "p2",
            "-cq", "16",          # lowered for better quality
        ]
        print(f"[clip/local] Encoder args for {encoder}: {args}")
        return args
    else:
        # libx264 – medium preset, lower crf for better quality
        args = [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "16",
        ]
        print(f"[clip/local] Encoder args for libx264: {args}")
        return args

def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _run_ffmpeg(cmd: list, what: str) -> None:
    """Run ffmpeg, raising with its actual stderr on failure.

    check=True alone only reports the exit status, which turns a specific,
    fixable complaint ("encoder is experimental", "no such file") into an opaque
    "returned non-zero exit status 1".
    """
    # Log the command (truncated for readability)
    cmd_str = " ".join(cmd)
    if len(cmd_str) > 200:
        cmd_str = cmd_str[:200] + "..."
    print(f"[clip/local] Running ffmpeg ({what}): {cmd_str}")
    start = time.time()
    # Prepare creationflags for high priority on Windows
    creationflags = 0
    if os.name == "nt":
        try:
            # Boost current process priority so child inherits it
            original_priority = _get_priority_class()
            _set_priority_class(0x00000080)  # HIGH_PRIORITY_CLASS
            # Also suppress window creation to avoid stray consoles
            creationflags = subprocess.HIGH_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
            print(f"[clip/local] Setting high priority for ffmpeg process (flags={creationflags}).")
        except Exception as e:
            try:
                print(f"[clip/local] Failed to set high priority flags: {e}")
            except UnicodeEncodeError:
                # Fallback: print with escaped non-ASCII characters
                safe_e = str(e).encode('utf-8', errors='backslashreplace').decode('utf-8')
                print(f"[clip/local] Failed to set high priority flags: {safe_e}")
            creationflags = 0
    try:
        # We don't need stdout; capture stderr to report errors.
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=180,  # 3 minutes should be ample for any short clip
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        # Restore priority before raising
        if os.name == "nt":
            try:
                if 'original_priority' in locals():
                    _set_priority_class(original_priority)
            except Exception:
                pass
        raise RuntimeError(f"ffmpeg ({what}) timed out after 180s")
    elapsed = time.time() - start
    if proc.returncode != 0:
        detail = proc.stderr.strip()
        tail = "\n".join(detail.splitlines()[-6:]) or f"exit status {proc.returncode}"
        raise RuntimeError(f"ffmpeg failed during {what} after {elapsed:.2f}s:\n{tail}")
    else:
        print(f"[clip/local] ffmpeg ({what}) completed in {elapsed:.2f}s")
    # Restore original priority
    if os.name == "nt":
        try:
            if 'original_priority' in locals():
                _set_priority_class(original_priority)
        except Exception:
            pass


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg -ss start -to end → re-encoded mp4 with audio."""
    print(f"[clip/local] _cut_subclip: source={source_path}, start={start:.3f}, end={end:.3f}, output={out_path}")
    encoder = _get_video_encoder()
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", source_path,
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
    ] + _video_encoder_args(encoder) + [
        "-c:a", "aac", "-strict", "-2", "-b:a", "128k",
        out_path,
    ]
    _run_ffmpeg(cmd, "cutting the clip")
    # Optionally log file size
    try:
        size = os.path.getsize(out_path)
        print(f"[clip/local] Cut clip written, size={size} bytes")
    except OSError:
        pass
    return out_path


def _crop_box(src_w: int, src_h: int, aspect_ratio: str) -> Tuple[int, int]:
    """Largest even-sized box with `aspect_ratio` that fits inside src_w x src_h."""
    target = _ratio(aspect_ratio)
    if target < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target)
    # H.264 needs even dimensions.
    crop_w = max(2, crop_w - (crop_w % 2))
    crop_h = max(2, crop_h - (crop_h % 2))
    return crop_w, crop_h


def _load_face_detector():
    """Return a callable(gray_frame) -> list of (x, y, w, h), or None.

    OpenCV 5 dropped CascadeClassifier from the main wheel, so face tracking is
    treated as an optional enhancement rather than a requirement -- without this
    guard the whole reframe step dies and clips come out uncropped.

    Additionally, OpenCV's C++ FileStorage.open() cannot open non-ANSI paths:
    when the repo lives under a Cyrillic directory (e.g. ".../AI-... — копия"),
    cv2.data.haarcascades points inside the venv and the XML "loads" as empty.
    We therefore copy the cascade to the system temp dir (pure ASCII) first and
    load it from there -- that keeps face detection working regardless of where
    the user unpacked the project.
    """
    try:
        import cv2  # type: ignore
    except ImportError:
        print("[clip/local] OpenCV not installed – face detection disabled.")
        return None

    if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
        print("[clip/local] OpenCV lacks CascadeClassifier – face detection disabled.")
        return None

    def _load_from(path):
        cascade = cv2.CascadeClassifier(path)
        return None if cascade.empty() else cascade

    orig = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = _load_from(orig)
    if cascade is None:
        # Cyrillic/Unicode path fallback: copy to an ASCII temp file and retry.
        try:
            import tempfile, shutil
            tmp_dir = tempfile.gettempdir()
            tmp_path = os.path.join(tmp_dir, "ayt_face_cascade.xml")
            if not os.path.isfile(tmp_path):
                shutil.copy2(orig, tmp_path)
            cascade = _load_from(tmp_path)
        except Exception as e:
            print(f"[clip/local] cascade temp-copy fallback failed: {e}")
            cascade = None

    if cascade is None:
        print("[clip/local] Failed to load Haar cascade – face detection disabled.")
        return None

    print("[clip/local] Face detector loaded successfully.")
    return lambda gray: cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
    )


def _probe_dimensions(path: str) -> Tuple[int, int]:
    """Read a video's pixel dimensions, preferring ffprobe, falling back to cv2."""
    print(f"[clip/local] _probe_dimensions: probing {path}")
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            w, h = proc.stdout.strip().split("x")[:2]
            dim = (int(w), int(h))
            print(f"[clip/local] Dimensions via ffprobe: {dim[0]}x{dim[1]}")
            return dim
        else:
            print("[clip/local] ffprobe failed, falling back to OpenCV.")
    except Exception as e:
        try:
            print(f"[clip/local] ffprobe exception: {e}")
        except UnicodeEncodeError:
            # Fallback: print with escaped non-ASCII characters
            safe_e = str(e).encode('utf-8', errors='backslashreplace').decode('utf-8')
            print(f"[clip/local] ffprobe exception: {safe_e}")

    import cv2  # type: ignore

    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            raise RuntimeError(f"could not open {path}")
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        dim = (w, h)
        print(f"[clip/local] Dimensions via OpenCV: {dim[0]}x{dim[1]}")
        return dim
    finally:
        cap.release()


def _reframe_with_ffmpeg(in_path: str, out_path: str, aspect_ratio: str) -> str:
    """Static centre crop to the target ratio, in one ffmpeg pass.

    Used when no face detector is available. Faster than the OpenCV path and it
    keeps the audio stream, so no separate mux is needed.
    """
    print(f"[clip/local] _reframe_with_ffmpeg: in={in_path}, out={out_path}, aspect={aspect_ratio}")
    src_w, src_h = _probe_dimensions(in_path)
    crop_w, crop_h = _crop_box(src_w, src_h, aspect_ratio)
    x = (src_w - crop_w) // 2
    y = (src_h - crop_h) // 2
    print(f"[clip/local] Crop box: {crop_w}x{crop_h} at ({x},{y})")

    encoder = _get_video_encoder()
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", in_path,
        "-vf", f"crop={crop_w}:{crop_h}:{x}:{y}",
    ] + _video_encoder_args(encoder) + [
        "-c:a", "aac", "-strict", "-2", "-b:a", "128k",
        out_path,
    ]
    _run_ffmpeg(cmd, f"cropping to {aspect_ratio}")
    # Log output size
    try:
        size = os.path.getsize(out_path)
        print(f"[clip/local] Cropped clip written, size={size} bytes")
    except OSError:
        pass
    return out_path


def _reframe_vertical(in_path: str, out_path: str, aspect_ratio: str) -> str:
    """Crop the cut clip to the target aspect ratio, tracking faces if possible."""
    print(f"[clip/local] _reframe_vertical: in={in_path}, out={out_path}, aspect={aspect_ratio}")
    # Master switch: FACE_TRACK_ENABLED ('1' default). When off we skip the
    # Haar detector entirely and go straight to the static centre-crop ffmpeg
    # path — the user asked for a fixed, non-roaming frame.
    if str(env("FACE_TRACK_ENABLED", "1") or "").strip().lower() in ("0", "false", "no"):
        print("[clip/local] FACE_TRACK_ENABLED=0 — face tracking off, centre crop", flush=True)
        return _reframe_with_ffmpeg(in_path, out_path, aspect_ratio)
    detect_faces = _load_face_detector()
    if detect_faces is None:
        print("[clip/local] face detector unavailable — using centre crop", flush=True)
        result = _reframe_with_ffmpeg(in_path, out_path, aspect_ratio)
        print(f"[clip/local] _reframe_vertical finished (centre crop) -> {out_path}")
        return result

    import cv2  # type: ignore

    cap = cv2.VideoCapture(in_path)
    writer = None
    silent_path = out_path + ".silent.mp4"
    try:
        if not cap.isOpened():
            raise RuntimeError(f"could not open {in_path}")

        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        crop_w, crop_h = _crop_box(src_w, src_h, aspect_ratio)
        print(f"[clip/local] Face‑tracked crop box: {crop_w}x{crop_h}")

        writer = cv2.VideoWriter(
            silent_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (crop_w, crop_h)
        )
        if not writer.isOpened():
            raise RuntimeError("OpenCV could not open a video writer for the reframed clip")

        last_center: Optional[Tuple[int, int]] = None
        smoothing = 0.15  # how aggressively to chase a new face position
        frame_count = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_count += 1

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detect_faces(gray)
            if len(faces) > 0:
                # Pick the largest face — usually the speaker.
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                cx, cy = x + w // 2, y + h // 2
                if last_center is None:
                    last_center = (cx, cy)
                else:
                    lx, ly = last_center
                    last_center = (
                        int(lx + (cx - lx) * smoothing),
                        int(ly + (cy - ly) * smoothing),
                    )
            if last_center is None:
                last_center = (src_w // 2, src_h // 2)

            cx, cy = last_center
            x0 = max(0, min(src_w - crop_w, cx - crop_w // 2))
            y0 = max(0, min(src_h - crop_h, cy - crop_h // 2))
            writer.write(frame[y0:y0 + crop_h, x0:x0 + crop_w])

        print(f"[clip/local] Processed {frame_count} frames for face tracking.")
    finally:
        # Release before touching the files: Windows keeps them locked while the
        # capture/writer are open, which turns cleanup into WinError 32.
        cap.release()
        if writer is not None:
            writer.release()

    try:
        # Mux audio from the cut clip back onto the silent reframed video.
        print("[clip/local] Muxing audio back onto reframed video.")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", silent_path,
            "-i", in_path,
            "-c:v", "copy",
            "-c:a", "aac", "-strict", "-2", "-b:a", "128k",
            "-map", "0:v:0", "-map", "1:a:0?",
            "-shortest",
            out_path,
        ]
        _run_ffmpeg(cmd, "muxing audio back in")
    finally:
        if os.path.exists(silent_path):
            os.remove(silent_path)

    # Log output size
    try:
        size = os.path.getsize(out_path)
        print(f"[clip/local] Face‑tracked clip written, size={size} bytes")
    except OSError:
        pass
    print(f"[clip/local] _reframe_vertical finished -> {out_path}")
    return out_path


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
    finalize: bool = True,
    transcript: Optional[Dict] = None,
) -> str:
    """Cut + reframe one highlight. With ``finalize=False`` (draft) the heavy
    effects (blur bars / overlay / music) are skipped — a plain reframed clip
    for the user to approve; effects are applied later by ``finalize_clip_local``.
    """
    try:
        print(f"[clip/local] crop_clip_local start: source={source_path}, [{start_time:.3f}-{end_time:.3f}s], aspect={aspect_ratio}, output={out_path}")
    except UnicodeEncodeError:
        # Fallback: print with escaped non-ASCII characters
        safe_source = source_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        safe_out_path = out_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(f"[clip/local] crop_clip_local start: source={safe_source}, [{start_time:.3f}-{end_time:.3f}s], aspect={aspect_ratio}, output={safe_out_path}")
    cut_path = out_path + ".cut.mp4"
    try:
        t0 = time.time()
        _cut_subclip(source_path, start_time, end_time, cut_path)
        t1 = time.time()
        print(f"[clip/local] Cutting took {t1 - t0:.2f}s")

        # T8: jump-cut silent pauses out of the cut clip before reframing.
        # Env: SILENCE_CUT ('1') master switch; SILENCE_NOISE_DB ('-35'),
        # SILENCE_MIN_DUR ('0.45'), SILENCE_KEEP_EXTRA ('0.15').
        caption_segs = None  # kept segments, once known — captions.py consumes them
        if str(env("SILENCE_CUT", "1") or "").strip().lower() not in ("0", "false", "no"):
            try:
                noise_db = float(env("SILENCE_NOISE_DB", "-35") or "-35")
                min_dur = float(env("SILENCE_MIN_DUR", "0.45") or "0.45")
                keep_extra = float(env("SILENCE_KEEP_EXTRA", "0.15") or "0.15")
                silences = detect_silences(cut_path, noise_db, min_dur)
                segs = build_keep_segments(get_duration(cut_path), silences, keep_extra)
                kept = sum(e - s for s, e in segs) if segs is not None else 0.0
                if segs is None:
                    print("[clip/local] silence-cut skipped: nothing worth cutting")
                elif kept < MIN_KEPT_TOTAL_SEC:
                    print(f"[clip/local] silence-cut skipped: only {kept:.1f}s kept (< {MIN_KEPT_TOTAL_SEC:.1f}s)")
                else:
                    tight_path = cut_path + ".tight.mp4"
                    cut_pauses(cut_path, tight_path, segs)
                    os.replace(tight_path, cut_path)
                    caption_segs = segs
            except Exception as e:
                print(f"[clip/local] silence-cut skipped: {e}")

        # T-cap: with subtitles enabled, write the .ass sidecar here — the only
        # moment the exact silence-cut segments are known, which the word
        # timings must be remapped past. The sidecar sits next to the draft;
        # the draft itself stays uncaptioned (burn happens at finalize).
        if captions_enabled() and transcript:
            try:
                write_caption_ass(transcript, start_time, end_time,
                                  out_path + ".ass",
                                  kept_segments=caption_segs)
            except Exception as e:
                print(f"[clip/local] caption sidecar skipped: {e}")
        elif captions_enabled():
            print("[clip/local] captions enabled but no transcript — sidecar skipped",
                  flush=True)

        _reframe_vertical(cut_path, out_path, aspect_ratio)
        t2 = time.time()
        print(f"[clip/local] Reframing took {t2 - t1:.2f}s")

        if finalize:
            finalize_clip_local(out_path, aspect_ratio)
        else:
            print("[clip/local] finalize deferred (draft) — effects skipped until approved")
    finally:
        # Best-effort cleanup: a failure to delete the scratch file must not
        # replace the real error with a confusing WinError 32.
        try:
            if os.path.exists(cut_path):
                os.remove(cut_path)
                print("[clip/local] Removed temporary cut file.")
        except OSError as e:
            try:
                print(f"[clip/local] could not remove {cut_path}: {e}", flush=True)
            except UnicodeEncodeError:
                # Fallback: print with escaped non-ASCII characters
                safe_cut_path = cut_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
                safe_e = str(e).encode('utf-8', errors='backslashreplace').decode('utf-8')
                print(f"[clip/local] could not remove {safe_cut_path}: {safe_e}", flush=True)

    # Final size log
    try:
        size = os.path.getsize(out_path)
        print(f"[clip/local] Final clip written, size={size} bytes, total time={time.time() - t0:.2f}s")
    except OSError:
        pass
    print(f"[clip/local] crop_clip_local finished -> {out_path}")
    return out_path


def finalize_clip_local(out_path: str, aspect_ratio: str,
                        captions_ass: Optional[str] = None,
                        blur_source: Optional[str] = None) -> str:
    """Apply the visual/audio effects to a reframed clip, in place.

    Blur bars (9:16), TikTok-стайл оверлей и фоновая музыка — всё то, что
    раньше жёг комп на каждом черновике. Теперь вызывается только после того,
    как пользователь одобрил черновик в ревью‑панели. Безопасно падать частично:
    каждая стадия в своём try/except, клип никогда не теряется.

    ``blur_source``: optional original landscape clip the draft was cut from;
    forwarded to blurpad so the blurred backdrop comes from the FULL frame,
    not the 9:16 crop (a 9:16 crop re-scaled onto the canvas leaves no room
    for bars — that was the always-bright-blur bug). When omitted, blurpad
    tries to rediscover the source next to the draft.

    Караоке‑субтитры: когда включены (`CAPTIONS_ENABLED`), берём sidecar
    ``out_path+'.ass'`` (либо явный ``captions_ass``) и вжигаем ПОСЛЕ blurpad
    (чтобы попасть на готовый холст 1080×1920), но ДО оверлея/музыки.
    """
    # T10: blurred bars to 1080x1920 (9:16 only, env BLUR_BARS default '1').
    if blurpad_enabled_for(aspect_ratio):
        swap_path = out_path + ".prerender.mp4"
        try:
            os.replace(out_path, swap_path)  # reframe output becomes blurpad input
            try:
                apply_blur_padding(swap_path, out_path, source_path=blur_source)
            finally:
                if os.path.exists(swap_path):
                    try:
                        os.remove(swap_path)
                    except OSError:
                        pass
        except Exception as e:
            print(f"[clip/local] blurpad skipped: {e}")
            # Restore the reframed clip so the pipeline never loses it.
            if not os.path.exists(out_path) and os.path.exists(swap_path):
                try:
                    os.replace(swap_path, out_path)
                except OSError:
                    pass

    # Караоке-субтитры: вжигать после blurpad (см. docstring), до оверлея/музыки.
    if captions_enabled():
        ass = captions_ass or out_path + ".ass"
        if os.path.isfile(ass):
            try:
                burn_captions(out_path, ass)
            except Exception as e:
                print(f"[clip/local] caption burn skipped: {e}", flush=True)
        else:
            print(f"[clip/local] captions enabled but sidecar missing ({ass}) — "
                  "skipping burn", flush=True)

    # Overlay looping TikTok animation at the bottom if file exists
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    overlay_path = os.path.join(base_dir, 'TIKTOK1.mov')
    if os.path.isfile(overlay_path):
        try:
            t3 = time.time()
            _overlay_tiktok(out_path, overlay_path)
            t4 = time.time()
            print(f"[clip/local] TikTok overlay took {t4 - t3:.2f}s")
        except RuntimeError as e:
            if "Unrecognized option 'stream_loop'" in str(e):
                # Fallback for older ffmpeg: create a looped overlay via concat demuxer
                print("[clip/local] stream_loop not supported – using fallback method.")
                t3 = time.time()
                _overlay_tiktok_fallback(out_path, overlay_path)
                t4 = time.time()
                print(f"[clip/local] TikTok overlay fallback took {t4 - t3:.2f}s")
            else:
                raise
    else:
        print("[clip/local] TikTok overlay file not found:", overlay_path, flush=True)

    # T9: mix a looping background music bed into the finished clip (in place).
    # Env: MUSIC_ENABLED ('0'), MUSIC_FILE, MUSIC_VOLUME percent (default 40).
    try:
        m = music_settings_from_env()
        if m["enabled"] and m["file"] and music_file_valid(m["file"]):
            mix_music(out_path, m["file"], m["volume"])
        elif m["enabled"]:
            print(f"[clip/local] music skipped: MUSIC_FILE missing or invalid (MUSIC_FILE={env('MUSIC_FILE', '')!r})")
    except Exception as e:
        print(f"[clip/local] music mix failed: {e}")
    return out_path


def _overlay_tiktok(base_video_path: str, overlay_path: str) -> None:
    """Overlay TikTok animation using OpenCV for video, ffmpeg only for audio."""
    # Master switch: OVERLAY_ENABLED (default "1"). Set to 0/false/no to skip
    # the watermark entirely — both overlay paths return before touching the file.
    if str(env("OVERLAY_ENABLED", "1") or "").strip().lower() in ("0", "false", "no"):
        print("[clip/local] overlay disabled")
        return
    # Use OpenCV route unless disabled via env var
    if env("USE_OVERLAY_OPENCV", "1") == "0":
        return _overlay_tiktok_ffmpeg(base_video_path, overlay_path)
    else:
        return _overlay_tiktok_opencv(base_video_path, overlay_path)


def _overlay_geometry(frame_w: int, frame_h: int,
                      overlay_w: int, overlay_h: int) -> Tuple[int, int, int, int]:
    """Resolve GUI overlay settings to (x, y, w, h) for a given frame size.

    The GUI uses a 9-position grid (``OVERLAY_POSITION`` = tl/tc/tr/ml/mc/mr/bl/bc/br)
    plus a uniform ``OVERLAY_MARGIN``. Anything >= 10 in ``OVERLAY_SCALE`` is treated
    as a GUI percent. The old ``OVERLAY_VERTICAL_POS`` / per-side margin env vars are
    still honored only when no position grid was supplied.

    New keys:
      ``OVERLAY_X``, ``OVERLAY_Y`` — free-float position of the overlay CENTER as a
      0..1 fraction of the frame. When BOTH are set and parse to floats inside
      [0, 1] they replace the 9-position grid entirely
      (x = OVERLAY_X*frame_w - scaled_w/2, y = OVERLAY_Y*frame_h - scaled_h/2,
      then clamped fully inside the frame). If only one is set, or a value is
      unparseable/out of range, both are ignored and the grid/legacy path below
      is used.
    """
    # --- free-float center position (optional) --------------------------------
    # OVERLAY_X / OVERLAY_Y = position of the overlay CENTER as a 0..1 fraction
    # of the frame. Only honored when BOTH are set, parse as floats, and both
    # land inside [0, 1]; otherwise ignored entirely (grid/legacy path kept).
    def _to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    free_x = _to_float(env("OVERLAY_X", ""))
    free_y = _to_float(env("OVERLAY_Y", ""))
    if not (free_x is not None and 0.0 <= free_x <= 1.0):
        free_x = None
    if not (free_y is not None and 0.0 <= free_y <= 1.0):
        free_y = None
    free_pos = free_x is not None and free_y is not None

    # --- scale ---------------------------------------------------------------
    scale_raw = env("OVERLAY_SCALE", "1.0")
    try:
        scale = float(scale_raw)
    except (TypeError, ValueError):
        scale = 1.0
    if scale >= 10.0:
        scale = scale / 100.0
    # sane bounds so the box never vanishes or overflows silly values
    scale = max(0.01, min(scale, 3.0))

    w = max(2, int(round(overlay_w * scale)))
    h = max(2, int(round(overlay_h * scale)))
    # clamp to frame
    if w > frame_w:
        w = frame_w
    if h > frame_h:
        h = frame_h

    # --- margin ---------------------------------------------------------------
    def _to_int(v, default=20):
        try:
            return max(0, int(float(v)))
        except (TypeError, ValueError):
            return default

    margin = _to_int(env("OVERLAY_MARGIN", ""), default=None)
    margin_bottom = _to_int(env("OVERLAY_MARGIN_BOTTOM", ""), default=None)
    margin_left = _to_int(env("OVERLAY_MARGIN_LEFT", ""), default=None)
    if margin is None:
        # old setup: per-side margins, keep behavior
        margin_bottom = margin_bottom if margin_bottom is not None else 20
        margin_left = margin_left if margin_left is not None else 20
    else:
        margin_bottom = margin_left = margin

    # center margins for centered positions come from the uniform margin math below
    mx = margin_left
    my = margin_bottom

    # --- position (x, y of the TOP-LEFT corner of the overlay) ----------------
    pos = str(env("OVERLAY_POSITION", "") or "").strip().lower()
    x = y = None
    if len(pos) == 2 and pos[0] in "tbm" and pos[1] in "lcr":
        row, col = pos[0], pos[1]
        if col == "l":
            x = mx
        elif col == "r":
            x = frame_w - w - mx
        else:  # center
            x = (frame_w - w) // 2

        if row == "t":
            y = my
        elif row == "b":
            y = frame_h - h - my
        else:  # middle
            y = (frame_h - h) // 2
    else:
        # legacy path: OVERLAY_VERTICAL_POS as fraction from top for the TOP edge
        try:
            vertical_pos = float(env("OVERLAY_VERTICAL_POS", "0.8"))
        except (TypeError, ValueError):
            vertical_pos = 0.8
        if vertical_pos > 1.0:
            vertical_pos = vertical_pos / 100.0
        max_x = max(0, frame_w - w - mx)
        max_y = max(0, frame_h - h - my)
        x = max(0, min(mx, max_x))
        y = max(0, min(int(frame_h * vertical_pos), max_y))

    # Free-float override: only when BOTH OVERLAY_X and OVERLAY_Y were set and
    # valid (both in [0, 1]). The overlay center lands at
    # (OVERLAY_X*frame_w, OVERLAY_Y*frame_h); the rectangle is then clamped so
    # it stays fully inside the frame. Otherwise the grid/legacy result stands.
    if free_pos:
        x = int(free_x * frame_w - w / 2)
        y = int(free_y * frame_h - h / 2)
        x = max(0, min(x, frame_w - w))
        y = max(0, min(y, frame_h - h))
        print(f"[clip/local] overlay free-position x={x} y={y}")
    return x, y, w, h


def _overlay_tiktok_ffmpeg(base_video_path: str, overlay_path: str) -> None:
    """Original FFmpeg-based overlay (with -shortest).

    Placement honors the same GUI settings as the OpenCV path via
    ``_overlay_geometry``: OVERLAY_POSITION grid + OVERLAY_MARGIN +
    OVERLAY_SCALE, with OVERLAY_VERTICAL_POS as legacy fallback.
    """
    try:
        print(f"[clip/local] _overlay_tiktok_ffmpeg: base={base_video_path}, overlay={overlay_path}")
    except UnicodeEncodeError:
        # Fallback: print with escaped non-ASCII characters
        safe_base = base_video_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        safe_overlay = overlay_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(f"[clip/local] _overlay_tiktok_ffmpeg: base={safe_base}, overlay={safe_overlay}")

    base_w, base_h = _probe_dimensions(base_video_path)
    over_w, over_h = _probe_dimensions(overlay_path)

    x, y, scaled_w, scaled_h = _overlay_geometry(base_w, base_h, over_w, over_h)
    print(f"[clip/local] Overlay placement (ffmpeg): x={x}, y={y}, size={scaled_w}x{scaled_h}")

    encoder = _get_video_encoder()
    overlay_out = base_video_path + ".tiktok.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", base_video_path,
        "-stream_loop", "-1", "-i", overlay_path,
        "-filter_complex", f"[1:v]scale={scaled_w}:{scaled_h}[ov];[0:v][ov]overlay={x}:{y}:shortest=1",
    ] + _video_encoder_args(encoder) + [
        "-c:a", "copy",
        "-shortest",
        overlay_out,
    ]
    _run_ffmpeg(cmd, "overlaying TikTok (FFmpeg)")
    try:
        size = os.path.getsize(overlay_out)
        print(f"[clip/local] TikTok overlay output size={size} bytes")
    except OSError:
        pass
    except UnicodeEncodeError:
        print(f"[clip/local] TikTok overlay output size={size} bytes")
    os.replace(overlay_out, base_video_path)
    try:
        print(f"[clip/local] Overlay applied, replaced {base_video_path}")
    except UnicodeEncodeError:
        # Fallback: print with escaped non-ASCII characters
        safe_base = base_video_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(f"[clip/local] Overlay applied, replaced {safe_base}")


def _overlay_tiktok_opencv(base_video_path: str, overlay_path: str) -> None:
    """Overlay using OpenCV for video processing; audio handled via FFmpeg copy."""
    try:
        print(f"[clip/local] _overlay_tiktok_opencv: base={base_video_path}, overlay={overlay_path}")
    except UnicodeEncodeError:
        # Fallback: print with escaped non-ASCII characters
        safe_base = base_video_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        safe_overlay = overlay_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(f"[clip/local] _overlay_tiktok_opencv: base={safe_base}, overlay={safe_overlay}")
    try:
        import cv2  # type: ignore
    except ImportError:
        print("[clip/local] OpenCV not available – falling back to FFmpeg overlay.")
        return _overlay_tiktok_ffmpeg(base_video_path, overlay_path)

    # Open base video
    cap_base = cv2.VideoCapture(base_video_path)
    if not cap_base.isOpened():
        raise RuntimeError(f"Could not open base video: {base_video_path}")
    fps = cap_base.get(cv2.CAP_PROP_FPS)
    if fps == 0:
        fps = 30.0
    width = int(cap_base.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap_base.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(cap_base.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[clip/local] Base video: {width}x{height} @ {fps}fps, {frame_count} frames")

    # Overlay settings (position grid / margins / scale) are resolved by
    # _overlay_geometry once the overlay dimensions are known.

    # Open overlay video
    cap_over = cv2.VideoCapture(overlay_path)
    if not cap_over.isOpened():
        cap_base.release()
        raise RuntimeError(f"Could not open overlay video: {overlay_path}")
    overlay_fps = cap_over.get(cv2.CAP_PROP_FPS)
    overlay_width = int(cap_over.get(cv2.CAP_PROP_FRAME_WIDTH))
    overlay_height = int(cap_over.get(cv2.CAP_PROP_FRAME_HEIGHT))
    overlay_frame_count = int(cap_over.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[clip/local] Overlay video: {overlay_width}x{overlay_height} @ {overlay_fps}fps, {overlay_frame_count} frames")

    # Resolve GUI settings (9-position grid + uniform margin + scale percent,
    # or the legacy OVERLAY_VERTICAL_POS fallback) to pixel placement.
    x_offset, y_offset, overlay_width_scaled, overlay_height_scaled = _overlay_geometry(
        width, height, overlay_width, overlay_height
    )
    print(f"[clip/local] Overlay size: {overlay_width_scaled}x{overlay_height_scaled} (original {overlay_width}x{overlay_height})")
    print(f"[clip/local] Overlay placement: left={x_offset}, top={y_offset}")

    # Prepare temporary silent video (we'll encode with a lossless codec for speed, then later maybe re-encode?)
    # Use mp4v (widely available) – later we can re-encode with NVENC if needed, but for now keep as is.
    temp_video = base_video_path + ".opencv_overlay.mp4"
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(temp_video, fourcc, fps, (width, height))
    if not out.isOpened():
        cap_base.release()
        cap_over.release()
        raise RuntimeError(f"Could not open VideoWriter for {temp_video}")

    # Determine placement (already resolved by _overlay_geometry above)
    print(f"[clip/local] Overlay placement (final): top={y_offset}, left={x_offset}")

    frame_idx = 0
    while True:
        ret_base, frame_base = cap_base.read()
        if not ret_base:
            break
        # Get overlay frame, looping if needed
        ret_over, frame_over = cap_over.read()
        if not ret_over:
            # Reset to start of overlay
            cap_over.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret_over, frame_over = cap_over.read()
            if not ret_over:
                # If overlay empty, just write base
                out.write(frame_base)
                frame_idx += 1
                continue

        # Resize overlay frame to scaled dimensions
        frame_over = cv2.resize(frame_over, (overlay_width_scaled, overlay_height_scaled))

        # Ensure overlay frame same channels as base
        if frame_over.shape[2] == 4 and frame_base.shape[2] == 3:
            # overlay has alpha, blend
            alpha = frame_over[:, :, 3] / 255.0
            for c in range(3):
                frame_base[y_offset:y_offset+overlay_height_scaled, x_offset:x_offset+overlay_width_scaled, c] = (
                    alpha * frame_over[:, :, c] +
                    (1 - alpha) * frame_base[y_offset:y_offset+overlay_height_scaled, x_offset:x_offset+overlay_width_scaled, c]
                )
        else:
            # If overlay has no alpha, copy non-zero pixels using numpy.where to avoid shape mismatch
            gray_over = cv2.cvtColor(frame_over, cv2.COLOR_BGR2GRAY)
            mask = gray_over > 0
            if mask.any():
                roi = frame_base[y_offset:y_offset+overlay_height_scaled, x_offset:x_offset+overlay_width_scaled]
                # Check if shapes match for safe broadcasting
                if roi.shape[:2] == frame_over.shape[:2]:
                    for c in range(3):
                        roi[..., c] = np.where(mask, frame_over[:, :, c], roi[..., c])
                    frame_base[y_offset:y_offset+overlay_height_scaled, x_offset:x_offset+overlay_width_scaled] = roi
                else:
                    # Shape mismatch - fall back to simple copy where mask is True
                    # This ensures we don't crash but may not be perfect
                    frame_base[y_offset:y_offset+overlay_height_scaled, x_offset:x_offset+overlay_width_scaled][mask] = frame_over[mask]

        out.write(frame_base)
        frame_idx += 1
        if frame_idx % 50 == 0:
            try:
                print(f"[clip/local] Processed {frame_idx}/{frame_count} frames")
            except UnicodeEncodeError:
                # Fallback: print with escaped non-ASCII characters
                print(f"[clip/local] Processed {frame_idx}/{frame_count} frames")

    # Release captures
    cap_base.release()
    cap_over.release()
    out.release()
    try:
        print(f"[clip/local] Finished writing silent overlay video to {temp_video}")
    except UnicodeEncodeError:
        # Fallback: print with escaped non-ASCII characters
        safe_temp = temp_video.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(f"[clip/local] Finished writing silent overlay video to {safe_temp}")

    # Check if base video has an audio stream
    def _has_audio_stream(path: str) -> bool:
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=10,
            )
            return proc.returncode == 0 and proc.stdout.strip() != ""
        except Exception:
            return False

    has_audio = _has_audio_stream(base_video_path)
    if has_audio:
        # Extract audio from base using ffmpeg (fast), then mux.
        audio_path = base_video_path + ".aac"
        try:
            # Extract audio (copy)
            cmd_extract = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", base_video_path,
                "-vn", "-ac", "2", "-ar", "48000", "-b:a", "128k",
                audio_path,
            ]
            _run_ffmpeg(cmd_extract, "extracting audio")
            # Mux audio with silent video
            cmd_mux = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", temp_video,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                base_video_path + ".tiktok.mp4",
            ]
            _run_ffmpeg(cmd_mux, "muxing audio with overlay video")
            # Replace base
            os.replace(base_video_path + ".tiktok.mp4", base_video_path)
            try:
                print(f"[clip/local] Overlay applied via OpenCV+FFmpeg audio, replaced {base_video_path}")
            except UnicodeEncodeError:
                # Fallback: print with escaped non-ASCII characters
                safe_base = base_video_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
                print(f"[clip/local] Overlay applied via OpenCV+FFmpeg audio, replaced {safe_base}")
        finally:
            # Cleanup temp files
            try:
                os.remove(temp_video)
            except OSError:
                pass
            try:
                os.remove(audio_path)
            except OSError:
                pass
    else:
        # No audio stream; just use the silent video as final.
        try:
            os.replace(temp_video, base_video_path)
        except OSError as e:
            try:
                print(f"[clip/local] Failed to replace video: {e}")
            except UnicodeEncodeError:
                # Fallback: print with escaped non-ASCII characters
                safe_e = str(e).encode('utf-8', errors='backslashreplace').decode('utf-8')
                print(f"[clip/local] Failed to replace video: {safe_e}")
        try:
            print(f"[clip/local] Overlay applied (no audio) via OpenCV, replaced {base_video_path}")
        except UnicodeEncodeError:
            # Fallback: print with escaped non-ASCII characters
            safe_base = base_video_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
            print(f"[clip/local] Overlay applied (no audio) via OpenCV, replaced {safe_base}")


def _overlay_tiktok_fallback(base_video_path: str, overlay_path: str) -> None:
    """Overlay TikTok animation for ffmpeg versions lacking -stream_loop.
    Creates a temporary concatenated overlay file long enough to cover the base video.
    """
    try:
        print(f"[clip/local] _overlay_tiktok_fallback: base={base_video_path}, overlay={overlay_path}")
    except UnicodeEncodeError:
        # Fallback: print with escaped non-ASCII characters
        safe_base = base_video_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        safe_overlay = overlay_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(f"[clip/local] _overlay_tiktok_fallback: base={safe_base}, overlay={safe_overlay}")
    import tempfile
    # Get duration of base video
    def _probe_duration(path: str) -> float:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            try:
                return float(proc.stdout.strip())
            except ValueError:
                pass
        return 0.0
    base_dur = _probe_duration(base_video_path)
    try:
        print(f"[clip/local] Base video duration: {base_dur:.2f}s")
    except UnicodeEncodeError:
        print(f"[clip/local] Base video duration: {base_dur:.2f}s")
    if base_dur <= 0:
        # If we can't get duration, just attempt a reasonable number of loops
        loops = 100
        try:
            print("[clip/local] Could not determine base duration, using 100 loops.")
        except UnicodeEncodeError:
            print("[clip/local] Could not determine base duration, using 100 loops.")
    else:
        overlay_dur = _probe_duration(overlay_path)
        try:
            print(f"[clip/local] Overlay duration: {overlay_dur:.2f}s")
        except UnicodeEncodeError:
            print(f"[clip/local] Overlay duration: {overlay_dur:.2f}s")
        if overlay_dur <= 0:
            loops = 100
            try:
                print("[clip/local] Could not determine overlay duration, using 100 loops.")
            except UnicodeEncodeError:
                print("[clip/local] Could not determine overlay duration, using 100 loops.")
        else:
            loops = int(base_dur // overlay_dur) + 2  # extra to be safe
            try:
                print(f"[clip/local] Will loop overlay {loops} times to cover base video.")
            except UnicodeEncodeError:
                print(f"[clip/local] Will loop overlay {loops} times to cover base video.")

    # Create temporary list file for concat demuxer
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        for _ in range(loops):
            f.write(f"file '{os.path.abspath(overlay_path)}'\n")
        list_path = f.name
    try:
        print(f"[clip/local] Created concat list with {loops} entries at {list_path}")
    except UnicodeEncodeError:
        # Fallback: print with escaped non-ASCII characters
        safe_list_path = list_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(f"[clip/local] Created concat list with {loops} entries at {safe_list_path}")
    try:
        looped_overlay = base_video_path + ".looped_overlay.mov"
        # Concatenate overlay copies
        cmd_concat = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy",
            looped_overlay,
        ]
        _run_ffmpeg(cmd_concat, "creating looped overlay")
        # Now overlay the looped overlay onto base video (should be at least as long)
        encoder = _get_video_encoder()
        overlay_out = base_video_path + ".tiktok.mp4"
        cmd_overlay = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", base_video_path,
            "-i", looped_overlay,
            "-filter_complex", "[0:v][1:v]overlay=0:main_h-overlay_h-10",
        ] + _video_encoder_args(encoder) + [
            "-c:a", "copy",
            "-shortest",
            overlay_out,
        ]
        _run_ffmpeg(cmd_overlay, "overlaying TikTok (fallback)")
        try:
            size = os.path.getsize(overlay_out)
            print(f"[clip/local] Fallback overlay output size={size} bytes")
        except OSError:
            pass
        except UnicodeEncodeError:
            print(f"[clip/local] Fallback overlay output size={size} bytes")
        os.replace(overlay_out, base_video_path)
        try:
            print(f"[clip/local] Fallback overlay applied, replaced {base_video_path}")
        except UnicodeEncodeError:
            # Fallback: print with escaped non-ASCII characters
            safe_base = base_video_path.encode('utf-8', errors='backslashreplace').decode('utf-8')
            print(f"[clip/local] Fallback overlay applied, replaced {safe_base}")
    finally:
        try:
            os.remove(list_path)
            try:
                print("[clip/local] Removed temporary concat list.")
            except UnicodeEncodeError:
                print("[clip/local] Removed temporary concat list.")
        except OSError:
            pass
        try:
            if os.path.exists(looped_overlay):
                os.remove(looped_overlay)
                try:
                    print("[clip/local] Removed temporary looped overlay.")
                except UnicodeEncodeError:
                    print("[clip/local] Removed temporary looped overlay.")
        except OSError:
            pass


def _video_basename(source_path: str) -> str:
    """Extract a safe base name from the source video file path."""
    name = os.path.basename(source_path)
    # Remove common prefix like 'source_' if present
    if name.startswith("source_"):
        name = name[len("source_"):]
    # Strip extension
    name = os.path.splitext(name)[0]
    # Replace any problematic characters for filenames
    # Keep alphanumeric, underscore, hyphen
    import re
    name = re.sub(r'[^A-Za-z0-9_-]', '_', name)
    return name or "video"

def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    finalize: bool = True,
    transcript: Optional[Dict] = None,
) -> List[Dict]:
    # output_dir is the GUI-facing spelling; when given it wins and its
    # directory is created before any clip is written. Legacy out_dir keeps
    # working unchanged when output_dir is None.
    if output_dir is not None:
        os.makedirs(output_dir, exist_ok=True)
        out_dir = output_dir
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    video_base = _video_basename(source_path)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{video_base}_{i:02d}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
                finalize=finalize,
                transcript=transcript,
            )
            results.append({**h, "clip_url": out_path})
        except Exception as e:
            try:
                print(f"[clip/local] {i} failed: {e}", flush=True)
            except UnicodeEncodeError:
                # Fallback: print with escaped non-ASCII characters
                safe_e = str(e).encode('utf-8', errors='backslashreplace').decode('utf-8')
                print(f"[clip/local] {i} failed: {safe_e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results