"""Hermetic coverage of config.env precedence and settings_store behaviour.

The GUI's settings store is a strict whitelist (``settings_store.ALLOWED_FIELDS``):
any key not in it is dropped on load/save. ``config.env`` is case-sensitive and
looks up exactly the name it was given, so only ALLOWED keys that are also
uppercase can be staged in the settings layer and then read back through
``config.env`` — the set of such keys is discovered dynamically (currently just
``MUAPI_KEY``).

Nothing here touches the real settings.local.json: ``SETTINGS_PATH`` is pointed
into a fresh ``tempfile.mkdtemp()`` before the first use and that temp dir is
removed on exit. Managed process-env keys are always restored. Threads only
exercise the thread-local override store; no subprocess, no network, no heavy
imports (Flask, cv2, ffmpeg).
"""

import os
import shutil
import sys
import tempfile
import threading

# Redirect the settings file BEFORE any load()/save() -- see module notes.
_TMP = tempfile.mkdtemp(prefix="cfg-store-test-")

from shorts_generator import settings_store  # noqa: E402

settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

from shorts_generator import config  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


# Keys usable in the settings-file layer AND readable via config.env (see the
# module docstring): allowed by the store and uppercase.
LAYER_KEYS = sorted(k for k in settings_store.ALLOWED_FIELDS if k.upper() == k)
if LAYER_KEYS:
    FILE_KEY = LAYER_KEYS[0]                    # e.g. MUAPI_KEY
    FILE_VALUE = "from-settings-file"           # what the file layer holds
else:                                           # pragma: no cover - store layout changed
    FILE_KEY = FILE_VALUE = None

OVERRIDE_KEY = "TASK6_OVERRIDE_T1"  # not in the store / env; used only via set_overrides
DEFAULT_KEY = "TASK6_DEFAULT_UNSET"  # nobody sets this; only used with a default


def reset():
    """Clear thread-local overrides and the managed process-env key."""
    config.clear_overrides()
    if FILE_KEY:
        os.environ.pop(FILE_KEY, None)
    os.environ.pop(OVERRIDE_KEY, None)


def test_default_and_precedence():
    reset()
    # Clean slate for OVERRIDE_KEY: no override, not an ALLOWED key (so the file
    # layer can never answer for it), and not in os.environ -> default.
    check("default when nobody sets the key",
          config.env(OVERRIDE_KEY, "dflt") == "dflt", config.env(OVERRIDE_KEY, "dflt"))
    check("default as last resort for a fully-unset key",
          config.env(DEFAULT_KEY, "lastresort") == "lastresort")

    # override wins over every lower layer it could shadow (file/env/default).
    config.set_overrides({OVERRIDE_KEY: "ovr"})
    check("override beats default/env/file layers",
          config.env(OVERRIDE_KEY, "d") == "ovr", config.env(OVERRIDE_KEY, "d"))
    reset()


def test_file_layer_beats_env():
    if not FILE_KEY:
        print("SKIP  no uppercase ALLOWED key -> file-layer precedence checks skipped")
        return
    reset()
    # The settings file holds FILE_KEY; the same name in the process env must lose.
    os.environ[FILE_KEY] = "from-process-env"
    check("settings file beats process env",
          config.env(FILE_KEY, "d") == FILE_VALUE, config.env(FILE_KEY, "d"))
    os.environ.pop(FILE_KEY, None)
    # With the env key gone, the file layer still answers.
    check("settings file answers over the default",
          config.env(FILE_KEY, "d") == FILE_VALUE, config.env(FILE_KEY, "d"))


def test_falsy_and_empty_overrides():
    reset()
    # Falsy-but-present override "0" must WIN over the caller's default and the
    # file layer -- "0" is truthy as a string, so set_overrides keeps it and env()
    # returns it verbatim.
    config.set_overrides({OVERRIDE_KEY: "0"})
    check("falsy override '0' wins over default",
          config.env(OVERRIDE_KEY, "1") == "0", config.env(OVERRIDE_KEY, "1"))
    reset()

    # A genuinely-empty override is dropped by set_overrides's None/"" filter, so
    # env() falls through to the next layer (the process env here).
    os.environ[OVERRIDE_KEY] = "fallback-env"
    config.set_overrides({OVERRIDE_KEY: ""})   # dropped on bind
    check("empty-string override drops to the next layer",
          config.env(OVERRIDE_KEY, "d") == "fallback-env", config.env(OVERRIDE_KEY, "d"))
    reset()


def test_thread_isolation():
    if not FILE_KEY:
        print("SKIP  no uppercase ALLOWED key -> thread-isolation fallback check limited")
    reset()
    main_seen, t_seen, errs = [], [], []

    # Bind an override in the main thread for the run; the worker must not see it.
    config.set_overrides({OVERRIDE_KEY: "ovr-main"})

    def worker():
        try:
            # OVERRIDE_KEY is not an ALLOWED field and not in the env, so with no
            # override visible in this thread it falls to whatever default we pass.
            t_seen.append(config.env(OVERRIDE_KEY, "fresh-default"))
            config.set_overrides({OVERRIDE_KEY: "ovr-thread"})    # bind for this thread only
            t_seen.append(config.env(OVERRIDE_KEY, "fresh-default"))
            config.clear_overrides()
            # After clearing, the override is gone and the default answers again.
            t_seen.append(config.env(OVERRIDE_KEY, "fresh-default"))
        except Exception as e:  # pragma: no cover - failure path
            errs.append(repr(e))

    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=10)

    main_seen.append(config.env(OVERRIDE_KEY, "d"))  # main's own binding survived

    check("worker does not see main-thread override (fresh default answers)",
          t_seen[0] == "fresh-default", str(t_seen))
    check("worker set_overrides is thread-local",
          t_seen[1] == "ovr-thread", str(t_seen))
    check("worker clear_overrides restores its default",
          t_seen[2] == "fresh-default", str(t_seen))
    check("main-thread override unaffected by the worker",
          main_seen == ["ovr-main"], str(main_seen))
    check("no thread errors", not errs, str(errs))
    reset()


def test_settings_store_round_trip_and_secrets():
    # save() / load() round-trip through the redirected SETTINGS_PATH.
    settings_store.save({"nim_key": "real-secret", "url": "https://x", "num_clips": 5})
    loaded = settings_store.load()
    check("store round-trips saved values",
          loaded.get("nim_key") == "real-secret" and loaded.get("num_clips") == 5,
          str(loaded))

    # mask_secrets replaces every stored secret with the mask but leaves the
    # on-disk value intact and non-secret fields alone.
    masked = settings_store.mask_secrets(loaded)
    check("mask_secrets masks secrets on read",
          masked.get("nim_key") == settings_store.MASK, str(masked.get("nim_key")))
    check("mask_secrets leaves non-secrets alone",
          masked.get("url") == "https://x", str(masked.get("url")))
    check("stored secret still plaintext after masking",
          settings_store.load().get("nim_key") == "real-secret")

    # resolve_secret: a fresh value wins; the mask expands to the stored secret;
    # an empty submitted value is returned as-is.
    check("resolve_secret returns a fresh submitted value",
          settings_store.resolve_secret("nim_key", "nvapi-new") == "nvapi-new")
    check("resolve_secret expands the mask from disk",
          settings_store.resolve_secret("nim_key", settings_store.MASK) == "real-secret")
    check("resolve_secret passes a falsy submit through",
          settings_store.resolve_secret("nim_key", "") == "")


def main():
    if FILE_KEY:
        # Seed the settings file once so the file-layer tests have a stable value.
        settings_store.save({FILE_KEY: FILE_VALUE})

    try:
        test_default_and_precedence()
        test_file_layer_beats_env()
        test_falsy_and_empty_overrides()
        test_thread_isolation()
        test_settings_store_round_trip_and_secrets()
    finally:
        reset()
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
