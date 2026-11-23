# Plan: simplify the scheduling plumbing (2.1.5), then Codex round 6 and the [Verify]

Status 2026-11-23: prepared, not started (Uwe: "prepare for later run"). Released state: **2.1.4**,
`0c48fa85521b041a5e37dd4026158f05cf51f97d`, pushed. Marketplace: Verify omacom/omarchy-plugin-marketplace#14694
still targets `17c937f16a5b188591896986e8ee39e79f135d89` (1.0.2); not retargeted.

Uwe's decision: **simplify the plumbing** (restructure instead of patching the next edge case), release
2.1.5, **one** more Codex round (round 6), and retarget #14694 **only if it is CLEAN**.

## Why
Codex rounds 1–5 (2.1.0 → 2.1.4) found a Medium every round, always the next edge case of the same piece:
the bookkeeping of the schedule's systemd user units (`schedule_units.py`) and the request queue
(`schedule_handover.py`, `project_schedule.py`). No injection or privilege problem; add-on data cannot pick
the project command (confirmed in round 5).

## Round 5 findings at 2.1.4 (all three must be gone)
1. `project_schedule.py:374` `clear()`: when clearing a stored schedule fails, it leaves through `finally`
   without `remove_all()` → enabled timers can stay.
2. `schedule_units.py:197` `sync()`: a failed `enable --now` / `restart` leaves the unit files; the next sync
   sees them unchanged (not in `written`) and never retries activation.
3. `project_center.py:365`, `schedule_handover.py:353`: a timer job opens the store, which syncs every
   changed unit (30 s per slow `systemctl`), before serving its own request → can exceed the request's
   3-minute age limit and drop its own occurrence as stale (`TimeoutStartSec=infinity` gives no deadline).

## The simplification (one design, not three patches)
- **Serve first, reconcile after.** A timer job claims and starts its own occurrence before any unit
  reconciliation; reconciliation runs afterwards (or in the app), with its own overall deadline that can
  never expire an occurrence. The age check applies to when the request was *claimed*, not to when the job
  got around to it.
- **Desired state, not diffs.** `sync()` computes the wanted set of timers from the store and makes reality
  match it every time: write files that differ, and for every wanted timer verify `is-enabled` + `is-active`
  (a failed query counts as "unknown", never "fine") and (re)enable/start it if not confirmed; for every
  unwanted managed unit disable + remove, verifying afterwards. No "only what changed" shortcut, so a failed
  activation is retried on the next sync by construction.
- **Clear = best effort over everything, honest exit.** `clear` always attempts both: clearing the stored
  schedules and removing every managed unit; collects every failure; exits non-zero if any step failed or a
  managed unit is still present afterwards (keep 2.1.4's rescan).
- Keep everything already hardened: managed marker, no links, safe ExecStart, the private queue folder
  (`$XDG_RUNTIME_DIR`, else `/tmp/ai-agents-map-<uid>` 0700), atomic request files, claim by rename,
  streaming `os.scandir` without caps.

## Tests (each must fail on 2.1.4)
- store update fails during `clear` → units still removed, both failures reported, exit 1;
- `enable --now` fails once → the next `sync()` retries and the timer ends up enabled+active; an
  `is-active`/`is-enabled` query failure is never taken as success;
- slow fake `systemctl` (sleep) with many schedules → the timer job's own occurrence still runs (served before
  reconciliation) and is never dropped as stale;
- the existing 49+ schedule tests and the full suite stay green (known failures on main: 4 failures + 35
  errors in test_provider_config / test_agent_details / test_desktop).

## Then
1. Version 2.1.5 in `manifest.json`; re-render `preview.png` with the 2.1.5 pill (page + crops are kept in
   `/mnt/Data/Projects/aam-listing-2.1/`, see below); rebuild `assets/user-manual.pdf`
   (`python3 docs/manual/build.py`); README/ARCHITECTURE if the flow description changes.
2. `omarchy plugin validate .`, preflight, the marketplace validator; commit + push.
3. **Codex round 6** (explicitly approved): baseline `17c937f…`, target the new HEAD, the update notes in the
   prompt, focus on the scheduling plumbing. CLEAN → update the notes (6 rounds) and retarget #14694:
   replace the target SHA in the issue body, then post the notes as a comment (forms.md). Not CLEAN → stop
   and ask Uwe (simplify further, ship without scheduling, or hold).

## Material
- Draft update notes (for 2.1.4; update for 2.1.5): `docs/reviews/2.1-update-notes-draft.md`.
- Description (470 chars, passes the marketplace check) is already in `manifest.json`.
- Preview: `preview.png` (2.1.4). Its sources are outside the repo in `/mnt/Data/Projects/aam-listing-2.1/`
  (`preview.html` + crops `p1-map.png`, `p2-board.png`, `p3-schedule.png`; change the version pill in
  `preview.html` and render at 2× with headless Chromium as `references/preview.md` says), with the demo
  project script `make_demo.py` and the round-5 Codex output `codex-round5.txt`.
- Skill: `~/.claude/skills/omarchy-plugin-publishing` (Codex recheck rules, forms).
