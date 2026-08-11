# IMPROVEMENTS.md

Overnight hardening pass on the AI YouTube Shorts Generator (Flask GUI). All
work is committed on `main`, tests are green (16/16 via `run_all_tests.py`),
and the GUI server runs on `127.0.0.1:5000` by default.

> Русская сводка внизу файла.

---

## Security

- **`/api/*` gated behind `GUI_TOKEN`** (`static/settings` + `?token=` /
  Authorization Bearer). No token + non-loopback host still boots, but prints a
  loud banner. `92e74c3`
- **SSRF allow-list** on the video URL in local mode — only `youtube.com` /
  `music.youtube.com` / `youtu.be` accepted; scheme and host validated. `92e74c3`
- **Input validation** across `/api/generate`, `/api/shorts/save`, upload
  paths: type/range checks, path traversal blocked, filename sanitised. `92e74c3`
- **SSE/progress leak fixed** — progress queues and job records are cleaned up
  once a job finishes. `92e74c3`
- **Sanitized job errors** — internal / filesystem details no longer leak to
  the client. `92e74c3`
- **Streaming upload cap** on file size. `92e74c3`
- **Safe bind default** — `python app.py` now binds `127.0.0.1`, not `0.0.0.0`.
  Set `GUI_HOST=0.0.0.0` (+ `GUI_TOKEN`) only if you really want the LAN to
  reach the GUI. `f87a596`

## UX / GUI

- Queue no longer duplicates in-flight jobs; `activeJobId` is reset correctly. (wave2f `de01046`)
- **Empty / error / loading states** everywhere (skeleton list, "no shorts" card). `de01046`
- **Network-failure toasts** on every fetch; a visible close button on each toast. `de01046`
- **Delete confirmation** before dropping a short. `de01046`
- **Review flow reachable by keyboard**: focus moves into the modal, Esc closes. `de01046`
- **Upload progress bar** when sending a local file. `de01046`
- **Session restore on reload** — the in-progress job (and its progress)
  re-attaches after a browser refresh. `de01046`
- **Download-all** button once a batch is finalised. `de01046`
- **API-key fields mask placeholders** (•••) so keys aren't displayed. `de01046`
- **Re-run a job** with the same settings from the review UI (`POST /api/jobs/<id>/rerun`). `61d0588`
- **Review feed sorted by virality score** (best first). `61d0588`
- **ETA timer** — elapsed plus "≈N min left" while a job runs. `61d0588`
- **Copy link** buttons on result cards and in review (falls back to
  `execCommand` when the Clipboard API is unavailable). `61d0588`, `3c8e112`
- **Result cards** show title, score badge, time window, duration. `3c8e112`

## Processing / pipeline

- **`BLUR_BARS=0` honoured per request** — blurred side bars can be turned off,
  with a real-render regression test. `6d2422c`
- **Music upload hardened** — wired end to end, with template/JS contract checks. `f094f9d`
- **Review step no longer stalls** — after a save the next clip advances. `3148150`
- **Highlight chunking is tunable** via env (`LONG_VIDEO_THRESHOLD`,
  `CHUNK_SIZE_SECONDS`, `CHUNK_OVERLAP_SECONDS`, `MAX_HIGHLIGHT_API_ATTEMPTS`). `3c8e112`

## Docs / DX / packaging

- **`.env.example` documents every env var** the code reads — API keys, Whisper,
  cookies workaround, captions, face-track, silence cut, blur pad, overlay,
  music, GUI host/port/token, LLM timeouts, highlight chunking. `94dbf7b`, `f87a596`
- **Stale `WEB_INTERFACE.md` removed**; README points at the GUI. `01557ad`, `94dbf7b`
- **Installers** (`install.bat` / `install.sh`) idempotent — re-running won't
  wipe a working venv. `94dbf7b`

## Tests

`run_all_tests.py` → **16/16 PASS**. New/extended coverage: SSRF allow-list,
token gating, input validation, upload cap, blur-bars toggle, music wiring,
review/advance flow, GUI feature contracts, session restore.

---

## Known follow-ups из ночи (для юзера)

- ⚠️ **`settings.local.json` хранит реальные ключи** (`nim_key`, `gemini_key`) в открытом виде рядом с репо. Файл в `.gitignore`, но ключи стоит **отозвать и перевыпустить**, а в `settings.local.json` хранить только маски/пустые строки.
- 🗑️ **Мусор ~0.9–1.2 ГБ** — `output/` (923M) и копия ролика (`output/TIKTOK1.mov`, 272M). Я не удалял — реши сам, что из этого нужно.
- 👥 **История коммитов чистая** — только ты (`zxcyuiiop`); "выписывать contributors" не из кого.
- 📄 **`tasks2.txt` на диске не было** — работал по твоему сообщению в чате.
- 🎨 **Большой редизайн осознанно не трогал** (ты просил без редизайна) — только UX-полировка.

## Что дальше (опциональный второй круг, без редизайна)

- Ограничить `max_clips` для длинных видео (chaning per-run cap уже есть).
- «Умный» рекроп по субтитрам: держать говорящего в кадре, а не центр.
- Очередь → история: позволить скачать логи job'а из GUI.
- Docker / start-скрипт для полностью автономного подъёма.

---

## Night update — 2026-08-11

Second over-night pass on top of the GUI redesign. 7 new features,
`run_all_tests.py --short` is green (23/23), all commits on `main`:

- **Clips saved under their highlight title** — `POST /api/shorts/save` now
  accepts `title`; the file lands in `output/saved/` as `<title>.mp4`
  (`7175657`).
- **Title burned into the video** at ~750px from the bottom, tunable via
  `TITLE_ENABLED` / `TITLE_Y_FROM_BOTTOM` / `TITLE_FONT_SIZE` (`19e79aa`).
- **Batch save queue** in the review header → `POST /api/shorts/save_batch`
  walks all remaining cards sequentially (`eed6acf`).
- **Custom PNG watermark with a freeze-frame pause** —
  `POST /api/upload/watermark` + `WATERMARK_*` settings; the clip holds still
  for `WATERMARK_DURATION_SEC` at `WATERMARK_AT_SEC` with the logo overlaid
  (`f10e9e4`).
- **Persistent clip history** — `shorts_generator/history.py` writes
  `output/history.json`; thumbnails in `output/thumbs/`; favorite/delete via
  `/api/history/*`; backfills old saved clips on first read (`2177adf`).
- **Page split** — `/` Generate, `/history` gallery, `/settings` API keys;
  shared chrome in `static/common.js` (`81c1fc7`).
- **Visual redesign** — cobalt-signal theme over all three pages, one token
  set in `static/style.css` (`b7aa452`).

