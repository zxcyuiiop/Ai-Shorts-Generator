# -*- coding: utf-8 -*-
"""Filename sanitizing for user-facing clip names (highlight titles).

Unlike ``local/downloader._sanitize_folder_name`` (ASCII-only folder names for
the downloader), this keeps Unicode — Russian titles stay readable in the
Explorer — while stripping only what Windows forbids.
"""
import re

# Windows-forbidden filename characters <>:"/\|?* plus C0/C1 control chars.
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE_RUN = re.compile(r"\s+")


def _safe_title_name(title: str, max_len: int = 80) -> str:
    """Turn a highlight title into a safe file basename (no extension).

    Strips Windows-forbidden / control chars, collapses whitespace runs to
    single spaces, keeps spaces and Cyrillic (any Unicode), truncates to
    ``max_len`` characters, and trims trailing dots/spaces (illegal on
    Windows). Returns "" when nothing usable remains, so the caller can fall
    back to its default naming.
    """
    if not isinstance(title, str):
        return ""
    cleaned = _FORBIDDEN_CHARS.sub("", title)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip(" .")
    cleaned = cleaned[:max_len].strip(" .")
    return cleaned
