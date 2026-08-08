"""Local LLM backend — OpenAI, Gemini, Ollama, or NVIDIA NIM, selected by LLM_PROVIDER.

Every setting is read through config.env() at call time, never cached at import
time — the web GUI binds per-request overrides (keys/models pasted into the
browser) that would otherwise be missed.
"""
from ..config import env


def call_openai_llm(prompt: str) -> str:
    """OpenAI Chat Completions backend used by --mode local."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    from ..config import require_openai_key

    client = OpenAI(api_key=require_openai_key())
    response = client.chat.completions.create(
        model=env("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
        timeout=float(env("LOCAL_LLM_TIMEOUT", "600")),
    )
    return response.choices[0].message.content or ""


def call_gemini_llm(prompt: str) -> str:
    """Gemini backend used by --mode local when LLM_PROVIDER=gemini."""
    try:
        from google import genai  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for LLM_PROVIDER=gemini. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    from ..config import require_gemini_key

    client = genai.Client(api_key=require_gemini_key())
    response = client.models.generate_content(
        model=env("GEMINI_MODEL", "gemini-2.5-flash"),
        contents=prompt,
        config={
            "temperature": 0.2,
            "response_mime_type": "application/json",
            "max_output_tokens": 8192,
        },
    )
    return response.text or ""


def call_ollama_llm(prompt: str) -> str:
    """Ollama backend — fully local, no API key, no extra pip install.

    Uses Ollama's native /api/generate rather than its OpenAI-compatible shim so
    we can set num_ctx. The OpenAI shim gives no way to raise the context window,
    and Ollama's 4096-token default silently truncates highlight prompts.
    """
    import requests

    base_url = env("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = env("OLLAMA_MODEL", "llama3.1:8b")

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",  # constrain to JSON — highlights.py parses the reply
        "options": {
            "temperature": 0.2,
            "num_ctx": int(env("OLLAMA_NUM_CTX", "16384")),
        },
    }

    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json=payload,
            timeout=float(env("LOCAL_LLM_TIMEOUT", "600")),
        )
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"Could not reach Ollama at {base_url}. Is it running? Start it with:\n"
            "    ollama serve\n"
            f"and make sure the model is pulled:\n    ollama pull {model}"
        ) from e

    if response.status_code == 404:
        raise RuntimeError(
            f"Ollama has no model named {model!r}. Pull it first:\n"
            f"    ollama pull {model}"
        )
    response.raise_for_status()
    return response.json().get("response", "")


def call_nim_llm(prompt: str) -> str:
    """NVIDIA NIM backend — hosted catalog or a self-hosted NIM container.

    NIM speaks the OpenAI chat-completions protocol, so we reuse the openai
    client and only swap base_url.
    """
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for LLM_PROVIDER=nim (NIM speaks the OpenAI protocol). "
            "Install it with:\n    pip install -r requirements-local.txt"
        ) from e

    from ..config import require_nim_key

    client = OpenAI(
        api_key=require_nim_key(),
        base_url=env("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/"),
        timeout=float(env("LOCAL_LLM_TIMEOUT", "600")),
    )
    response = client.chat.completions.create(
        model=env("NIM_MODEL", "meta/llama-3.1-8b-instruct"),
        temperature=0.2,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def call_local_llm(prompt: str, provider: str = None) -> str:
    """Dispatch to a local LLM provider.

    `provider` overrides the LLM_PROVIDER default. It is passed explicitly
    (rather than mutating a module global) so concurrent jobs can use
    different providers without clobbering each other.
    """
    provider = (provider or env("LLM_PROVIDER", "openai")).lower()
    if provider == "nvidia":
        provider = "nim"  # common alias for the NVIDIA NIM backend
    if provider == "openai":
        return call_openai_llm(prompt)
    elif provider == "gemini":
        return call_gemini_llm(prompt)
    elif provider == "ollama":
        return call_ollama_llm(prompt)
    elif provider == "nim":
        return call_nim_llm(prompt)
    else:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER: {provider} "
            "(supported: openai, gemini, ollama, nim)"
        )


def make_local_llm_fn(provider: str = None):
    """Build a single-arg llm_fn bound to `provider`, for highlights.get_highlights."""
    return lambda prompt: call_local_llm(prompt, provider=provider)
