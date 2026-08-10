# GUI Redesign — Design Concept (2026-08-10)

## Goal
Take the single-page Flask GUI from "serviceable dark form" to Linear/Stripe/Apple caliber: quiet, airy, typographically precise, with a light+dark theme system and 8 original quality-of-life features. Desktop-first at 1440px. UI language stays Russian. No functional regressions; app.js element ids are a contract (test_gui_features.py checks them) and must not be renamed.

## Non-goals
- Mobile adaptation beyond not-breaking (current 640px media queries stay).
- Any backend/pipeline changes (this pass is frontend-only, except a static favicon route is already covered by /static).
- No new dependencies (no npm, no Tailwind — one hand-authored token CSS file + vanilla JS; Inter loaded from Google Fonts with system fallback).

## Design tokens (single source of truth in static/style.css)

Two themes via `data-theme` on <html> (dark default, prefers-color-scheme fallback, localStorage override).

Dark:
- bg #0a0b0d (near-black, no blue tint), bg-raised #101216, surface #16181d, surface-hover #1c1f26
- border rgba(255,255,255,0.07), border-strong rgba(255,255,255,0.14)
- text #e6e8eb, text-muted #9aa3af, text-faint #6b7280
- accent: single indigo #6366f1 (solid fills only — never as a big background gradient; tiny 1px gradient stroke allowed on the primary button), on-accent #fff
- semantic: success #34d399, warning #fbbf24, danger #f87171

Light (true inversion, not a dimmed dark):
- bg #fafbfc, surface #ffffff, border rgba(16,24,40,0.08)
- text #101828, muted #475467
- same accent, adjusted soft tints

Shared:
- radius: 6 / 10 / 14; spacing on a 4px grid
- type: Inter (Google Fonts, display=swap), fallback system-ui; mono: ui-monospace stack
- shadows: dark theme uses 1px borders + very soft shadows; light theme slightly stronger shadows
- motion: 120–180ms ease; everything disabled under prefers-reduced-motion
- custom scrollbar (thin, themed), themed ::selection, inline SVG favicon (static/favicon.svg → kills the 404)

## Screen changes
- Header: text-only wordmark (no 🎬 emoji heading), theme toggle button (sun/moon SVG), settings-save button.
- Form card: single bordered card per section group, uppercase microcopy headers with rule lines, clearer focus rings (2px accent outline), denser but airier grid.
- Queue: rows with status pills; generates込 commander stays primary CTA top-right of row.
- Progress: stage tracker with check icons replaces plain stage text (feature below); bar becomes slimmer with animated stripes while running.
- Review: bigger action buttons + keyboard shortcuts (feature), score chip, subtle saved state.
- Results: hover-lift cards, score chip color by value.
- Toasts: slide-in from right with progress underline.

## Feature brainstorm (15) → selected 8

Candidates:
1. Theme toggle (dark/light, persisted) — SELECTED. Baseline expectation at this design tier.
2. Pipeline stage tracker (icon steps: download → transcribe → analyze → render → done, live from /api/jobs stage string) — SELECTED. Turns a dead "…" progress line into a story.
3. Review keyboard shortcuts (S = сохранить, Delete = удалить, →/Space = далее, shown as kbd hints) — SELECTED. Reviewing N clips is repetitive; power-user path.
4. Result card hover-lift + press-down micro-motion — SELECTED (part of polish, tiny).
5. Toast progress underline — SELECTED (shows lifetime, dismiss on click anywhere on toast).
6. Confetti burst on review completion ("Все клипы сохранены") — SELECTED. Canvas confetti, ~1s, respects prefers-reduced-motion, single accent palette (no rainbow noise).
7. Skeleton shimmer on first load of results/queue while polling hasn't returned — SELECTED small (queue empty state already exists; skeleton reserved for review-body mount).
8. Live "dirty settings" dot on the Сохранить button when any tracked field differs from saved state — SELECTED. Solves a real confusion (user pressed save and nothing visible happened).
9. Inline SVG favicon + themed selection/scrollbar — SELECTED (detail layer).
10. Score heatmap on review video timeline — dropped: needs per-second score data the backend doesn't emit; inventing fake data violates truthfulness.
11. Hover-scrub video preview — dropped (bandwidth + mouse-hover on desktop-only marginal gain over click).
12. Command palette (Ctrl+K) — dropped: pages has ~6 actions; overkill = AI-cliché feature bloat.
13. Drag & drop a video file anywhere onto the page — SELECTED as part of local-file input upgrade? Simpler: style the existing file input as a drop target zone — small, real. → folded into form polish, counts with #4 as "micro-interactions pass".
14. Elapsed time already exists; "ETA" exists in recent pipeline work — keep as-is.
15. Sound on done — dropped (annoying, autoplay-policy fragile).

Selected set (10 counting polish groups):
F1 Theme toggle + auto system preference
F2 Pipeline stage tracker with check marks and current-step pulse
F3 Review keyboard shortcuts with visible kbd hints
F4 Toast progress underline + click-to-dismiss
F5 Confetti on review completion (reduced-motion safe)
F6 Dirty-settings indicator on the save button
F7 Skeleton shimmer while review/results mount + empty-state polish
F8 Micro-interactions pass: hover-lift cards, button press scale, focus-visible rings everywhere, drop-zone-styled file input
F9 Themed scrollbar / selection / favicon (404 fix included)
F10 Score chip color scale (≥85 success tint, <60 muted) — truthful, data already present

## Verification contract
- venv/Scripts/python.exe run_all_tests.py — 16/16 after every change
- node --check static/app.js
- Playwright screenshots (Edge headless, 1440x960) of main/progress/review/results/error in BOTH themes, plus console-error capture (must be NONE)
- Art-director critique pass on screenshots, fix 5 worst, repeat once
- FEATURES.md at repo root; design doc committed; push to origin/main at the end
