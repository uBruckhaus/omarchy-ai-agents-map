# Maprisma Settings and setup parity — GTK review

Reviewer: independent Codex agent, 2026-11-20. Target: the working tree on
`task/maprisma-settings-setup-parity`, based on AI Agents Map `17c937f`.
Scope: native GTK equivalents of Maprisma 079 through `bafec95` and 080 through
`ab1a98f`, plus their integration with native page navigation and persistence.

The reviewer made no application-source changes. The archived
`/mnt/Data/Projects/ai-agents-map` source was not edited. This verdict does not
represent an external Claude or Antigravity review. Antigravity's port-review
attempt was blocked by local socket `EPERM`. The bounded, no-tools Claude CLI
port-review attempt timed out after 120 seconds (exit 124), with no response or
verdict. Neither is credited with CLEAN here.

## Native architecture inventory

- The original `SettingsPages.save_setting()` swallowed write errors. Guide
  changes now use checked `ai_core.update_settings()` writes and retain failed
  changes for explicit retry.
- GTK already stores the test answer. Its answer display, sequential checklist,
  reviewed actions, signed add-on activation, and durable jobs remain native.
- Native `self.view` names only Map/Setup; guided page origin therefore needs
  separate metadata. Explicit close handlers consume that metadata after their
  normal cleanup. Generic navigation must not automatically reopen the guide.
- Presentation reads and deferred callbacks need invalidation when leaving,
  hiding, rebuilding, or reopening. Confirmed writes and jobs must continue.

## Round 1 — independent source findings

Line references identify the corresponding locations in the final working tree.

1. **Medium** `first_agent_page.py:619`, checklist details and waiting state.
   The new generic manual-setup hint replaced the existing step details. It
   also described an asynchronous model-header read as manual work because
   that pending step had no action.
   > Author: Append the hint to retained manual instructions. Pending model
   > reads carry their existing waiting explanation and receive no manual-work
   > hint. Added fake-row rendering regressions for both cases.
2. **Medium** `first_agent_page.py:139`, queued verification persistence.
   Two confirmed background answers could compute verification from the same
   cached state. The queued second save then replaced the first agent's newly
   persisted membership.
   > Author: Merge verification additions against fresh persisted membership
   > inside the serialized writer. Added two-confirmed-answer coverage.
3. **Medium** `first_agent_page.py:38`, canceled model-fit cache.
   Removing a pending fit timer without removing its cached `None` sentinel
   prevented the same model from being read again after cancellation.
   > Author: Remove pending sentinels when abandoning their presentation read.
   > A regression restarts the same model and rejects the old worker's result.
4. **Medium** `first_agent_page.py:562`, late context completion.
   The non-backup context worker unconditionally refreshed the guide after
   completion, including after leaving or reopening another presentation.
   > Author: Guard the presentation refresh with its generation and active
   > page, preserving the confirmed operation and result feedback. Added a
   > completion-after-reopen regression.
5. **Medium** `first_agent_page.py:139`, unresolved save failures.
   Preserving only failed route selections let a failed verification or Later
   save lose its error and retry when an unrelated queued save succeeded.
   Multiple failed answers could also overwrite each other's retry state.
   > Author: Preserve unresolved changes, merge verification additions, and
   > retain explicit retry. A newer answer intentionally supersedes the old
   > answer text. Regressions cover failed answer then successful answer,
   > failed Later then background progress, and two failed answers retried
   > together. Helpers are in `first_agent_state.py:17` and
   > `first_agent_state.py:25`.
   > A final follow-up also retains failed changes while a concurrent check
   > temporarily replaces the visible error, and lets a read confirm additive
   > verification membership without requiring exact list equality.
6. **Medium** `settings_pages.py:539`, deferred focus restoration.
   Idle callbacks queued before leaving or rebuilding Settings could apply
   old focus signatures to a later page or widget tree. Synchronous idle
   doubles did not exercise this ordering.
   > Author: Capture the layout and revision; require the same mapped layout
   > and active Settings page before restoring focus. Unmapping invalidates
   > queued work even when the same tree returns. Added four deferred-idle
   > regressions covering page changes, rebuilds, unmap/remap and hidden trees.

## Round 2 — integration findings and author audit

The reviewer verified the following integration corrections, including issues
identified independently by the author.

1. **Medium** `ai_popup.py:2957`, explicit header versus internal navigation.
   The author's audit found that clearing provenance in generic `show_view()`
   broke internal returns through Map, while a Map-header click could call a
   guided close handler before clearing provenance and reopen the guide.
   > Author: Clear provenance at explicit Map/Setup entry points, before their
   > branches; leave generic internal returns intact. Handler-level fixtures
   > distinguish header navigation from guided Back and native Setup recovery.
2. **Medium** `first_agent_page.py:394`, delayed Later navigation.
   The author's audit identified a late successful Later save closing a fresh
   guide visit after the original visit had been left or hidden.
   > Author: Bind navigation callbacks to a presentation visit; keep confirmed
   > progress writes intact. Explicit retry can bind its navigation to the
   > current visit. Regressions cover leave/reopen, hide, retry and refresh.
3. **Medium** `ai_popup.py:3732` and `connect_page.py:1258`, background completion.
   Installer completion, or a connection completed after background unpacking,
   could consume guided provenance and replace foreground Settings/Help.
   > Author: Clean finished page state without navigating when its form is
   > not foreground. `guide_navigation.py:36` defers the appropriate return
   > until Back. Unpack progress and completion also check form identity before
   > changing widgets. Added foreground-preservation and replacement-form
   > regressions. Explicit close still preserves the existing special
   > security, skill-update and Smart Check return paths.

## Round 2 verdict — CLEAN (Codex source review)

**CLEAN — independent Codex source review.** All reported findings are
addressed in the reviewed working tree. The port keeps native sections and
actions, checked persistence, explicit retry, bounded presentation waits,
complete-checklist readiness, and page-origin-aware returns. No additional
actionable source defects were identified.

The reviewer independently ran only these three related test groups:

```text
PYTHONPATH=tests python3 -m unittest -v \
  test_settings_responsive test_first_agent_recovery test_guide_navigation
```

**68 tests pass:** 19 Settings, 31 guide recovery, and 18 navigation tests.
Output: `/tmp/ai-agents-map-port-independent-final.log`. Earlier reviewer runs
passed 55 and 66 tests before the final regressions were added. These runs
overlap and are not additive. `git diff --check` also passed.

## Verification limits

Tests exercise the actual handlers using GTK-free doubles, queued callbacks,
temporary data and mocked writes. The reviewer also inspected installed GTK
introspection metadata without constructing widgets; the used ComboRow
factories, ActionRow line setters and widget bounds APIs are available.

There is no usable desktop display in this sandbox. GDK display lookup fails,
and a private Broadway backend cannot bind its socket (`EPERM`); Xvfb is not
available. This review therefore does **not** establish actual GTK allocation
at compact sizes, rendered translations or themes, or physical keyboard and
screen-reader behavior. Those remain display-dependent verification items,
separate from the CLEAN source verdict.

No full suite, live settings/provider operation, credential read, push,
release, signing, marketplace action or version bump was performed by the
reviewer. Full suites remain reserved for before a push, per Uwe's instruction.

## Author validation and delivery

After the final fixes, the author ran the related groups together with existing
first-agent, i18n, manual-translation, no-run-on-open, window-name and three
headless Back-key checks: **89 tests passed**. This includes the same 68 tests
independently run above, not 89 additional tests. Modified Python files compile,
all manual translation stamps are current, dynamic error messages have all
seven translations, and whitespace checks pass. Shipped manual PDFs are not
rebuilt as part of this source port.

The review request for a later Claude run is saved in
`docs/reviews/claude-review-prompt.md`. Integration is supplied as a local commit
and patch because the canonical source is read-only in this session; no live
installation is updated.

## Round 3 — independent Claude + Antigravity review and fixes

On 2026-11-20, both external reviews of `2681d46930298263d3654e2e68570b6f492ea591`
against base `17c937f` completed outside the sandbox and returned **NOT CLEAN**.
These completed reviews supersede the unsuccessful external attempts described
above; the earlier CLEAN verdict applies only to the Codex source-review round.
The independent reports are saved at:

- `/home/uwe/ai-agents-map-reviews/2681d46/claude-terminal-result.txt`
- `/home/uwe/ai-agents-map-reviews/2681d46/agy-terminal-result.txt`

Claude reported M1, M2 and L1–L4, and identified S2/S4 as integration concerns.
Antigravity independently confirmed those eight issues and reported A1–A4.
All 12 verified findings are fixed in this author fix round. No new independent
CLEAN verdict is claimed for the fixes.

Test names below are in `tests/test_first_agent_recovery.py` for M/L findings,
`tests/test_guide_navigation.py` for S findings, and
`tests/test_settings_responsive.py` for A findings.

| Finding | Fix | Focused regression tests |
| --- | --- | --- |
| M1 — fixed | Remember canceled/skipped reads, restart after the save queue drains even with an existing snapshot, and clear the request on leave or actual read start. | `test_background_save_restarts_cancelled_check_with_existing_snapshot`; `test_pending_write_restarts_skipped_check_after_queue_drains`; `test_leaving_clears_skipped_read_request` |
| M2 — fixed | Keep wrapping Add-ons/Setup navigation usable during a blocked writer; hide Try again until the writer exits. | `test_timed_out_save_keeps_recovery_navigation_and_hides_retry_until_writer_exits` |
| L1 — fixed | Invoke identical previous/current save callbacks only once on retry. | `test_four_failed_verification_retries_call_saved_once_on_success` |
| L2 — fixed | Rendering leaves error state intact, emits one checklist READ_ERROR, and binds its retry to a fresh check. | `test_invalid_checklist_render_preserves_errors_and_refreshes_only_once` |
| L3 — fixed | Preserve the unsaved choice and corresponding save error after a failed read or model-fit timeout. | `test_failed_route_save_keeps_choice_error_after_read_failure`; `test_failed_route_save_keeps_choice_error_after_fit_timeout` |
| L4 — fixed | Give the overview card's Later save an error callback; show translated error feedback on failure/timeout and keep the card visible. | `test_overview_later_failure_or_timeout_reports_error_and_keeps_card` |
| S2 — fixed | Discard guided handoffs before hide closes connect/install forms, preventing a throwaway guide resume/read. | `test_hiding_guided_forms_cleans_up_without_resuming_guide` |
| S4 — fixed | Header Map closes connections with `navigate=False`, then opens Map directly, bypassing security/skill-update/Smart Check returns. | `test_map_header_cleans_special_connection_without_special_returns` |
| A1 — fixed | Ignore unallocated widths; default missing/nonpositive fonts to 11pt and convert absolute pixel sizes to points. | `test_unallocated_tick_keeps_wide_layout_and_geometry`; `test_tick_normalizes_absolute_and_unset_font_sizes` |
| A2 — fixed | Unescape group titles/descriptions before searching, so literal apostrophes/ampersands match and entity names do not. | `test_search_unescapes_group_titles_and_descriptions` |
| A3 — fixed | Preserve the known compact/wide state during rebuild and apply visibility before restoring navigation focus. | `test_compact_rebuild_restores_selector_focus_before_any_tick` |
| A4 — fixed | Capture layout/revision for queued scroll callbacks and require the same mapped layout, revision, and active Settings page. | `test_queued_scroll_rejects_left_unmapped_rebuilt_or_remapped_settings` |

All **16 new regression tests fail by assertion against the original `2681d46`
source**, loaded in memory without replacing working-tree files, and pass with
the fixes. This includes both M1 paths, failure and timeout of overview Later,
both L3 paths, escaped headings and descriptions, all three special connection
returns, and leaving/unmapping/rebuilding/remapping before queued scroll work.

Author validation: **106 related tests passed**, including 84 in the three
changed groups (24 Settings, 40 guide recovery, 20 navigation). The remaining
22 cover first-agent behavior, i18n, manual structure/PDF presence,
no-run-on-open, window method names, and the three display-independent Back-key
checks. The exact related-only command was:

```sh
PYTHONPATH=tests python3 -m unittest -v \
  test_settings_responsive test_first_agent_recovery test_guide_navigation \
  test_first_agent test_i18n test_manual test_manual_translations \
  test_no_run_on_open test_window_names \
  test_back_keys.BackKeysTest.test_every_back_page_goes_back_with_both_keys \
  test_back_keys.BackKeysTest.test_keys_stay_in_a_field_being_edited \
  test_back_keys.BackKeysTest.test_terminal_keeps_its_keys
```

`python3 docs/manual/translations.py check`, `py_compile` on all seven changed
Python files, and `git diff --check` passed. All three reused dynamic error
messages retain all seven translations; no new UI strings were introduced.
The test output is `/tmp/maprisma-parity-related.log`. Display-dependent GTK
allocation, rendering and physical keyboard behavior remain unverified here.
No full suite, push, `/mnt/Data` write, live settings change, or version bump
was performed.

Delivery note: committed onto `task/maprisma-settings-setup-parity` as `821f1cb`.

## Round 4 — independent Claude + Antigravity re-review of `821f1cb` and fixes

On 2026-11-20, independent Claude Round 2 review (`/home/uwe/ai-agents-map-reviews/821f1cb/claude-result.txt`)
and Antigravity Round 2 inspection confirmed ten of the twelve Round 3 fixes and
identified three remaining edge cases:

1. **Low/Medium — L1 Later-retry callback doubling** (`first_agent_page.py`):
   When a verification save failed and a subsequent Later save also failed,
   retrying via the Later branch wrapped `original` in a new `saved` closure
   while `previous_callback` still referenced `original`, bypassing the identity
   deduplication check.
   > Fix: assign `self._first_retry_callback = saved` before `self.first_save(...)`
   > in the Later retry branch. Added `test_later_retry_after_failed_verification_calls_saved_once_on_success`.
2. **Low — L3 suppressed read/fit failure never re-checked after save retry** (`first_agent_page.py`):
   When a read failure/timeout or model-fit timeout kept the save error because
   `retry_changes` was set, a subsequent successful save retry rendered the old
   snapshot (or unknown fit) without restarting `first_look()`.
   > Fix: set `_first_read_wanted = True` when a read failure/timeout or model-fit
   > timeout defers to `retry_changes`, and clear `first_fits` when restarting a
   > wanted read after the save succeeds. Extended both L3 regression tests.
3. **Low — `hide_window()` discarded active `models`/`addons` guided handoffs** (`ai_popup.py`):
   Calling `guide_navigation.abandon_handoffs(self)` unconditionally during
   `hide_window()` dropped `models_state["origin_page"]` and `return_page` even
   when hiding from `models` or `addons`, which remain open on reopen.
   > Fix: suppress guide resume only for the page `hide_window()` is actually
   > closing (`settings`, `connect`, `install`), preserving `models`/`addons`
   > guided Back across hide/reopen. Added `test_hiding_models_or_addons_keeps_guided_return_while_settings_skips_resume`.

## Round 5 — final verification and TRIPLE CLEAN (`e9b89dd`)

- **Codex Round 3 (`2f447f4`)**: **CLEAN** (`/home/uwe/ai-agents-map-reviews/2f447f4/codex-result.txt`).
- **Claude Round 3 (`2f447f4`)**: verified Fixes 2 and 3; flagged one Low finding (linear `leave()` closure stacking across repeated failed Later retries).
  > Fix (`30fc44f`): track `self._first_later_leave` separately from `_first_retry_callback` so repeated Later retries keep non-leave callbacks flat and attach one fresh current-visit `leave()`.
- **Codex Round 4 (`30fc44f`)**: **CLEAN** (`/home/uwe/ai-agents-map-reviews/30fc44f/codex-result.txt`).
- **Claude Round 4 (`30fc44f`)**: flagged one Low/Medium edge case (failed `first_choose(route)` followed by failed `first_later()` stayed on `firstagent` on retry because `first_retry()` excluded `"route"` from the Later branch) plus a fresh-leave test assertion nit.
  > Fix (`e9b89dd`): trigger the Later retry branch whenever `state.retry_changes.get("later") is True` (`first_choose` always sets `later=False`); added `test_failed_route_then_failed_later_retry_still_leaves_guide` and pinned `len(leaves) == 5` and `leaves[-1].call_count == 1`.
- **Claude Round 5 (`e9b89dd`)**: **CLEAN** (`/home/uwe/ai-agents-map-reviews/e9b89dd/claude-result.txt`).
- **Codex Round 5 (`e9b89dd`)**: **CLEAN** (`/home/uwe/ai-agents-map-reviews/e9b89dd/codex-result.txt`).
- **Antigravity (`e9b89dd`)**: **CLEAN** — **109 related tests passed** (24 Settings, 42 guide recovery, 21 navigation, plus 22 existing first-agent/i18n/manual/no-run-on-open/window-name/Back-key checks), `py_compile`, `docs/manual/translations.py check`, and `git diff --check` all pass.

Display-dependent GTK allocation, rendering, and physical keyboard checks remain desktop smoke-test items. No full suite was run and nothing was pushed.
