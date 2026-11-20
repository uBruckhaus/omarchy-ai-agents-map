# Maprisma Settings and setup parity for AI Agents Map

## Scope and source

Port the user-facing improvements from Maprisma tasks 079 and 080 to the active
GTK AI Agents Map app, as requested by Uwe on 2026-11-20. Source references:
Maprisma task 079 through `bafec95`, task 080 through `ab1a98f`, including the
subsequent Claude review fixes. Do not copy Flutter widgets or the Dart core
into GTK.

Target base: `17c937f` on the active plugin's `maprisma-parity` branch. Work is
isolated on `task/maprisma-settings-setup-parity` in
`/home/uwe/Projects/ai-agents-map-experience-port`. The archived
`/mnt/Data/Projects/ai-agents-map` source is not edited.

## Behavior

- Responsive Settings: compact section selection when width or text size
  requires it, independently scrollable wide navigation and content, global
  cross-section search, no selected section while searching, and preserved
  section/query/focus through resizing or rebuilds. Existing GTK sections and
  preference actions remain native.
- Setup: distinct progress, failed read/save with explicit retry, empty routes,
  unavailable saved choice, and guidance for the first incomplete prerequisite.
  Keep the confirmed choice/checklist when checking or saving fails. Bound
  presentation reads, discard stale replies after leaving or replacement,
  and suppress duplicate writes.
- Preserve GTK's existing sequential actions, saved test answer, signed add-on
  activation and complete-checklist readiness. Setup and Add-ons are the native
  recovery destinations; this app has no Flutter-style Find existing AI page.
- Return from guided installer, connection, model and Add-ons pages after
  their normal cleanup. Explicit Map/Setup navigation stays at its destination.
  Leaving or hiding the guide cancels presentation work without stopping
  confirmed operations or durable jobs.
- Update all eight UI languages and manual source editions.

## Validation and delivery

Run only tests/checks related to these changes. Full suites are reserved for
before a push, following Uwe's instruction. Use temporary fixtures, never real
credentials, live settings, providers, installers or agent conversations.

GTK/PyGObject is installed, but this sandbox cannot connect to a desktop
display. A private Broadway backend also fails with socket `EPERM`; no Xvfb
is available. GTK-free fixtures can verify actual state and navigation handlers,
but do not establish pixel layout or physical keyboard behavior. Record this
limit and the independent review results.

No push, release, marketplace submission, plugin signing, or version bump is
part of this port. The source repository under `/mnt/Data` is read-only to this
session. Preserve a local commit and an applicable patch for integration.

## Final checks

The final focused run passed 89 tests: the 68 new Settings/recovery/navigation
checks plus existing first-agent, i18n, manual structure, no-run-on-open,
window-name and three display-independent Back-key checks. Modified Python
files compile, manual translation stamps are current for all seven translated
editions, dynamic error messages are translated, and `git diff --check` is clean.
No full suite was run and nothing was pushed.

Independent Codex review found and verified fixes for stale focus callbacks,
pending model-fit reads, manual-step detail, concurrent verification saves,
retry intent across unrelated saves, context completion after leaving, and
background installation/connection navigation. See the review record. Agy's
CLI review could not start because the sandbox denies its local TCP listener.
The no-tools Claude CLI review timed out after 120 seconds without a verdict;
no CLI result is inferred from the independent Codex review.

The port is delivered as an isolated local commit and a patch checked against
the read-only source base. Neither the canonical `/mnt/Data` checkout nor a
live installation is changed. Desktop visual/physical-keyboard checks and
regenerating shipped manual PDFs remain integration/release checks.
