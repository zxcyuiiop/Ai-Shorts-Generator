# Features — GUI Redesign (2026-08-10)

A design-system pass over the single-page Flask GUI plus ten original quality-of-life
features. Style reference: Linear / Stripe / Apple — quiet surfaces, one accent color,
Inter typography, no visual noise. All UI microcopy stays Russian; everything below is
frontend-only (`static/`, `templates/`) — the processing pipeline is untouched.

Design tokens and theme values live at the top of `static/style.css`
(`:root` + `[data-theme="light"]`). Design rationale:
`docs/superpowers/specs/2026-08-10-redesign-design.md`.

## Design system

- **Tokens, not ad-hoc styles** — color, radius, spacing (4px grid), shadow and motion
  are CSS custom properties consumed by every component; both themes derive from the
  same token set.
- **Typography** — Inter (Google Fonts, `display=swap`) with a system-ui fallback;
  uppercase microcopy headers with rule lines for section titles.
- **One accent** — indigo `#6366f1`, used for primary actions and focus rings only.
  Success/warning/danger reserved for semantic state (score chips, toasts, errors).
- **Motion discipline** — 120–180 ms eases; every animation is disabled under
  `prefers-reduced-motion: reduce`.
- **Keyboard first** — every interactive element has a visible `:focus-visible` ring;
  the review workflow is fully keyboard-driven (F3).

## F1 — Dark / light theme toggle

- **Idea:** a sun/moon button in the header switches themes instantly; the choice
  persists in `localStorage` and falls back to `prefers-color-scheme`.
- **Why:** the app runs for minutes at a time next to other tools; a forced dark-only
  UI is the single most common complaint for desktop-ish web tools. A true light
  theme (not a dimmed dark) is table stakes at this design tier.
- **How to verify:** click the header toggle → theme flips without reload; reload the
  page → theme is remembered; no flash of the wrong theme on load (inline anti-FOUC
  script in `<head>`).

## F2 — Pipeline stage tracker

- **Idea:** the progress section shows the pipeline as a step list
  (Скачивание → Транскрипция → Анализ → Рендер → Готово) with check marks for done
  stages and a pulse on the current one.
- **Why:** a bare progress bar with "…" for 3+ minutes reads as "is it stuck?".
  Named stages turn waiting into a story and make failures self-explanatory.
- **How to verify:** start a job → the tracker under the progress bar advances stage
  by stage; finished steps get a ✓.

## F3 — Review keyboard shortcuts

- **Idea:** in clip review: `S`/`ы` (layout-proof) save, `Delete` delete,
  `→`/`Space` next clip, `Esc` close. A hint bar with styled `<kbd>` chips sits under
  the action buttons.
- **Why:** reviewing N clips is the most repetitive flow in the app; a mouse-only
  path makes it a chore.
- **How to verify:** open review → press keys; hints are visible under the buttons;
  shortcuts are ignored while typing in inputs.

## F4 — Toasts with countdown bar + click-to-dismiss

- **Idea:** toasts slide in from the right with a thin underline that drains over the
  toast's lifetime; clicking the toast dismisses it immediately.
- **Why:** users couldn't tell whether a toast would vanish or stick; the underline
  answers it wordlessly.
- **How to verify:** trigger any toast (e.g. save settings) → underline shrinks;
  click → toast closes at once.

## F5 — Confetti on review completion

- **Idea:** finishing a review with at least one saved clip fires a ~1 s canvas
  confetti burst in the app's own palette (accent + semantic colors, no rainbow).
- **Why:** "Все клипы обработаны" is the payoff moment of the whole session — it
  should feel like one. Respects `prefers-reduced-motion` (skipped entirely).
- **How to verify:** save at least one clip in review → completion screen shows a
  brief confetti burst; with reduced motion enabled, nothing animates.

## F6 — Unsaved-settings dot

- **Idea:** when any tracked settings field differs from the last saved state, the
  "Сохранить" button shows a small accent dot; it clears on successful save.
- **Why:** the real bug behind "нажал сохранить и ничего не произошло" was zero
  feedback — the dot + existing toast close the loop.
- **How to verify:** change any setting → dot appears; save → dot disappears.

## F7 — Review loading skeletons

- **Idea:** while the review clip list is being fetched, three shimmering skeleton
  cards stand in for content.
- **Why:** an empty white gap during fetch reads as broken; a skeleton reads as
  "coming up".
- **How to verify:** throttle network (DevTools) and open review → skeletons shimmer
  before cards render.

## F8 — Animated running progress bar

- **Idea:** while a job runs (<100%), the progress bar shows slim diagonal stripes
  drifting forward; at 100% it goes solid.
- **Why:** motion = liveness. A static 55% bar and a stuck 55% bar look identical;
  stripes make "working" visible at a glance.
- **How to verify:** start a job → stripes drift during active stages, stop at 100%.

## F9 — Themed scrollbar, selection, favicon

- **Idea:** thin themed scrollbar, accent-tinted `::selection`, and an inline SVG
  favicon (also kills the recurring `/favicon.ico` 404 in logs).
- **Why:** the detail layer — the place where "hand-made" and "template" apps differ.
- **How to verify:** select text (accent tint), scroll a long page (thin dark/thumb
  scrollbar), check the tab icon and the absence of favicon 404s in server logs.

## F10 — Virality score color chips

- **Idea:** score badges in results/review are tinted by value: ≥85 success green,
  60–84 amber, <60 muted red.
- **Why:** triage by color is instant; raw numbers forced reading every card.
  Uses data the backend already returns — no invented metrics.
- **How to verify:** results screen → cards show green/amber/red chips by score.

---

# Night update — 2026-08-11

Second over-night pass on top of the GUI redesign: page split, persistent history,
save-time highlights, a real PNG watermark with freeze-frame, and a cohesive
cobalt visual theme. All tests green (23/23 via `run_all_tests.py --short`).

## N1 — Clips saved under their highlight title

- **Idea:** when a draft is approved it can carry the LLM highlight title; the
  saved file in `output/saved/` is named by that title (sanitized via
  `_safe_title_name`), and `GET /api/jobs/<id>/shorts` surfaces `title` per clip.
- **Why:** «clip_07.mp4» was useless for later triage; a human-readable title
  makes the saved folder self-describing.
- **How to verify:** in review, press «Сохранить» on a card — the file lands in
  `output/saved/` as `<title>.mp4` (a repeat title gets `_2`, `_3`, …).

## N2 — Highlight title burned into the video

- **Idea:** the same title is drawn with a `drawtext` pass ~750 px above the
  bottom of the final 1080×1920 frame, after captions and before the overlay.
  Tunable: `TITLE_ENABLED`, `TITLE_Y_FROM_BOTTOM`, `TITLE_FONT_SIZE`.
- **Why:** the first seconds of a Short need a hook; the title is the hook
  the model already picked.
- **How to verify:** save any titled clip → the title text is visible near the
  bottom; «Settings» / `settings.local.json` can move it up/down or disable it.

## N3 — Batch save queue in the review panel

- **Idea:** a «Сохранить всё оставшееся» button in the review header queues all
  remaining cards through the new `POST /api/shorts/save_batch` endpoint — one
  request, sequential saves, per-item `{ok, error?}` results.
- **Why:** saving 10+ drafts one click at a time was the last mouse-heavy step
  in the whole flow.
- **How to verify:** generate ≥2 clips → in review press the batch button →
  cards mark themselves «Сохранено» without per-card clicks.

## N4 — Custom watermark with a freeze-frame pause

- **Idea:** any PNG/JPEG logo can be burned in via `POST /api/upload/watermark`
  (persisted as `WATERMARK_FILE`). During finalize the clip freezes for
  `WATERMARK_DURATION_SEC` at `WATERMARK_AT_SEC`, showing the logo at
  `WATERMARK_SCALE`% of frame width — the classic TikTok outro beat.
- **Why:** first-class branding without editing the video afterwards; the
  freeze makes even a tiny logo readable.
- **How to verify:** upload a logo on the Settings page → save a clip → at
  the configured second the picture holds and the logo overlays it; duration
  grows by the pause.

## N5 — Persistent clip history

- **Idea:** every approved clip is recorded in `output/history.json`
  (`shorts_generator/history.py`) with title, source title, score, duration,
  aspect and a JPEG thumbnail in `output/thumbs/`; endpoints: `GET /api/history`,
  favorite + delete. Survives server restarts and re-scans `output/saved/` for
  untracked files.
- **Why:** «what did I already keep?» had no answer across sessions.
- **How to verify:** save a clip → open `/history` → the clip appears with a
  thumbnail; star toggles, delete removes the entry and the file.

## N6 — Generate / History / Settings pages

- **Idea:** the single page split into three routes — `/` (generate + review),
  `/history` (searchable gallery with favorites), `/settings` (API keys and
  effect knobs). Shared chrome (theme toggle, toasts) lives in `common.js`.
- **Why:** the monolith page had outgrown one screen; settings and history
  were buried layers deep.
- **How to verify:** the header nav shows three tabs; each route loads
  standalone with working theme/toasts.

## N7 — Cohesive visual redesign (cobalt-signal)

- **Idea:** all three pages share one token set (deep cobalt accent, wide
  layout, calmer card surfaces); review cards, history grid and settings form
  were rebuilt on it. Russian microcopy unchanged.
- **Why:** the F1–F10 polish layered on the old base had started to drift;
  a single systemic pass makes the app look like one product.
- **How to verify:** open any page — consistent accent/typography; dark/light
  toggle works everywhere.

