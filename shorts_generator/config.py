import os
import threading

from dotenv import load_dotenv
from .settings_store import load as load_settings

load_dotenv()

# Per-thread setting overrides. The web GUI lets a user paste keys/models into
# the browser instead of editing .env; those apply to that one request only.
# Thread-local (not os.environ) so two jobs running at once can use different
# providers without clobbering each other.
_overrides = threading.local()


def set_overrides(mapping: dict) -> None:
    """Bind settings for the current thread only. Falsy values are ignored."""
    _overrides.values = {k: v for k, v in (mapping or {}).items() if v}


def clear_overrides() -> None:
    _overrides.values = {}


def env(name: str, default: str = "") -> str:
    """Read a setting: thread-local override first, then settings from
    settings.local.json, then the process env.

    Always call this at use time rather than caching the result at import time —
    otherwise per-request overrides never take effect.

    Present-but-falsy values ("0", False, 0) still count as set: OVERLAY_ENABLED=0
    must beat the clipper's default of "1", not fall through to it.
    """
    values = getattr(_overrides, "values", None)
    if values and values.get(name):
        return values[name]
    if values and name in values:
        return values[name]  # explicit falsy override (e.g. OVERLAY_ENABLED "0")
    # Check settings persisted by the GUI
    try:
        settings = load_settings()
        if settings and name in settings:
            return settings[name]
    except Exception:
        pass
    return os.getenv(name, default)


MUAPI_API_KEY = os.getenv("MUAPI_API_KEY", "").strip()
MUAPI_BASE_URL = os.getenv("MUAPI_BASE_URL", "https://api.muapi.ai/api/v1").rstrip("/")

POLL_INTERVAL_SECONDS = float(os.getenv("MUAPI_POLL_INTERVAL", "5"))
POLL_TIMEOUT_SECONDS = float(os.getenv("MUAPI_POLL_TIMEOUT", "600"))

# Local-mode (--mode local) settings — only consulted when running offline.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()

# Ollama (LLM_PROVIDER=ollama) — fully local, no API key needed.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
# Ollama defaults to a 4096-token context, which silently truncates long
# transcripts. Highlight prompts run ~5-6k tokens for a 20-min chunk, so we
# raise it. Lower this if the model does not fit in VRAM at this size.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))

# NVIDIA NIM (LLM_PROVIDER=nim) — hosted catalog or a self-hosted container.
# Self-hosted NIM needs no key; point NIM_BASE_URL at http://localhost:8000/v1.
NIM_API_KEY = os.getenv("NIM_API_KEY", "").strip()
NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
NIM_MODEL = os.getenv("NIM_MODEL", "meta/llama-3.1-8b-instruct")

# Local LLM servers are slow on long prompts — give them room before giving up.
LOCAL_LLM_TIMEOUT = float(os.getenv("LOCAL_LLM_TIMEOUT", "30"))
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "base")
LOCAL_WHISPER_DEVICE = os.getenv("LOCAL_WHISPER_DEVICE", "auto")  # auto / cpu / cuda
LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR", "output")

# VAD (Voice Activity Detection) settings for faster-whisper
# Default threshold is 0.5; lower = more sensitive, higher = less sensitive
# Default min_speech_duration_ms is 250ms; increase to avoid tiny false positives
# Default min_silence_duration_ms is 2000ms; increase to avoid splitting mid-sentence
# DISABLED by default because VAD is too aggressive on mixed speech/music content
LOCAL_WHISPER_VAD_FILTER = os.getenv("LOCAL_WHISPER_VAD_FILTER", "false").strip().lower() == "true"
_vad_params_env = os.getenv("LOCAL_WHISPER_VAD_PARAMETERS", "")
if _vad_params_env:
    import json
    LOCAL_WHISPER_VAD_PARAMETERS = json.loads(_vad_params_env)
else:
    # Match faster-whisper defaults when VAD is enabled
    LOCAL_WHISPER_VAD_PARAMETERS = {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        "max_speech_duration_s": float("inf"),
        "min_silence_duration_ms": 2000,
        "speech_pad_ms": 400,
    }


def require_api_key() -> str:
    key = env("MUAPI_API_KEY").strip()
    if not key:
        raise RuntimeError(
            "MUAPI_API_KEY is not set. Add it to your .env file or export it as an env var."
        )
    return key


def require_openai_key() -> str:
    key = env("OPENAI_API_KEY").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Local mode needs an OpenAI key for highlight ranking. "
            "Add it to your .env or export it, or switch back to --mode api."
        )
    return key


def require_gemini_key() -> str:
    key = env("GEMINI_API_KEY").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Local mode needs a Gemini key when LLM_PROVIDER=gemini. "
            "Add it to your .env or export it, or switch LLM_PROVIDER back to openai."
        )
    return key


def require_nim_key() -> str:
    """NIM's hosted catalog needs a key; a self-hosted container does not."""
    key = env("NIM_API_KEY").strip()
    if not key:
        raise RuntimeError(
            "NIM_API_KEY is not set. The hosted NVIDIA catalog needs a key (nvapi-...) — "
            "get one at https://build.nvidia.com. If you are running a self-hosted NIM "
            "container, point NIM_BASE_URL at it (e.g. http://localhost:8000/v1) and set "
            "NIM_API_KEY to any placeholder."
        )
    return key
