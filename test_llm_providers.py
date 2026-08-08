"""Exercise the Ollama and NIM backends with mocked HTTP.

Verifies the real request shape (num_ctx, format=json, model, path) and the
error paths, without needing Ollama or a NIM key installed.
"""
import json
import os
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPLY = '{"highlights": [{"title": "T", "start_time": 1.0, "end_time": 40.0, "score": 90, "hook_sentence": "h", "virality_reason": "r"}]}'

captured = {}
mode = {"fail": None}


def mock_requests_post(url, **kwargs):
    """Mock requests.post for Ollama native API."""
    captured["url"] = url
    captured["body"] = kwargs.get("json", {})

    resp = Mock()
    if mode["fail"] == "404":
        resp.status_code = 404
        resp.json.return_value = {"error": "model not found"}
        resp.raise_for_status.side_effect = Exception("404")
    elif mode["fail"] == "connection":
        from requests.exceptions import ConnectionError
        raise ConnectionError("connection refused")
    else:
        resp.status_code = 200
        resp.json.return_value = {"response": REPLY, "done": True}
        resp.raise_for_status = Mock()
    return resp


def mock_openai_client(api_key, base_url, timeout):
    """Mock OpenAI client for NIM."""
    captured["base_url"] = base_url
    captured["api_key"] = api_key

    client = Mock()
    completion = Mock()
    completion.choices = [Mock()]
    completion.choices[0].message.content = REPLY

    def create_call(**kwargs):
        captured["model"] = kwargs.get("model")
        captured["messages"] = kwargs.get("messages")
        return completion

    client.chat.completions.create = create_call
    return client


def patch_openai():
    """Patch the openai module the backend imports lazily inside the function."""
    fake = Mock()
    fake.OpenAI = mock_openai_client
    return patch.dict(sys.modules, {"openai": fake})


def set_provider(name):
    """Select the LLM provider the way the app does, via config overrides."""
    from shorts_generator import config

    current = dict(getattr(config._overrides, "values", {}) or {})
    current["LLM_PROVIDER"] = name
    config.set_overrides(current)


def main():
    os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
    os.environ["OLLAMA_MODEL"] = "llama3.1:8b"
    os.environ["OLLAMA_NUM_CTX"] = "16384"
    os.environ["NIM_BASE_URL"] = "https://integrate.api.nvidia.com/v1"
    os.environ["NIM_API_KEY"] = "nvapi-test"
    os.environ["NIM_MODEL"] = "meta/llama-3.1-8b-instruct"

    from shorts_generator.local import llm
    from shorts_generator import highlights
    import importlib

    failures = []

    def check(name, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    # --- Ollama ---
    with patch("requests.post", side_effect=mock_requests_post):
        out = llm.call_ollama_llm("find highlights")

    b = captured["body"]
    check("ollama hits native /api/generate", "/api/generate" in captured["url"], captured["url"])
    check("ollama sends model", b.get("model") == "llama3.1:8b", str(b.get("model")))
    check("ollama raises num_ctx past 4096 default",
          b.get("options", {}).get("num_ctx") == 16384, str(b.get("options")))
    check("ollama constrains to json", b.get("format") == "json", str(b.get("format")))
    check("ollama disables streaming", b.get("stream") is False, str(b.get("stream")))
    check("ollama returns model text", out == REPLY, out[:40])

    # Reply must survive the real parser highlights.py uses.
    parsed = highlights._sanitize_highlights(highlights._parse_json_loose(out)["highlights"], 120.0)
    check("ollama reply parses into highlights", len(parsed) == 1 and parsed[0]["score"] == 90, str(parsed))

    # --- NIM ---
    with patch_openai():
        out = llm.call_nim_llm("find highlights")

    check("nim uses correct base_url", captured["base_url"] == "https://integrate.api.nvidia.com/v1")
    check("nim sends catalog model name",
          captured["model"] == "meta/llama-3.1-8b-instruct", str(captured.get("model")))
    check("nim sends api key", captured["api_key"] == "nvapi-test", str(captured["api_key"]))
    check("nim returns model text", out == REPLY, out[:40])

    # --- dispatch ---
    # The provider is read from config at call time, not from a module global,
    # so it has to be set through the override store -- assigning
    # llm.LLM_PROVIDER silently does nothing and lets calls hit the real API.
    with patch("requests.post", side_effect=mock_requests_post):
        set_provider("ollama")
        check("dispatch: ollama", llm.call_local_llm("x") == REPLY)

    with patch_openai():
        set_provider("nim")
        check("dispatch: nim", llm.call_local_llm("x") == REPLY)
        set_provider("nvidia")
        check("dispatch: nvidia alias", llm.call_local_llm("x") == REPLY)

    set_provider("bogus")
    try:
        llm.call_local_llm("x")
        check("dispatch: unknown provider errors", False, "no raise")
    except RuntimeError as e:
        check("dispatch: unknown provider errors", "ollama" in str(e) and "nim" in str(e), str(e))

    # --- error paths ---
    mode["fail"] = "404"
    with patch("requests.post", side_effect=mock_requests_post):
        try:
            llm.call_ollama_llm("x")
            check("ollama missing model -> pull hint", False, "no raise")
        except Exception as e:
            check("ollama missing model -> pull hint", "ollama pull" in str(e), str(e).split(chr(10))[0])
    mode["fail"] = None

    mode["fail"] = "connection"
    with patch("requests.post", side_effect=mock_requests_post):
        try:
            llm.call_ollama_llm("x")
            check("ollama down -> serve hint", False, "no raise")
        except RuntimeError as e:
            check("ollama down -> serve hint", "ollama serve" in str(e), str(e).split(chr(10))[0])
    mode["fail"] = None

    os.environ.pop("NIM_API_KEY", None)
    from shorts_generator import config as cfg
    importlib.reload(cfg)
    importlib.reload(llm)
    with patch_openai():
        try:
            llm.call_nim_llm("x")
            check("nim without key -> build.nvidia.com hint", False, "no raise")
        except RuntimeError as e:
            check("nim without key -> build.nvidia.com hint", "build.nvidia.com" in str(e),
                  str(e).split(chr(10))[0])
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
