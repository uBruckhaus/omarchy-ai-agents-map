# Plan: the plugin as Maprisma (parity port)

Uwe, 2026-11-11: the Omarchy marketplace takes the Python plugin, so it gets all of Maprisma's features and
looks quite the same. Maprisma (Flutter, /mnt/Data/Projects/maprisma) stays the source of the features; this
plugin (GTK4 + libadwaita, Python) follows it. Branch: `maprisma-parity` (from dev 4493a20). Nothing is pushed
or submitted without Uwe's OK; Codex review before the marketplace push.

## Where things stand

- Plugin: ~46k lines of Python, last synced 2026-10-28 (0.26.1 published, #11928).
- Maprisma since then: tasks 004–068, ~80k lines of Dart. Its root `*.py` (the Python reference) carries a
  part of the core: 63 files shared with the plugin differ, 37 are new (flutter_*.py route handlers, addons,
  identity, provider_strata, system_moves, subagent_readers …).
- Dart only (must be written in Python, not copied): Ask AI (`smart_card.dart` 2.3k lines + plans,
  `map_smart_card.dart` 3.4k UI), action rules (`action_rules.dart`), dictation (`dictation.dart` 1.5k),
  llama.cpp measuring (`llama_tune.dart`), the choose routes (`models.choose_*`, `choice_failures`),
  updates.task (068), F12 screenshot with note.

## Gap list

Status: HAVE · PART · MISS · N/A. Source: py = copy from Maprisma's Python, dart = port, ui = GTK work.

| Area | Maprisma tasks | Plugin | Source | Size |
|---|---|---|---|---|
| Core sync: graph, inventory, agent details, readers, installers, MCP identity | 052, 054, 061–063j | PART (older copy) | py | L |
| Online / local agents cards, own provider, sign-in state, batteries stale, billed costs | 060a, 063a, 063c, 063g, 063j | MISS/PART | py | M |
| MCP servers of one name, digest identity | 063e, 063h | MISS | py | M |
| Strata as a local LLM | 064 | MISS | py + dart | L |
| Codex/Antigravity default model, Antigravity → Google | 061, 062 | MISS | py | S |
| Map look: group cards with counts + Expand, column chips, boxes with ⋮ menu, line colours, used-only filter, clipping, selection grays the rest, even gaps | 008, 016, 022, 050a, 054 | PART | ui | L |
| + cards on the map (Add MCP / skills / model), programs and local LLMs to install on the map | 056, 059, 060b | PART (on Setup only) | ui | M |
| Update badge on boxes, Update with AI…, Update now; Update runs what the check found | 068 | PART | dart + ui | M |
| Header like Maprisma: AI Map, Undo, status, Ask AI, legend, help, gear, refresh | 011, 026 | PART | ui | S |
| Centred overlay window for terminal and actions, dimmed map | 013, 017, 023, 025 | PART (side pane) | ui | M |
| F12 screenshot with a note, screenshot folder | 012, 015, 067a | MISS | ui | M |
| Corners "Slightly rounded" + three control sizes | 067b | PART | py + ui | S |
| Status per column (green / yellow / red) | 006 | PART | py (health.py) + ui | M |
| Ask AI: propose → check → review → apply, per column, helper choice, cost first, examples | 004, 007, 014, 018, 020, 026, 050c, 051, 063d | MISS | dart → py | XL |
| Rules for what may be done (allow / deny), on every change route | 019 | MISS | dart → py | L |
| Dictation in Ask AI and Settings | 024 | MISS | dart → py | L |
| Best context per local model, measure and tune | 029 | PART | py (rules) + dart (measure) | M |
| Settings: sections on the left, Find in Settings, group headings, new keys | 057, 058 | MISS | ui | M |
| Disconnect all at once, one Undo | 017, 020 | PART | dart | M |
| Known servers stay, fixed lines with a reason | 018 | PART | py | M |
| Signed add-ons and the add-on lock | 027, 028, 055 | N/A today | py | L (decision) |
| Recording, typed client, contract | 001–003 | N/A (internal) | – | – |
| System Map page | 008–011 | N/A (removed in Maprisma) | – | – |

Kept from the plugin (house rules, not in Maprisma): ⓘ More plugins page, bar widget + toggle singleton,
own settings folder `~/.config/omarchy/ai-agents-map/` (BarWidget reads `language` there), 8 languages.

## Phases

Each phase: tests green (`python3 -m unittest discover -s tests`), manual + translations for user-visible
changes, one commit per step, Uwe looks at it before the next phase.

1. **Core sync (py).** Bring Maprisma's newer Python core into the plugin file by file (graph, inventory,
   agent_details, agents, agent_readers, harnesses, install_ctl, mcp_ctl/library/tools, settings_schema,
   corners, provider_online, provider_strata, adapters), leaving out Maprisma-only parts (flutter_* routes,
   identity paths, add-ons unless decided). GTK pages adjusted to the new data. Covers 052–064's core.
2. **Map and header look.** Header order and symbols; group cards with counts and Expand; boxes with ⋮ menu
   (Connect, Details, Actions, update items), badges incl. the update badge; line colours per kind, used-only
   filter, selection grays the rest; + cards, programs to install, online / local agent cards; legend dialog;
   corners incl. slight.
3. **Overlay window.** One centred window over the dimmed map for terminal, reviews and actions; F12 screenshot
   with note; status symbol per column.
4. **Ask AI (dart → py).** The smart card engine and its plans (MCP, models, agents, providers, menu actions,
   disconnect all), the review/apply/Undo path, helper choice, examples, Update with AI…; action rules with
   their window, enforced on every change route.
5. **Settings and the rest.** Sections + Find in Settings and the new keys; dictation; measure and tune;
   remaining 063 details.
6. **Release.** Manual (8 languages + PDFs), preview.png, publishing skill preflight, Codex until CLEAN, then
   ask Uwe before push and [Verify].

## Open decisions (Uwe)

- Signed add-ons in the plugin, or all agents and LLMs built in?
- The Setup page and Smart Check page: replace by Maprisma's column focus and Ask AI status, or keep?
