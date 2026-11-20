# Claude review request: AI Agents Map Maprisma parity

Review the unpushed GTK port on `task/maprisma-settings-setup-parity` against
base `17c937f` in `/home/uwe/Projects/ai-agents-map-experience-port`.

The task ports Maprisma 079 compact Settings (`bafec95`) and 080 setup recovery
(`ab1a98f`) to AI Agents Map. Keep the native GTK sections, sequential checklist,
full-checklist ready condition, saved answer and confirmed background work.
GTK Add-ons and Setup are its recovery destinations.

This is a read-only code review. Do not edit files, write Git, access credentials
or real conversations, call providers, install software, or use live settings.
Report actionable findings ordered by severity, with file/line, a concrete
trigger and its user-visible consequence. If none remain, say CLEAN and name
any unverified behavior.

Focus on:

- Compact selector/wide scrolling sidebar, global search including full hints,
  selected-section semantics, text scaling and wrapping.
- Focus and query preservation through resize/rebuild, with deferred focus
  callbacks invalidated when the layout is replaced or Settings is left.
- Checked asynchronous reads/writes, timeouts and stale callbacks; retaining
  confirmed choices and failed-save retry intent through Refresh, a later save,
  overlapping verification completions and leaving/reopening the guide.
- Guided Back after installer, connection, model or Add-ons cleanup; explicit
  Map/Setup choices and background completions must preserve the current page.
- Translation completeness and accurate native GTK documentation.

Relevant files: `settings_pages.py`, `first_agent_page.py`,
`first_agent_state.py`, `guide_navigation.py`, and the integration diffs in
`ai_popup.py`, `connect_page.py`, `models_page.py`, `setup_page.py`.

Only related tests are authorized during development; full suites are reserved
for immediately before a push. The final related run passed 89 tests, including
Settings, recovery, navigation, existing first-agent behavior, translations,
manual structure, no-run-on-open and the display-independent Back-key tests.
The three new groups comprise 68 tests. Read
`docs/reviews/maprisma-experience-parity.md` for independent review evidence.

No usable display exists in this sandbox, so do not create GTK widgets or claim
visual, allocation or physical keyboard validation from the headless fixtures.
A normal desktop still needs a narrow/wide window and large-text smoke check.
