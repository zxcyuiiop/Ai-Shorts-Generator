"""Checks for the YouTube anti-bot cookie pass-through in local downloads.

Covers:
  - _cookie_opts(): env unset -> {}; YTDLP_COOKIES_FROM_BROWSER -> cookiesfrombrowser
    tuple (with optional profile); YTDLP_COOKIES -> cookiefile; browser beats file
  - _wrap_bot_error(): anti-bot phrases become a RuntimeError naming the .env
    knobs; other exceptions pass through untouched
  - download_youtube_local(): cookie kwargs actually reach yt_dlp.YoutubeDL and
    a bot-wall download raises the friendly message

yt_dlp objects are stubbed -- no network access happens.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator import config as cfg  # noqa: E402
from shorts_generator.local import downloader as dl  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def run_cookie_opts_checks():
    cfg.clear_overrides()
    check("no env -> no cookie kwargs", dl._cookie_opts() == {},
          str(dl._cookie_opts()))

    cfg.set_overrides({"YTDLP_COOKIES_FROM_BROWSER": "chrome"})
    check("browser name -> cookiesfrombrowser tuple",
          dl._cookie_opts() == {"cookiesfrombrowser": ("chrome",)},
          str(dl._cookie_opts()))

    cfg.set_overrides({"YTDLP_COOKIES_FROM_BROWSER": "firefox,default-release"})
    check("browser,profile -> 2-tuple",
          dl._cookie_opts() == {"cookiesfrombrowser": ("firefox", "default-release")},
          str(dl._cookie_opts()))

    cfg.set_overrides({"YTDLP_COOKIES_FROM_BROWSER": "  Edge  "})
    check("whitespace + case-insensitive name",
          dl._cookie_opts() == {"cookiesfrombrowser": ("Edge",)},
          str(dl._cookie_opts()))

    cfg.clear_overrides()
    cfg.set_overrides({"YTDLP_COOKIES": r"C:\tmp\cookies.txt"})
    check("cookies file -> cookiefile",
          dl._cookie_opts() == {"cookiefile": r"C:\tmp\cookies.txt"},
          str(dl._cookie_opts()))

    cfg.set_overrides({"YTDLP_COOKIES": r"C:\tmp\cookies.txt",
                       "YTDLP_COOKIES_FROM_BROWSER": "brave"})
    check("browser env wins over file",
          dl._cookie_opts() == {"cookiesfrombrowser": ("brave",)},
          str(dl._cookie_opts()))
    cfg.clear_overrides()


def run_wrap_checks():
    bot = Exception(
        "ERROR: [youtube] PcLsS_hTprE: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )
    wrapped = dl._wrap_bot_error(bot)
    check("bot wall -> RuntimeError", isinstance(wrapped, RuntimeError),
          type(wrapped).__name__)
    check("message names the browser knob",
          "YTDLP_COOKIES_FROM_BROWSER" in str(wrapped))
    check("message names the file knob", "YTDLP_COOKIES" in str(wrapped))
    check("message keeps the original yt-dlp text", "Sign in to confirm" in str(wrapped))

    other = ValueError("HTTP Error 503: Service Unavailable")
    check("503 bot-ish error also wrapped",  # '403' marker, not 503, mind
          dl._wrap_bot_error(Exception("HTTP Error 403: Forbidden")).args[0].startswith(
              "YouTube rejected"),
          "")
    check("non-bot error untouched",
          dl._wrap_bot_error(other) is other)


class _FakeYDL:
    """Records the opts YoutubeDL was built with; optionally raises the wall."""
    last_opts = None
    raise_bot = False

    def __init__(self, opts):
        _FakeYDL.last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        if _FakeYDL.raise_bot:
            raise Exception(
                "Sign in to confirm you're not a bot. Use --cookies-from-browser"
            )
        return {
            "id": "abc123XYZ", "ext": "mp4", "title": "demo",
            "requested_formats": [],
        }

    def prepare_filename(self, info):
        # Not actually written -- download_youtube_local has fall-backs, so use
        # the simplest contract it understands: an existing file in out_dir.
        import tempfile
        out_dir = _FakeYDL.out_dir
        path = os.path.join(out_dir, "source_abc123XYZ.mp4")
        with open(path, "wb") as fh:
            fh.write(b"fake")
        return path


def run_download_flow_checks():
    import tempfile
    import types

    tmp = tempfile.mkdtemp(prefix="ytdlp-cookies-")
    _FakeYDL.out_dir = tmp
    _FakeYDL.raise_bot = False
    fake_mod = types.SimpleNamespace(YoutubeDL=_FakeYDL)
    real_import = dl._import_ytdlp
    real_out_dir = dl.LOCAL_OUTPUT_DIR
    try:
        dl._import_ytdlp = lambda: fake_mod
        dl.LOCAL_OUTPUT_DIR = tmp  # keep the run out of the real output dir

        def _clear_cache():
            """Drop source_* files between scenarios so each one really hits
            the (stubbed) downloader instead of the on-disk reuse cache."""
            for name in os.listdir(tmp):
                if name.startswith("source_"):
                    os.remove(os.path.join(tmp, name))

        cfg.clear_overrides()
        dl.download_youtube_local("https://www.youtube.com/watch?v=abc123XYZ")
        check("no env -> YoutubeDL opts have no cookie keys",
              _FakeYDL.last_opts is not None
              and "cookiesfrombrowser" not in _FakeYDL.last_opts
              and "cookiefile" not in _FakeYDL.last_opts,
              str(_FakeYDL.last_opts and sorted(_FakeYDL.last_opts)))
        _clear_cache()

        cfg.set_overrides({"YTDLP_COOKIES_FROM_BROWSER": "chrome"})
        dl.download_youtube_local("https://www.youtube.com/watch?v=abc123XYZ")
        check("browser env forwarded to yt-dlp",
              _FakeYDL.last_opts.get("cookiesfrombrowser") == ("chrome",),
              str(_FakeYDL.last_opts))
        _clear_cache()

        _FakeYDL.raise_bot = True
        try:
            dl.download_youtube_local("https://www.youtube.com/watch?v=abc123XYZ")
        except RuntimeError as e:
            check("bot wall surfaces as friendly RuntimeError",
                  "YTDLP_COOKIES_FROM_BROWSER" in str(e), str(e)[:80])
        else:
            check("bot wall surfaces as friendly RuntimeError", False, "no exception")
    finally:
        dl._import_ytdlp = real_import
        dl.LOCAL_OUTPUT_DIR = real_out_dir
        cfg.clear_overrides()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    run_cookie_opts_checks()
    run_wrap_checks()
    run_download_flow_checks()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
