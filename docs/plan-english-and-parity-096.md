# AI Agents Map — English-Only & Maprisma Parity Plan (through Tasks 094–096)

**Baseline verified (read-only):**
- Plugin `main` at `db7277b` (`1.0.2` + Experience Parity port): already synced with Maprisma tasks **004–068** (via `1.0.0–1.0.2`) plus **079** (responsive Settings) and **080** (first-agent recovery, commit `9d5f9da`).
- Maprisma sources inspected: `/mnt/Data/Projects/maprisma` (`main` @ `44de1d3b`, through task **094**), `/home/uwe/Projects/maprisma-095` (`task/095-board-order` @ `04255cf7`), and `/home/uwe/Projects/maprisma-096` (`task/096-running-prompts` @ `a052c155`).

---

## A. English-Only Conversion (GUI, Manuals, Settings Hint)

### 1. Inventory & File-by-File Changes

| Area | Current files | Action |
|---|---|---|
| Python UI translations | `translations/{de,es,fr,it,nl,pl,pt}.json` | **Delete** entire `translations/` directory. |
| Python i18n module | [i18n.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/i18n.py) | **Keep as English-only shim** (avoid touching ~40 importers & [tests/test_llm_engine.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_llm_engine.py#L34)): `LANGUAGES = [("en", "English")]`, `CODES = {"en"}`, `STRINGS = {}`, `current() -> "en"`, `system_language() -> "en"`, `language_setting() -> "en"`, `_(t) -> t`, `N_(t) -> t`, `ngettext(o, other, n) -> o if n == 1 else other`, `save_language()` no-op. Remove `_load_tables()`. |
| Bar widget QML | [I18n.js](file:///mnt/Data/Projects/omarchy-ai-agents-map/I18n.js), [BarWidget.qml](file:///mnt/Data/Projects/omarchy-ai-agents-map/BarWidget.qml) | **Delete** `I18n.js`. In `BarWidget.qml`, remove `import "I18n.js"`, `languageSetting`/`systemLang`/`lang`/`t()`, `parseSettings()`, `FileView`, and the 15 s `Timer`; set `tooltipText: "AI Agents Map"` directly. |
| Settings hint (exact location) | [settings_pages.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/settings_pages.py#L896-L905), [settings_pages.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/settings_pages.py#L1050-L1061), [ai_popup.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/ai_popup.py#L4601) | In `SettingsPages.fill_general_settings()` (inside the **General** group, lines 899–904), **replace** the `Language` `combo_row` with a read-only `Adw.ActionRow` (title `"Language and manual"`, subtitle `"English only — install Maprisma for multi-language support."`). **Delete** `set_language()` (lines 1050–1061) and update the stale restart comment in `ai_popup.py:4601`. |
| Settings schema | [settings_schema.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/settings_schema.py#L115) | **Keep `"language"` as a tolerated legacy key** (never written, never rejected) so upgrades with existing `"language": "de"` in `~/.config/omarchy/ai-agents-map/settings.json` stay clean. |
| Help manual opener | [settings_pages.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/settings_pages.py#L1453-L1486) | Simplify `manual_path()` to only look up `user-manual.pdf`. Optionally append `"Install Maprisma for multi-language manuals."` in `help_manual_card()` intro. |
| Manual HTML & builder | `docs/manual/manual.{de,es,fr,it,nl,pl,pt}.html`, `docs/manual/translations.py`, [docs/manual/build.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/docs/manual/build.py) | **Delete** the 7 translated HTML files and `docs/manual/translations.py`. Keep [docs/manual/manual.html](file:///mnt/Data/Projects/omarchy-ai-agents-map/docs/manual/manual.html). Simplify `docs/manual/build.py` to `LANGUAGES = ("en",)` and remove `WORDS[non-en]` + `import translations`. |
| Shipped manual PDFs | `assets/user-manual.{de,es,fr,it,nl,pl,pt}.pdf`, [assets/user-manual.pdf](file:///mnt/Data/Projects/omarchy-ai-agents-map/assets/user-manual.pdf) | **Delete** the 7 non-English PDFs (~15 MB saved). Keep & rebuild only `assets/user-manual.pdf`. |
| Add-on loader | [addons.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/addons.py#L65), `addons/*` | **Do not modify** bundled `addons/` files (preserves `MANIFEST.sha256` and `SIGNATURE`). Keep optional `translations/<lang>.json` tolerance in `addons.py` as harmless no-op. |
| Marketplace & docs metadata | [manifest.json](file:///mnt/Data/Projects/omarchy-ai-agents-map/manifest.json#L8), [README.md](file:///mnt/Data/Projects/omarchy-ai-agents-map/README.md#L1057-L1066) | Remove `"8 languages."` from `manifest.json` `description`; update `README.md` file table (drop `I18n.js` and `user-manual*.pdf` 8-language note, state English-only + Maprisma for multi-language). |
| Tests | [tests/test_i18n.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_i18n.py), [tests/test_manual.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_manual.py), [tests/test_manual_translations.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_manual_translations.py), [tests/test_language_history.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_language_history.py), [tests/test_settings_responsive.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_settings_responsive.py#L199) | **Delete** `test_manual_translations.py`. Update `test_i18n.py` to test English passthrough & `current() == "en"`. Set `LANGUAGES = ("en",)` in `test_manual.py`. Rewrite `test_language_history.py` to test `ai_core.update_settings` history preservation directly without `save_language('de')`. Update `test_settings_responsive.py:199,437` row title `"Language"` → `"Language and manual"`. Keep `test_settings_schema.py` & `test_existing_ai.py` passing via tolerated `"language"` key. |

### 2. English-Only Risks
- **Tests:** `test_i18n.py`, `test_manual.py`, `test_manual_translations.py`, `test_language_history.py`, and `test_settings_responsive.py` fail unless updated together.
- **Upgrade compatibility:** Existing `~/.config/omarchy/ai-agents-map/settings.json` with `"language": "de"` must remain readable without schema errors.
- **Quickshell bar widget:** Deleting `I18n.js` without updating `BarWidget.qml` breaks the Omarchy top-bar icon at load time.
- **Marketplace static scan:** Rebuilt `assets/user-manual.pdf` must remain non-executable (`0644`), `> 512 KiB`, and contain a `\0` byte in the first 4 KiB (checked by `build.py:300-303` and `test_manual.py`).

---

## B. Maprisma Parity Gap (Tasks 069–096) & Parallel Work Packages

### Tasks Marked N/A or Already Done
- **079, 080:** Already ported (`9d5f9da`..`db7277b`).
- **071, 073:** Maprisma standalone packaging/AppImage/Hyprland runner sizing — **N/A** (Omarchy plugin uses layer-shell popup + `ai-agents-map-toggle`).
- **083, 084 (IPC host parts):** Maprisma out-of-process Dart feature-plugin IPC (`feature_plugins.dart`, `feature_worker.dart`, handle-expiry routing across processes) — **N/A** as IPC; this plugin is single-process Python and calls feature modules in-process (keeping `084`'s supervisor control-pipe/clean-stop rules inside WP5).

### Work Packages & Parallelism Matrix

> **Parallel execution rule:** Run **WP0** first (small foundation touching `settings_schema.py`, `adapter_data.py`, `adapters/*.toml`, i18n/manuals). Then **WP1, WP2, WP3, WP4, WP5** touch disjoint files and can run **concurrently in 5 parallel agent branches**. **WP6** starts once WP5's Python API is settled (can run in parallel with WP1–WP4). **WP7** is the single integration/wiring pass touching shared GTK shell files (`ai_popup.py`, `settings_pages.py`, `details_pages.py`, `terminal_pane.py`, manual).

| WP | Scope & Maprisma Tasks | Size | Source (`py` copy vs `dart/ui` port) | Maprisma Reference Files | Plugin Files Touched | Parallel? |
|---|---|---|---|---|---|---|
| **WP0** | **English-only (Section A) + Core Sync:** `069a` (generic desktop palettes), `072` (adapter `[ask]`/`[task]` metadata), `addons.py` key-fingerprint note, pre-register parity keys in `settings_schema.py` (`three_d`, `ai_activity_log*`, `ai_activity_import*`, `main_tab`) | **S** | `py` copy + Section A edits | `desktop_generic.py`, `adapter_data.py`, `adapters/*.toml`, `addons.py`, `settings_schema.py` | Section A files + `desktop_generic.py`, `adapter_data.py`, `adapters/*.toml`, `addons.py`, `settings_schema.py` | Run **first** (unblocks WP1–WP5) |
| **WP1** | **3D Map Layout (`069b`):** Extruded 3D category plinths, tiered 3D boxes, floating cylindrical cables, ports, glowing 3D pulses, `three_d` toggle (default on) | **M** | `py` (`flutter_map_editor.py` validator) + `dart/ui` → GTK4 Cairo/Snapshot | `flutter_map_editor.py`, `flutter_app/lib/{map_3d,map_editor,agent_map,map_groups,map_lights}.dart` | `flutter_map_editor.py`, `map_view.py`, `map_3d.py` *(new)*, `tests/test_map_3d.py` *(new)* | **Parallel** (independent) |
| **WP2** | **Configure Local LLM Providers for This PC (`077`, `093` "Setup" rename):** Hardware recommendations (VRAM/RAM/CPU/ROCm/Vulkan/CUDA), editable config form with live validation, preview + backup + Undo for llama.cpp, Ollama, LM Studio, Jan, KoboldCpp, vLLM, Strata | **L** | **Hybrid:** `py` copy for engine/provider facts & writers + `dart/ui` port for planner & GTK editor | **Py:** `provider_{llamacpp,ollama,lmstudio,jan,koboldcpp,vllm,strata}.py`, `context_ctl.py`, `llm_engine.py`, `install_ctl.py`, `adapters/hermes.py`. **Dart:** `packages/maprisma_core/lib/src/{pc_facts,provider_config,provider_config_files,provider_config_routes,llm_plans}.dart`, `flutter_app/lib/{provider_config_view,performance_view,providers_view}.dart` | `provider_*.py`, `context_ctl.py`, `llm_engine.py`, `install_ctl.py`, `adapters/hermes.py`, `provider_config.py` *(new)*, `provider_config_page.py` *(new)*, `tests/test_provider_config.py` *(new)* | **Parallel** (independent) |
| **WP3** | **Unified Ask AI (`078`):** Single plain-language composer across all map columns, auto-routing, suggestions from installed setup, free-text clarification, bounded follow-up context, early cancellation | **M** | `dart/ui` port | `packages/maprisma_core/lib/src/{smart_card,plan_worker}.dart`, `flutter_app/lib/map_smart_card.dart` | `smart_card.py`, `askai_page.py`, `tests/test_smart_card.py` | **Parallel** (independent) |
| **WP4** | **Live Agent Activity, Activity Log, Reports & History Import (`074`, `075`, `076`):** Running-agent session activity/progress/origin/stuck detection, opt-in local activity log (7-day default), "Collect history" from agent files, Reports page (Log / Topic reports / SQLite Library + "How reporting works" help dialog) | **L** | `dart/ui` port (stdlib `sqlite3`, no Python ref) | `packages/maprisma_core/lib/src/{activity_readers,agent_activity_report,agent_progress,ai_activity_log,jsonl_session_activity,live_session_activity,trajectory_activity,history_import,history_topic,reports_generate,reports_library,reports_sqlite,report_format}.dart`, `flutter_app/lib/{agent_activity_dialog,activity_log_pane,history_import_controller,reports_page,reports_output,reports_help}.dart` | `activity_log.py` *(new)*, `agent_activity_report.py` *(new)*, `history_import.py` *(new)*, `reports_ctl.py` *(new)*, `reports_page.py` *(new)*, `agent_activity_dialog.py` *(new)*, `tests/test_activity_reports.py` *(new)* | **Parallel** (independent) |
| **WP5** | **AI Project Center Core (`082` core, `084` supervisor, `085` core, `087`, `092` core, `095` core, `096` core):** Project CRUD/store (limit 64), stage/activity/bucket projection & `by_agent` summary, launch readiness + `Approved · blocked`, issue objects (`code`/`target`/`node`/`fix`), manual board order (`move`) + `importance` (`high`/`medium`/`low`), running-project messages (`queue` & `now` with `acknowledge_interrupted`), subprocess supervisor & saved handoff (reusing `process_group.py`) | **L** | `dart` port (in-process Python; reuses `process_group.py` & WP0's `adapters/{agy,claude,codex,pi}.toml`) | `packages/maprisma_core/lib/src/features/{ai_project_center,project_inventory,project_stage,project_supervisor}.dart` (incl. `maprisma-095` & `maprisma-096` branches) | `project_stage.py` *(new)*, `project_supervisor.py` *(new)*, `project_center.py` *(new)*, `tests/test_project_center.py` *(new)* | **Parallel** with WP1–WP4 |
| **WP6** | **Projects Tab & Board UI (`082` UI, `086` views, `088`, `089`, `090`, `094`, `095` UI, `096` UI):** Projects board (summary/filter chips, cards with stage rail, team strip, issues, Open/Review/Pause/Retry/Delete trash icon), manual drag/Move/keys reorder + High/Medium/Low mark & Goal filter, **Cards \| Phases** view (6 columns / narrow stacked list), project page (breadcrumb `Projects › <title>`, large stage rail, Goal/Team+Team graph/Plan/Run tabs), running-project message box (**Send** / **Send now**, waiting/delivered list, card badge) | **L** | `dart/ui` → GTK4/Adw port | `flutter_app/lib/{main_tabs,ai_project_center,project_board,project_page_parts,project_phases,project_messages}.dart` (incl. `maprisma-095` & `maprisma-096`) | `projects_board_page.py` *(new)*, `project_detail_page.py` *(new)*, `project_phases_page.py` *(new)*, `project_messages_view.py` *(new)*, `tests/test_projects_ui.py` *(new)* | Depends on **WP5** API (parallel with WP1–WP4) |
| **WP7** | **Shell Integration, Cross-Tab Links & Manual (`070` Esc check, `086` header tabs, `091`, `092` UI, `093` manual/screenshots):** Wire `[ Projects ] [ AI Map ]` header tabs, `Ctrl+1/2`, departure guards, Reports header button, local provider **Setup** menu + Details **Configure for this PC**, live activity button & terminal progress line, **Show on map** / **Fix on map** pan+highlight, **Used in N projects** agent badge/chip, Settings switches (3D map, activity log/import), rewrite English manual & rebuild PDF | **M** | `dart/ui` wiring + manual | `flutter_app/lib/{main,agent_map,map_editor,settings_view,agent_terminal}.dart`, `docs/manual/manual.html` | `ai_popup.py`, `settings_pages.py`, `details_pages.py`, `terminal_pane.py`, `docs/manual/manual.html`, `assets/user-manual.pdf`, `manifest.json`, `README.md`, `tests/test_tab_links.py` *(new)* | **Last** (integrates WP1–WP6) |

---

## C. Verification & Release-Safety Constraints

### 1. Headless Test Execution
- **Per-WP focused runs (fast, zero display required):**
  ```bash
  PYTHONPATH=tests python3 -m unittest -v test_i18n test_manual test_settings_responsive test_settings_schema test_compile
  ```
  (Replace/add the new WP test module names; mock `Gtk`/`Adw` widgets following [tests/test_settings_responsive.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_settings_responsive.py) and [tests/test_first_agent_recovery.py](file:///mnt/Data/Projects/omarchy-ai-agents-map/tests/test_first_agent_recovery.py) so all UI logic runs headless without Xvfb/Wayland.)
- **Syntax & diff hygiene:**
  ```bash
  python3 -m unittest tests/test_compile.py && git diff --check
  ```
- **Full suite (before merge/push only, with `LANG=C` for `test_battery`):**
  ```bash
  LANG=C python3 -m unittest discover -s tests -p 'test_*.py'
  ```
  Note from [docs/reviews/maprisma-experience-parity.md](file:///mnt/Data/Projects/omarchy-ai-agents-map/docs/reviews/maprisma-experience-parity.md#L278): display-dependent widget allocation tests (e.g., `test_back_keys.test_editing_sees_real_fields`) and live GTK4 layout/overflow checks require a desktop `WAYLAND_DISPLAY` session as a final smoke test; isolated temp `HOME`/config (`tests/isolate.py`) must always be used so real user settings/credentials are never touched.
- **English manual PDF rebuild (WP7):**
  ```bash
  python3 docs/manual/build.py
  ```

### 2. Omarchy Plugin Release-Safety Constraints
- **Plugin entry points preserved:** Keep [manifest.json](file:///mnt/Data/Projects/omarchy-ai-agents-map/manifest.json) (`id: "ubruckhaus.ai-agents-map"`, `kinds: ["bar-widget"]`, `entryPoints.barWidget: "BarWidget.qml"`), [BarWidget.qml](file:///mnt/Data/Projects/omarchy-ai-agents-map/BarWidget.qml) (clean QML load after removing `I18n.js`), and [ai-agents-map-toggle](file:///mnt/Data/Projects/omarchy-ai-agents-map/ai-agents-map-toggle) singleton PID/USR1 toggle intact.
- **Omarchy house features retained:** Keep `ⓘ` More plugins (`more_plugins.py`/`info_page.py`), `launcher_entry` (`app_entry.py`), Omarchy theme/Look integration (`desktop_omarchy.py`), and own config directory `~/.config/omarchy/ai-agents-map/` (`identity.py`).
- **Signed bundled add-ons untouched:** Never edit files inside `addons/*`; bundled data-only policy remains unchanged.
- **Marketplace binary scan & security invariants:** `assets/user-manual.pdf` must be `0644`, `> 512 KiB`, with `\0` in the first 4 KiB; all new subprocesses (especially WP5 `project_supervisor.py` and WP2/WP4 workers) must use `process_group.py` (clean group kill on Stop/exit), bounded reads (`scan_budget.py`), no shell interpolation, atomic private writes, and action-rule checks (`action_rules.py`) on every mutating route.
- **No push/release/version bump** without explicit user approval and final desktop GTK4 smoke verification.
