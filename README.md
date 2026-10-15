# AI Agents Map

An easy overview of the AI setup on this computer, and a way to change it by drawing: which coding
agents are installed, which LLMs they use (cloud services and local runtimes) and the models those
serve, which MCP servers and skills each agent has. For people new to it as much as for checking and
tidying your own setup.

![AI Agents Map](preview.png)

## Install

```sh
omarchy plugin add https://github.com/uBruckhaus/omarchy-ai-agents-map.git --enable
```

A robot button appears in the bar. Click it to open or close the window. It fills the screen; Esc, a
click beside the card or switching to another window closes it. It stays running in the background
between opens, so it appears instantly.

Requires Python with PyGObject, GTK4 and GTK4 Layer Shell in an Omarchy/Hyprland session; all of
these come with a standard Omarchy install. Installing local LLMs uses `curl` and `tar` (and `uv` for
vLLM); the MCP and skills search uses `curl`.

## One view: cards and the map beside them

The cards are on the left, the map on the right (on a screen narrower than about 1000 px only the
cards). The sidebar button left of the title (Ctrl+B) hides the cards so the map takes the whole
width; every open shows them again.

- **Cards**, in four sections. Click a card to open its details.
  - **Agents**: every coding agent Omarchy offers (its Setup → Defaults → Agent list), installed
    ones first with their version, Omarchy's default agent marked. Details: plan, rate limits with
    reset times and today's usage (from Omarchy's own usage data, refreshed when you open the card),
    model, providers (local ones named by runtime), MCP servers, skills, tools, last used, settings
    folder.
  - **Local LLMs**: llama.cpp, Ollama, LM Studio and vLLM: running or stopped, the address, the user
    service that starts it, the models folder and every model it can serve, and which agents use it.
  - **MCP servers**: each server with what it runs (a program or a remote host) and which agents
    use it, also servers no agent uses any more. **ⓘ Details** shows each agent's settings file,
    command or address (names only for environment variables and headers) and, on request, the
    server's tools; **Remove** takes it from every agent and from the list.
  - **Skills**: each skill folder with its skills and the agents that read it (linked skills count
    where they really live, so a shared `~/.agents/skills` shows up once). **ⓘ Details** lists every
    skill with its description and how each agent gets the folder.
- **Map**: MCP servers and skills ← agents → LLM providers → models. Hover a box to light up
  everything it is joined to. A click on a card or a box selects it (✓), Shift+click adds or
  removes more (one agent at a time). The category you select in then shows only the selected
  boxes; the other columns keep every box, but only the connections of the selection are drawn.
  Each agent's active path (agent → provider in use → its model) is drawn bold. Esc or **Clear
  selection** clears the selection. The **Show** toggles above the map shorten it to the categories
  you are working on (models need a provider category, MCP servers and skills need Agents); the
  choice is remembered. **− 100 % +** (Ctrl+scroll, Ctrl+plus/minus/0) zooms the map.

Every column heading (**MCP SERVERS · SKILLS ▾**, **AGENTS ▾**, **LLM PROVIDERS ▾**, **MODELS ▾**)
opens a list of its boxes in groups (MCP servers and skills; online and local providers; models by
the folder they live in): untick a box, or a whole group at once, to leave it out of the map. What
hangs only on it goes too (a provider's models, an agent's own MCP servers and skill sets). The
heading shows how many are shown, e.g. (5/8). The models column is grouped by folder as well, and
each folder ends in a dashed **+ Model** box that opens the download page below for that folder
(when several local LLMs share it, you pick which one downloads).

## Connecting by drawing

Every change below shows exactly which lines of which file change before anything is written, keeps
the old file in `~/.local/state/ai-agents-map/backups/`, and can be undone from the bar that follows.

- **Agent ↔ local LLM**: press on an agent and drag to a local LLM (or back). Pick the model and
  whether the agent uses it right away. Click a line to select it: drag its end to another local LLM
  to move the connection, or onto empty space to remove it. With an agent selected, a click on a
  model of a local LLM it is connected to makes that model the agent's model. Supported: Hermes
  (`~/.hermes/config.yaml`), Pi (`~/.pi/agent/`) and OpenCode (`~/.config/opencode/opencode.json`).
- **Agent ↔ MCP server**: the server's settings are copied from an agent (or the list) that has it
  into the other agent's own format, for Claude Code, Codex, Copilot, Crush, Cursor, Gemini, Grok,
  Hermes, Muse, Oh My Pi, OpenClaw, OpenCode and Pi. Claude Code is changed with
  `claude mcp add-json/remove --scope user`, except for servers with environment variables or
  headers (they may hold keys): those are written into `~/.claude.json` directly, so no key ever
  appears in a command line.
- **Agent ↔ skill folder**: Pi and Hermes get the folder in their settings list; every other agent
  gets one link per skill in its own skills folder. A folder an agent reads by itself (most read
  `~/.agents/skills`) cannot be taken from that one agent; the page says so.
- **Local LLMs**: right-click to **Start** or **Stop** one or to start it at login (its user
  service, or `lms server` for LM Studio), or **Free GPU memory**: its models are unloaded, it keeps
  running and loads one again when asked (vLLM cannot unload, so it is stopped).

## Local LLMs start and stop with their agents

Connect an agent to a local LLM once, close AI Agents Map and just start the agent: its local LLMs
start with it, and when the last agent using one closes, that LLM stops, so its GPU memory is free
again. This works with the window closed and after a reboot.

- A small watcher does it, the user service `ai-agents-map-providers.service`
  (`provider_watch.py`). It is set up when the first agent is connected to a local LLM and removed
  when the last connection goes or the setting **Start local LLMs with their agents** is switched off.
- Once a second it looks at this user's processes (`/proc` only). A connected agent starts →
  `systemctl --user start` the service of the local LLM it uses now, the one its current model is
  on (`lms server start` for LM Studio); other LLMs it is only set up for stay off. Choosing
  another model on the map while the agent runs starts that LLM within a second. The last one closes
  → after 5 s the LLM is stopped, unless another program still has a connection open to it.
- Background servers of agents (OpenCode's `opencode serve --service`, which runs all the time) do
  not count as the agent being open, and their idle connections do not keep an LLM running.
- If `lms server start` had to start LM Studio itself (a hidden `lm-studio --run-as-service`), that
  one is ended too, with its children and its engines (`~/.lmstudio/extensions/backends/…`, which
  hold the loaded model and outlive the app otherwise): the engines with SIGTERM, then the app's
  processes all at once with SIGKILL and the AppImage process last, because LM Studio runs from the
  AppImage's mount and ending it piece by piece makes the rest crash. An LM Studio window you opened
  is left alone.
- What it still has to stop is kept in `~/.local/state/ai-agents-map/provider-watch.json`, so a
  restart (reboot, plugin update) or removing an LLM's last connection never leaves it running.

Above the map, **GPU memory** (in use, free, split into local LLMs, desktop & apps and free) and
**GPU activity** (load, temperature, power; green while a model is generating) show the same as LLM
Monitor. Each local LLM's box shows how much GPU memory it holds, and loaded models get a green dot.

## Adding MCP servers and skills

**+ MCP server** (on the map or under the cards) opens a page with three sources that all fill the
same form; you tick the agents that get it, **Test** asks the server for its tools, **Add** writes
all ticked agents at once (one preview, one Undo). No agent is ticked unless you selected one.

- **Found on this PC**: programs that offer an MCP server, found by reading the files (nothing is
  started): your own programs (`~/.local/bin`, `~/bin`, extracted apps in `~/Applications`, `/opt`)
  and anything on PATH with "mcp" in its name. A program counts when its own usage text has an
  `mcp` mode (`x-cli mcp [--automation-read-root DIR]`) or it holds the MCP protocol's words; read
  and write folders get a folder that fits the app. AppImages are never run by this scan: **Look
  inside…** unpacks each one's `usr/bin` into a temporary folder, reads it and removes it again.
  Adding a server that lives in an AppImage unpacks the AppImage into
  `~/Applications/<app>-<version>/` first. Servers already set up are not offered again.
- **Search the web**: the official MCP registry (`registry.modelcontextprotocol.io`). npm packages
  run with `npx`, PyPI packages with `uvx`, images with `docker`, remote servers by their https
  address; the variables a server needs (API keys, folders) become fields.
- **By hand**: a command line, or an address with headers.

**+ Skills** opens the same kind of page: skill folders that apps brought along and no agent reads
yet (`/usr/share`, `/opt`, `~/Applications`, `~/.local/share`, global npm packages, Claude plugins),
or a search of the skills.sh directory. A skill from the web is fetched from its GitHub repository
(file list from `api.github.com`, files from `raw.githubusercontent.com`) into a folder of your
choice, by default `~/.agents/skills`. Skills can hold scripts an agent may run later; the page
shows the repository and lists the scripts before anything is fetched.

MCP servers stay listed (`~/.config/omarchy/ai-agents-map/mcp-servers.json`, readable only by
you) when no agent uses them any more, so a line can connect them again. On every open,
AI Agents Map looks for new programs and skill folders in the background; new ones show as
"· N new" on the **+** boxes.

### New agents

When Omarchy offers an agent this plugin does not know yet and it is installed, AI Agents Map asks
an installed agent once to find out on the web where it keeps skills and MCP servers: Claude Code
(`claude -p` with only its WebSearch and WebFetch tools: no shell, no file access, no MCP servers)
or else Codex (`codex --search exec` in its read-only sandbox). The prompt holds only the agent's
name, command and version. The answer is checked before use (folders only under your home folder,
never `~/.ssh`, `~/.gnupg` or credential files; JSON, TOML or YAML only; a fixed list of field
names; nothing in it is ever run) and kept in `~/.config/omarchy/ai-agents-map/agents/`. Settings →
**Look up new agents** switches this off; right-click the agent on the map to look it up again.

## Installing local LLMs

Right-click a local LLM on the map. Not installed (grey): **Install…** downloads the official release
into a program folder you choose (one folder per version, a `current` link, the command in
`~/.local/bin`) and sets up a user service, so no password is needed; you also choose the model
folder (the one your other local LLMs use is suggested) and whether it starts at login. Installed
this way: **Update…** (new version folder, the previous one is kept as a fallback), **Model folder…**
(change it later) and **Uninstall…** (program, command, service and settings; never the models).
Installs made by hand are shown but never changed.

- **Ollama**: the official release (with ROCm for AMD cards), user service `ollama`.
- **llama.cpp**: the official build of your choice (Vulkan, preselected for AMD unless ROCm is
  installed; ROCm; CUDA; CPU), run as a model router (`llama-server` service: every GGUF in the model
  folder, one loaded at a time, per-model settings in `~/.config/llama.cpp/models.ini`).
- **LM Studio**: its AppImage, unpacked (no FUSE needed), with a desktop entry; its download folder
  is set to the model folder. Its server runs with `lms server`.
- **vLLM**: for NVIDIA cards, in its own Python environment (uv); on AMD cards vLLM needs ROCm
  containers, so it is not offered there.

## Downloading models

Right-click an installed local LLM → **Models…** (or click a folder's **+ Model** box): the installed models on the left; on the right
search Hugging Face for GGUF models, pick one and a size (the one that fits your graphics card with
room for context is marked and preselected) and **Download** it into the model folder, with
progress (Stop download stops it). LM Studio gets the folder layout it expects; a llama.cpp
model folder that only holds links (a router folder) gets the file next to the originals and a link.
For Ollama there is also a field for library names (qwen3:8b); a Hugging Face model is pulled as
hf.co/<repo>:<size>, and Ollama is started for it if it is not running. Before anything is sent to
Ollama on 127.0.0.1, AI Agents Map checks that the listening port belongs to you.

### Context size

Each installed model shows its context now and the best one for your graphics card ("now 96k ·
best 92k"), calculated from the model file's header: its size, and how much video memory every token
of context takes (KV heads × key and value size × the cache type: f16, q8_0, q4_0). Sliding-window
layers (Gemma) only keep their window and recurrent layers nothing, so those models get much more.
**Set** writes it: for llama.cpp `ctx-size` in the model's section of `~/.config/llama.cpp/models.ini`
(backup and Undo, restart llama.cpp for it to take effect), for Ollama the model's `num_ctx`, for
LM Studio `llm.load.contextLength` in the model's own defaults file
`~/.lmstudio/.internal/user-concrete-model-default-config/<model>.json` (what LM Studio writes when
you change it in its model settings; backup and Undo; used the next time LM Studio loads the model).
LM Studio's models are listed by the names its server answers to (`gemma-4-12b-it-qat`), read from
its model index, so agents connected to it ask for a model LM Studio knows. LM Studio loads a model
with 8192 tokens unless told otherwise, too few for an agent's prompt: connecting an agent to an LM
Studio model whose context is not set yet sets the best one in the same step (it is in the preview,
the backup and Undo), and the agent is told the same context. A context you set yourself is kept.

## Privacy and capabilities

- Never asks for root or administrator rights. Every program it starts is run as an argv list
  without a shell, and every program's output and every web answer is read with a size limit and
  one overall deadline.
- **Reads** (to draw the setup): the agents' settings files, mise's install folders, the pacman
  database, user services (`~/.config/systemd/user`), model folders and this user's own processes
  (`/proc`, own UID only, including their GPU memory from DRM fdinfo), LM Studio's model index
  (`~/.lmstudio/.internal/model-index-cache.json`), the GPU's sysfs values
  (memory, load, temperature, power) and `/proc/net/tcp` (who listens on or is connected to a local
  LLM's port). From settings it takes names, model ids, folders and addresses; it never
  opens credential files (`auth.json`, `.credentials.json`). MCP server entries are read whole so
  they can be copied to another agent; their environment and header values are never shown.
- **Network** (each only on a click, except where noted): GitHub's release API and release
  downloads for Ollama and llama.cpp, `lmstudio.ai`/`installers.lmstudio.ai` for LM Studio, PyPI
  (and `uv`) for vLLM; `huggingface.co` for model search and downloads; the local Ollama on
  127.0.0.1 only after checking that the listening socket belongs to you; the same for each running
  local LLM's own API every 2 s while the window is open (which models are loaded) and for
  **Free GPU memory** (unload); answers are capped at 1 MiB and only shown, never run;
  `registry.modelcontextprotocol.io`; `skills.sh`, `api.github.com` and
  `raw.githubusercontent.com` for skills; an MCP server's own address when you click **Test**; and,
  for a newly installed unknown agent (automatic, see above), whatever Claude Code or Codex
  searches. All web requests go through `curl` with https only, at most a few https redirects
  (none for skills and the registry), size and time limits, and request headers handed over on
  stdin, never on the command line.
- **Starts**: `systemctl --user` start/stop/enable/disable/daemon-reload for the services it
  installed or that start a local LLM (service-management); the installers' `curl`, `tar`, the LM
  Studio AppImage's `--appimage-extract`, `uv venv`/`uv pip install` (installer);
  the watcher service described above (service-management: `systemctl --user start/stop` of the
  connected LLMs' services, `lms server start/stop`, `lms daemon down`; a hidden LM Studio it
  started is signalled through a pidfd taken when it is found, so a reused PID is never hit);
  `lms unload --all` for **Free GPU memory**;
  `omarchy-agent-usage-update <agent>` when an agent card's usage is older than ten minutes; an MCP
  server's command when you click **Test** or ask for its tools (in its own process group, stopped
  afterwards); an AppImage's `--appimage-extract` only on **Look inside…** or when adding a server
  from it; `claude mcp add-json/remove`; `claude -p` or `codex exec` for a new agent; `wl-paste`,
  `lspci`, `hyprctl`.
- **Writes**, each only after you confirm it with its preview (except AI Agents Map's own state):
  the agents' settings files named above (keeping their file mode; new files 0600), links in the
  agents' skills folders, fetched skill files, programs you install into a folder you choose (one
  folder per version, a `current` link, the command in `~/.local/bin`), user services in
  `~/.config/systemd/user`, their settings in `~/.config/<runtime>/`, an LM Studio desktop entry,
  `~/.config/llama.cpp/models.ini` and LM Studio's per-model defaults (context sizes). Generated service files refuse paths with `%`,
  quotes, `$`, backslashes or control characters. Own state: `~/.config/omarchy/ai-agents-map/`
  (settings, the MCP list, looked-up agents, `providers-with-agents.json` for the watcher; private
  files), the watcher's unit `~/.config/systemd/user/ai-agents-map-providers.service` and its state
  `~/.local/state/ai-agents-map/provider-watch.json`, `~/.local/state/ai-agents-map/backups/`
  (0700), `~/.cache/ai-agents-map/` (scan results and tool lists).
- Text pasted into its fields is read with `wl-paste` (at most 8 KiB, 2 s deadline) instead of
  GTK's built-in paste, which buffers whatever another app offers with no limit; middle-click paste
  is off and the fields take no drops.
- The PID file lives in `$XDG_RUNTIME_DIR` (or a private 0700 fallback); the popup is only signalled
  after full process identity checks (uid, interpreter, exact script path, kernel start time).

## Languages and settings

The gear (Ctrl+,) opens the settings: **Language** (system or one of English, Deutsch, Français,
Español, Italiano, Português, Nederlands, Polski), **Corners** (like windows, rounded or square, as
in App Manager and Theme Manager), **Look up new agents** and **Start local LLMs with their agents**. Backspace or Esc goes back. Settings
live in `~/.config/omarchy/ai-agents-map/settings.json`.

## Remove

```sh
omarchy plugin remove ubruckhaus.ai-agents-map
```

This removes the bar button and the window. Switch off **Start local LLMs with their agents**
first, or remove the watcher by hand:

```sh
systemctl --user disable --now ai-agents-map-providers.service
rm ~/.config/systemd/user/ai-agents-map-providers.service && systemctl --user daemon-reload
```

Changes you made with it stay (agents' settings, MCP
servers, skills, installed local LLMs); uninstall a local LLM first with right-click →
**Uninstall…** if you want it gone. To also drop AI Agents Map's own state:

```sh
rm -rf ~/.config/omarchy/ai-agents-map ~/.local/state/ai-agents-map ~/.cache/ai-agents-map
```

## Files

| Path | Purpose |
| --- | --- |
| `manifest.json` | Plugin metadata (bar-widget) |
| `BarWidget.qml` | Bar button: runs `ai-agents-map-toggle` |
| `ai-agents-map-toggle` | Singleton toggle: start the window, or signal it to show/hide |
| `ai_popup.py` | GTK4 layer-shell window: cards, map, pages |
| `map_view.py`, `graph.py` | The map: drawing, selection, lines; what it shows |
| `agents.py`, `agent_details.py`, `inventory.py` | What is installed and set up (read only) |
| `connect.py` | Planned changes: diff, backup, apply, Undo |
| `provider_watch.py` | The watcher service: start local LLMs with their agents, stop them after |
| `gpu_usage.py` | GPU memory and activity, per local LLM and loaded models |
| `mcp_ctl.py`, `mcp_library.py`, `mcp_tools.py` | MCP servers per agent, the list, asking a server for its tools |
| `mcp_scan.py`, `mcp_registry.py` | MCP servers found on this PC and in the official registry |
| `skills_ctl.py`, `skills_scan.py`, `skills_web.py` | Skill folders per agent, from apps, from skills.sh |
| `agent_profiles.py` | Looking up new agents on the web |
| `install_ctl.py`, `runtime_ctl.py`, `models_ctl.py`, `context_ctl.py` | Local LLMs: install, start/stop, models, context size |
| `netfetch.py` | The one bounded way to the web (curl) |
| `ai_core.py`, `palette.py`, `corners.py`, `layer_watch.py`, `pidfile.py`, `i18n.py`, `I18n.js` | Shared helpers, theme colours, corners, closing for Omarchy's menus, PID file, languages |
| `tests/` | Unit tests (`python3 -m unittest discover -s tests`) |
