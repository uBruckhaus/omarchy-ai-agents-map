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

## Final verdict — CLEAN

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
