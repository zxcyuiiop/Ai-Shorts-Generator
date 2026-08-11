"""Find the most viral-worthy highlights in a transcript.

Logic ported from ViralVadoo's transcript_analysis/highlight_generator.py:
  - content-type / density detection
  - chunking for long videos with overlap
  - virality-criteria prompt
  - score-based dedupe with overlap suppression

The LLM call is pluggable via the `llm_fn` argument so the same prompts can
drive either MuAPI (default, --mode api) or a direct local LLM client
(--mode local).
"""
import json
import re
from typing import Callable, Dict, List, Optional

from . import muapi
from .config import env


LLMFn = Callable[[str], str]


CONTENT_TYPE_PROMPT = """Analyze this video transcript sample and classify the content type.
Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.
Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).
Respond with JSON only: {"content_type": "...", "density": "..."}"""


VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, anger, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""


HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who has studied thousands of viral clips on TikTok, Instagram Reels, and YouTube Shorts. You know exactly what makes viewers stop scrolling, watch to the end, and share.

{virality_criteria}

Content type: {content_type} | Density: {density}

Your task: identify the most viral-worthy highlights from the transcript.

Rules:
- Every highlight must open with a strong HOOK — a line that grabs attention within the first 3 seconds
- {duration_instruction}
- Never cut mid-sentence or mid-thought — each clip must feel complete and self-contained
- Each highlight must be a self‑contained narrative unit: it should convey a complete idea, emotion, or story beat (setup → conflict → payoff, or a full anecdote, joke, or insight) that can be understood without any prior context from the video. If a concept relies on earlier information, briefly recap that information within the highlight so the clip stands on its own.
- Clips must not overlap significantly with each other
- Score 0-100 on viral potential (not general quality)
- {num_clips_instruction}. Aim for DISTINCT moments spread across the whole video — do not stop early if more good moments exist
- For each highlight, identify the single best "hook_sentence" — the opening line that would make someone stop scrolling
- Explain in one sentence why this clip is viral ("virality_reason")
- LANGUAGE: write "title", "hook_sentence" and "virality_reason" strictly in the language the transcript is spoken in (Russian speech → Russian text, English speech → English text). Never translate into another language.

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"score":int,"hook_sentence":"string","virality_reason":"string"}}]}}"""


# Target clip lengths the GUI/CLI can request. Each entry carries the hard
# bounds used to filter the model's output and the wording fed to the prompt --
# the LLM is not reliable at honouring a range it was only told once, so
# out-of-range candidates are dropped afterwards as well.
CLIP_LENGTH_PRESETS = {
    "any": {
        "label": "Any length",
        "min": 15.0,
        "max": 180.0,
        "instruction": (
            "Duration sweet spot: 45-90 seconds. Go shorter (20-44s) only for a perfect "
            "standalone one-liner. Go longer (91-180s) only when a story arc needs full "
            "context to land"
        ),
    },
    "short": {
        "label": "Under 30s",
        "min": 10.0,
        "max": 30.0,
        "instruction": (
            "HARD REQUIREMENT: every clip must be between 10 and 30 seconds long. "
            "Pick punchy standalone moments — a single quotable line or one tight "
            "exchange. Never exceed 30 seconds"
        ),
    },
    "medium": {
        "label": "30-60s",
        "min": 30.0,
        "max": 60.0,
        "instruction": (
            "HARD REQUIREMENT: every clip must be between 30 and 60 seconds long. "
            "Pick a complete thought or a short story beat that resolves inside "
            "60 seconds. Never exceed 60 seconds"
        ),
    },
    "long": {
        "label": "60-90s",
        "min": 60.0,
        "max": 90.0,
        "instruction": (
            "HARD REQUIREMENT: every clip must be between 60 and 90 seconds long. "
            "Pick moments with enough setup and payoff to fill the full minute-plus "
            "without padding. Never exceed 90 seconds"
        ),
    },
    "extended": {
        "label": "90-180s",
        "min": 90.0,
        "max": 180.0,
        "instruction": (
            "HARD REQUIREMENT: every clip must be between 90 and 180 seconds long. "
            "Pick full story arcs or multi-step explanations that genuinely need the "
            "extra time. Never exceed 180 seconds"
        ),
    },
}

DEFAULT_CLIP_LENGTH = "any"


def get_length_preset(name: Optional[str]) -> Dict:
    """Look up a clip-length preset, falling back to the permissive default."""
    return CLIP_LENGTH_PRESETS.get(
        (name or DEFAULT_CLIP_LENGTH).strip().lower(),
        CLIP_LENGTH_PRESETS[DEFAULT_CLIP_LENGTH],
    )


CHUNK_SIZE_SECONDS = int(env("CHUNK_SIZE_SECONDS", "1200") or 1200)       # 20-min chunks for long videos
LONG_VIDEO_THRESHOLD = int(env("LONG_VIDEO_THRESHOLD", "1800") or 1800)     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = int(env("CHUNK_OVERLAP_SECONDS", "60") or 60)
GPT_CALL_TIMEOUT_SECONDS = 300  # cap LLM polls at 5 min — a wedged call should fail fast
MAX_HIGHLIGHT_API_ATTEMPTS = int(env("MAX_HIGHLIGHT_API_ATTEMPTS", "2") or 2)


def call_muapi_llm(prompt: str) -> str:
    """Default LLM backend: MuAPI gpt-5-mini."""
    result = muapi.run(
        "gpt-5-mini",
        {"prompt": prompt},
        label="gpt-5-mini",
        timeout=GPT_CALL_TIMEOUT_SECONDS,
    )

    outputs = result.get("outputs")
    if isinstance(outputs, list) and outputs and isinstance(outputs[0], str) and outputs[0].strip():
        return outputs[0]

    for key in ("output", "text", "response", "result", "content"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = v.get("text") or v.get("content")
            if isinstance(inner, str) and inner.strip():
                return inner
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]

    raise RuntimeError(f"Could not extract gpt-5-mini text from response: {result}")


def _parse_json_loose(raw: str) -> Dict:
    """gpt-5-4 sometimes wraps JSON in markdown fences — strip and parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _sanitize_highlights(raw_highlights: object, duration: float) -> List[Dict]:
    """Normalize model output into the expected shape; skip invalid entries."""
    if not isinstance(raw_highlights, list):
        return []

    max_end = duration if duration > 0 else float("inf")
    cleaned: List[Dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue

        start = _coerce_float(item.get("start_time"), default=-1.0)
        end = _coerce_float(item.get("end_time"), default=-1.0)
        if start < 0 or end <= start:
            continue

        if max_end != float("inf"):
            start = min(start, max_end)
            end = min(end, max_end)
            if end <= start:
                continue

        cleaned.append(
            {
                "title": str(item.get("title") or "Untitled Highlight").strip(),
                "start_time": start,
                "end_time": end,
                "score": max(0, min(100, _coerce_int(item.get("score"), default=0))),
                "hook_sentence": str(item.get("hook_sentence") or "").strip(),
                "virality_reason": str(item.get("virality_reason") or "").strip(),
            }
        )

    return cleaned


def detect_content_type(transcript: Dict, llm_fn: LLMFn = call_muapi_llm) -> Dict[str, str]:
    segments = transcript.get("segments", [])
    sample = " ".join(s["text"] for s in segments[:25])[:3000]
    prompt = f"{CONTENT_TYPE_PROMPT}\n\nTranscript sample:\n{sample}"
    try:
        raw = llm_fn(prompt)
        result = _parse_json_loose(raw)
        # Ensure we have the expected keys; if not, fall back to defaults.
        if not isinstance(result, dict) or "content_type" not in result or "density" not in result:
            return {"content_type": "other", "density": "medium"}
        return result
    except Exception:
        return {"content_type": "other", "density": "medium"}


def build_transcript_text(transcript: Dict) -> str:
    segments = transcript.get("segments", [])
    return "\n".join(f"[{s['start']:.1f}s] {s['text'].strip()}" for s in segments)


def chunk_transcript(transcript: Dict) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration", segments[-1]["end"] if segments else 0)
    chunks = []
    start = 0
    while start < duration:
        end = min(start + CHUNK_SIZE_SECONDS, duration)
        chunk_segs = [
            s for s in segments
            if s["start"] >= start and s["end"] <= end + CHUNK_OVERLAP_SECONDS
        ]
        if chunk_segs:
            chunk = dict(transcript)
            chunk["segments"] = chunk_segs
            chunk["duration"] = end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        start += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS
    return chunks


def _filter_by_length(highlights: List[Dict], preset: Dict) -> List[Dict]:
    """Drop clips outside the requested length window.

    Models routinely ignore a stated range, so the prompt instruction alone is
    not enough -- without this the user picks "under 30s" and still gets 90s
    clips. Over-long candidates are trimmed to the cap when their opening still
    clears the minimum; too-short ones cannot be salvaged and are dropped.
    """
    lo, hi = preset["min"], preset["max"]
    kept: List[Dict] = []
    for h in highlights:
        start = float(h["start_time"])
        end = float(h["end_time"])
        length = end - start
        if length < lo:
            continue
        if length > hi:
            end = start + hi
            h = {**h, "end_time": end}
        kept.append(h)
    return kept


def call_highlight_api(
    transcript_text: str,
    content_info: Dict,
    duration: float,
    num_clips: int,
    is_chunk: bool = False,
    llm_fn: LLMFn = call_muapi_llm,
    clip_length: Optional[str] = None,
) -> Dict:
    # Ask for more than the user wants so dedupe has headroom, but don't undercut
    # the target: "at least N" makes the model stop at N, and after dedupe and the
    # length filter fewer than num_clips clips survive. Ask for the FULL target.
    target = max(num_clips * 2, 5)
    natural_max = max(2 if is_chunk else 3, int(duration / 90))
    # The old cap of 8 was what made a request for 10 clips collapse to ~4.
    min_clips = min(target, max(natural_max, num_clips), num_clips)
    preset = get_length_preset(clip_length)
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        virality_criteria=VIRALITY_CRITERIA,
        content_type=content_info.get("content_type", "other"),
        density=content_info.get("density", "medium"),
        duration_instruction=preset["instruction"],
        num_clips_instruction=f"Generate at least {min_clips} highlights",
    )
    base_prompt = f"{system}\n\nTranscript:\n{transcript_text}"
    prompt = base_prompt
    last_error = "unknown"

    for attempt in range(1, MAX_HIGHLIGHT_API_ATTEMPTS + 1):
        raw = llm_fn(prompt)
        # Log raw response preview (truncate for readability)
        raw_preview = raw[:200].replace("\n", " ") if raw else ""
        print(f"[highlights] attempt {attempt}/{MAX_HIGHLIGHT_API_ATTEMPTS} raw preview: {raw_preview}", flush=True)
        try:
            parsed = _parse_json_loose(raw)
            highlights_raw = parsed.get("highlights", [])
            print(f"[highlights] parsed JSON, got {len(highlights_raw)} raw highlights", flush=True)
            highlights = _sanitize_highlights(highlights_raw, duration=duration)
            print(f"[highlights] after sanitization: {len(highlights)} valid highlights", flush=True)
            if highlights:
                lengths = [h["end_time"] - h["start_time"] for h in highlights]
                print(f"[highlights] highlight lengths (s): {[round(l,2) for l in lengths]}", flush=True)
            in_range = _filter_by_length(highlights, preset)
            print(f"[highlights] after length filter ({preset['min']}-{preset['max']}s): {len(in_range)} highlights", flush=True)
            if in_range:
                dropped = len(highlights) - len(in_range)
                if dropped:
                    print(
                        f"[highlights] dropped {dropped} clip(s) outside "
                        f"{preset['min']:.0f}-{preset['max']:.0f}s",
                        flush=True,
                    )
                return {"highlights": in_range}
            last_error = (
                "no highlights within the requested length range"
                if highlights else "no valid highlights in response"
            )
        except Exception as e:
            last_error = str(e)
            print(f"[highlights] exception during processing: {e}", flush=True)

        if attempt < MAX_HIGHLIGHT_API_ATTEMPTS:
            print(
                f"[highlights] invalid model output on attempt {attempt}/{MAX_HIGHLIGHT_API_ATTEMPTS}; retrying",
                flush=True,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights' array."
                + " Each item must include: title, start_time, end_time, score, hook_sentence, virality_reason."
                + f" Every clip MUST last between {preset['min']:.0f} and {preset['max']:.0f} seconds."
                + " If a moment is shorter than the minimum, extend equally before and after the hook sentence"
                + " (while keeping the hook within the first 3 seconds) to reach the minimum length."
                + " No markdown fences, no commentary."
            )

    raise RuntimeError(
        f"Highlight generator produced invalid output after {MAX_HIGHLIGHT_API_ATTEMPTS} attempts: {last_error}"
    )


def dedupe_highlights(highlights: List[Dict]) -> List[Dict]:
    """Drop a highlight if it overlaps >50% with a higher-scoring one already kept."""
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)
    kept: List[Dict] = []
    for h in highlights:
        h_start = float(h["start_time"])
        h_end = float(h["end_time"])
        h_dur = h_end - h_start
        overlapping = False
        for k in kept:
            latest_start = max(h_start, float(k["start_time"]))
            earliest_end = min(h_end, float(k["end_time"]))
            overlap = earliest_end - latest_start
            if overlap > 0 and overlap > 0.5 * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(h)
    return kept


def get_highlights(
    transcript: Dict,
    num_clips: int = 3,
    llm_fn: Optional[LLMFn] = None,
    clip_length: Optional[str] = None,
) -> Dict:
    """Main entry point — returns {highlights: [...]} sorted by score.

    `llm_fn` swaps the underlying LLM. Defaults to MuAPI gpt-5-mini; local
    mode passes in a local LLM-backed callable.

    `clip_length` selects a CLIP_LENGTH_PRESETS entry ("short", "medium",
    "long", "extended") to constrain how long each clip runs.
    """
    llm_fn = llm_fn or call_muapi_llm
    duration = transcript.get("duration", 0)
    preset = get_length_preset(clip_length)
    content_info = detect_content_type(transcript, llm_fn=llm_fn)
    # Fallback logging for debugging
    content_type = content_info.get("content_type") if content_info else None
    density = content_info.get("density") if content_info else None
    print(
        f"[highlights] content={content_type} density={density} duration={duration:.0f}s "
        f"target={preset['label']}",
        flush=True,
    )

    if duration >= LONG_VIDEO_THRESHOLD:
        chunks = chunk_transcript(transcript)
        print(f"[highlights] long video — splitting into {len(chunks)} chunks", flush=True)
        all_highlights: List[Dict] = []
        for i, chunk in enumerate(chunks):
            offset = chunk.get("_offset", 0)
            text = build_transcript_text(chunk)
            print(f"[highlights] chunk {i + 1}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            result = call_highlight_api(
                text, content_info, chunk["duration"], num_clips=num_clips,
                is_chunk=True, llm_fn=llm_fn, clip_length=clip_length,
            )
            for h in result.get("highlights", []):
                h["start_time"] = float(h["start_time"]) + offset
                h["end_time"] = float(h["end_time"]) + offset
                all_highlights.append(h)
        highlights = dedupe_highlights(all_highlights)
    else:
        text = build_transcript_text(transcript)
        result = call_highlight_api(
            text, content_info, duration, num_clips=num_clips,
            llm_fn=llm_fn, clip_length=clip_length,
        )
        highlights = dedupe_highlights(result.get("highlights", []))

    return {"highlights": highlights}
