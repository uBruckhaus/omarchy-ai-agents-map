# AI Agents Map

An easy overview of the AI setup on this computer, and a way to change it by drawing: which coding
agents are installed, which LLMs they use (cloud services and local runtimes) and the models those
serve, which MCP servers and skills each agent has. For people new to it as much as for checking and
tidying your own setup.

![AI Agents Map](preview.png)

Setting up by drawing (the animation in Help, drawn in your theme's colours):

![An agent is connected to a local LLM, a model, an MCP server and a skill folder by drawing lines; then its pin and right-click menu](assets/drawing-demo.gif)

## Install

```sh
omarchy plugin add https://github.com/uBruckhaus/omarchy-ai-agents-map.git --enable
```

A robot button appears in the bar. On the first open AI Agents Map asks whether to add itself to
Omarchy's app launcher (Super + Space); nothing is written there unless you say yes.
It is a normal app window: Hyprland tiles or floats it like any other, Alt+Tab and workspaces work,
and it stays open while you work in other windows. The bar button opens it, brings it to the front
(also from another workspace), or hides it when it is already in front. Closing the window hides it:
the program keeps running in the background and opens again at once. While an agent runs inside it,
closing (Super + Q) asks first: **Stop and close** (its local LLM then stops too and frees the
graphics card's memory) or **Keep running**. When AI Agents Map ends, the agents running in it end
with it.

Requires Python with PyGObject, GTK4 and libadwaita in an Omarchy/Hyprland session; all come with a
standard Omarchy install (libadwaita with Nautilus). Running agents inside the window (▶ on an agent's box, or **Start here** in its menu) needs VTE for GTK4
(`sudo pacman -S vte4`); **Start in terminal** opens it in a terminal window of its own. Installing local LLMs uses `curl` and `tar` (and `uv` for
vLLM); the MCP and skills search uses `curl`.

## Setup and the map

The header has **Setup** and **Map** on every page (the one in view highlighted), then a red **!**
when something needs fixing, **ⓘ** for the legend, **?** for Help (F1) and the gear for Settings
(Ctrl+,).

**Setup** has a card for every agent, local LLM, MCP server, skill folder and model, in sections
that fold with a click on their bar:

- **How things get onto your map**: a folded card at the top with five numbered steps (switch on or
  find what you use, install what is missing, sign in to an online agent, connect by drawing, what
  shows up by itself); **What you can do with this**: what the setup is good for (everyday help,
  coding, images, voice, research and writing), one tile each with the next step.
- **AI agents, Local LLMs**: every one the plugin knows. Installed ones have a blue frame and their
  version, the others **Install**. A click on an installed card opens its details.
- **MCP servers, Skills, Models**: a card for each one that is installed, and a card at the end of
  each section that adds one (**Add MCP server**, **Add skills**, **Add model**).
- **⋯** or a right-click on a card opens the same menu as the box on the map: update, sign in, start
  or stop, details, uninstall or remove, move a model. A yellow **↑ version** means an update is
  waiting, a red **!** an open problem there (hover it).
- A small **battery** on an online agent's card shows what is left of its limits (a session, a
  week): the tightest one counts, yellow at a quarter or less, red under a tenth; hover it for each
  limit and when it resets. On a running local LLM's card it is the GPU memory still free.
- The refresh symbol (F5) looks again at what is installed, set up and signed in.

**The map** shows only what is set up, across the whole width: MCP servers and skills ← agents →
LLM providers → models, under the **GPU** card (GPU memory and activity; it folds).

- Hover a box to light it and what it is joined to; the **pin** at its top right keeps that lit.
  Click selects (✓), Shift+click adds more, Esc clears. **Right-click** a box for everything its
  card offers. **▶** on an agent's box starts it in a terminal in this window.
- Each agent's active path (agent → provider in use → its model) is drawn bold; a **red dotted**
  line joins an agent to a provider it is set up for but does not use now. Online agents' boxes
  carry the same battery as their cards, running local LLMs' boxes the free GPU memory.
- The **Show** toggles leave whole categories out (remembered). In a narrow window (below about
  1300 px) the map picks them itself, said in a line beside the toggles: agents and providers while
  an agent runs, else providers and models; **Maximize for more room** in the header gives it the
  whole screen. **− 100 % +** (Ctrl+scroll, Ctrl+plus/minus/0) zooms.
- Until the first line is drawn, a card above the map shows how in three steps (**Watch how** plays
  the animation from Help); it goes for good after the first connection, or with ×.
- **Legend**: **ⓘ** in the header explains every dot, line and mark of the map, drawn as there.
- **Terminal tabs**: while agents have terminals here, a tab line above the map has a tab for each
  with a dot for working, running or ended; a click shows that terminal, × closes an ended one
  (Settings › **Terminal tabs above the map**, on by default).
- **Working now**: while an agent is working (herdr shows it working, its process uses the CPU, its
  session files were written in the last seconds, or its local LLM is generating), light runs along
  its line to its provider and model (Settings › **Map animations**: agents and other programs, only
  agents, or off; off when the desktop turns animations off).
- **Used from outside the map**: when a program that is not on the map (a plugin such as Video
  Trans, a chat app, a script) generates with a local LLM, the line from that LLM to the model is
  drawn bold in a colour of its own, light runs along it, and the model box says who uses it ("in use
  · Video Trans"). It shows while that program sent a request in the last 10 s and the GPU was
  generating.
- **Running now**: an agent with an open instance gets a green dot and "running" (or "2 running"),
  and the model box it runs with says "in use · <agent>": the model given with `--model` when it was
  started, else the one in its settings. Checked every 2 seconds from `/proc` (your own processes
  only).

### Open problems, updates and suggestions

A red **!** in the header appears when something needs fixing; its tooltip says how many. It opens
**Open problems**, one line each (an agent that is not signed in, a local LLM without models, a
connection that does not work, little free space for models) with a button to where it is fixed:
the agent's or local LLM's details, the map with that line lit, Setup, or Tidy up. The same problems
show as a red **!** on their cards. The check only reads what the window collected plus the
graphics card, memory and free disk space; nothing is changed, started or sent.

Below them, **Updates and suggestions** lists new programs or skills found on this PC (with **Add
MCP server…** / **Add skills…**), what is on this PC but not on the map (with **Setup**), and
**updates** for the installed agents and local LLMs. With only these and no problem, the **!** shows
in the accent colour. Updates are checked in the background when the window opens and at most every
6 hours (cached in `~/.cache/ai-agents-map/updates.json`, 0600), each the way it was installed, so
**Update** installs what was named:

- agents installed with mise (Claude Code, Codex, Copilot, Cursor CLI, Muse, OpenCode, …): one
  `mise outdated --json`; **Update** runs `mise upgrade <tool>` in a terminal window of its own
  (with `MISE_USE_VERSIONS_HOST=0`, so mise reads the tool's own releases: its version service can
  lag behind a release for hours; aqua still checks the SHA-256);
- Claude Code also by its own release channel (`autoUpdatesChannel` in `~/.claude/settings.json`,
  from the npm registry's dist-tags), like its "Update available!" message; installed with its own
  installer, **Update** runs `claude update`;
- local LLMs and agents AI Agents Map installed: their latest release; **Update…** opens the
  Update page (SHA-256 checked as on install);
- system packages are left to the system's updates.

Nothing is installed or run by the check itself (except `mise outdated`); an update starts only
with a click on **Update**. Pages opened from the map or from Setup (details, adding MCP servers or
skills, models, installing, connecting) open in place of the map; **Back** returns to where they
were opened from.

### Details pages

An agent's details (a click on its card, or right-click its box › **Details**) start with whether it
is ready (and if not, why) and whether it is running now or when it was last used. Below, every
category is a card that folds, with a one-line summary and badges:

- **Usage and tokens**: today's and the last 7 days' tokens and sessions, the plan, a chart of tokens
  per day, rate limits with reset times (from Omarchy's own usage data, refreshed when you open the
  page) and tokens by model.
- **Projects, last 30 days** (Claude Code, OpenCode, Hermes): per project folder the working time,
  sessions, tokens, subagent runs, lines changed and what the tokens would cost at API prices, from
  each agent's own session files or database; no message text is read.
- **In herdr now**: when you run agents in [herdr](https://herdr.dev), each of the agent's herdr
  panes with its state (waiting for you, working, idle, done) and folder, read again every 4 seconds
  while the page is open; a click on one switches herdr to that pane and brings its terminal window
  to the front.
- **Health**: every check with a ✓, or what to do next (sign in with which command, which
  subscription or API key it needs and where to get one, install or start its local LLM, download a
  model, add MCP servers or skills, a connection that does not work).
- **Security settings**: what the agent may do without asking you, read from its own settings file —
  Claude Code (permission mode, sandbox, block bypass mode, allow/ask/deny rules), Codex (when it
  asks, sandbox, network in the sandbox, trusted folders), Copilot (permission mode, trusted
  folders), Cursor CLI (approval, sandbox, network, web search, allow/deny rules), Hermes (dangerous
  commands, hide secrets, where commands run), OpenCode (ask/allow/never for editing, commands, web
  pages, outside the project) and Pi (its tools; Pi never asks). Each setting opens to every choice
  with what it means and its risk; **Use…** shows the change (old → new, risk before and after, the
  lines of the file) and **Change** writes only that key, with a backup and Undo.
- **Risk management**: what the settings mean together, most serious first, with what lowers each
  risk (**Lower it…** opens the change); **What the words mean**: sandbox, approval, allow and deny
  rules, trusted folders, network, bypass, automatic review, prompt injection, MCP servers.
- **Configuration**: its settings, MCP servers, skills and settings folder.

A local LLM's details do the same: running, GPU memory and loaded models, models, connected agents,
what is missing, and who can reach it (only this PC on 127.0.0.1, or your whole network). An MCP
server's details show each agent's settings file, command or address (names only for environment
variables and headers) and, on request, the server's tools; **Remove** takes it from every agent. A
skill folder's details list every skill with its description and how each agent gets the folder.

## AI Playground and Information

Chess and other tests with your agents are in a plugin of their own, **AI Playground**: an agent
plays chess against you (or two agents against each other), speed and cost side by side, two answers
judged blind, pictures and video frames, and your local LLMs. It uses the agents that are set up here.

- When AI Playground is installed, its button (a gamepad) appears in the header. Select an agent on
  the map first, and AI Playground opens with that agent chosen. Without AI Playground the button is
  not shown.
- **ⓘ Information** (beside Help) describes AI Playground and links to the author's other Omarchy
  plugins; a card opens the plugin's page in the Omarchy Plugin Marketplace in your browser.

## Connecting by drawing

Every change below shows exactly which lines of which file change before anything is written, keeps
the old file in `~/.local/state/ai-agents-map/backups/`, and can be undone from the bar that follows.

- **Agent ↔ local LLM**: press on an agent and drag to a local LLM (or back). Pick the model and
  whether the agent uses it right away. Click a line to select it: drag its end to another local LLM
  to move the connection, or onto empty space to remove it. With an agent selected, a click on a
  model of a local LLM it is connected to makes that model the agent's model. Supported: Hermes
  (`~/.hermes/config.yaml`), Pi (`~/.pi/agent/`), OpenCode (`~/.config/opencode/opencode.json`) and
  OpenClaw (`~/.openclaw/openclaw.json`; a file it can no longer read safely is left alone, and
  `openclaw configure` is the way then).
- **Model lines**: a selected box shows an orange hook at the end of each line that can be moved.
  Drag a local LLM's line to a model onto another model of the same local LLM: every agent that uses
  it switches (one Undo for all); into empty space, they are disconnected from it. A model that is
  only loaded (no agent uses it) is unloaded when its line goes into empty space, or the other one
  is loaded instead. Nothing moves while a running agent, or a program outside the map, uses the
  model. Draw a line from a local LLM to one of its models to load it, and tick agents (none at
  first) that should get it.
- **Online models**: Claude Code's and Codex's models show under their cloud. Drag an agent's model
  line to another one to switch (its own setting: `model` in `~/.claude/settings.json`, `model = "…"`
  in `~/.codex/config.toml`; backup and Undo), counted from the agent's next start. A line drawn from
  an agent to its online provider starts the agent's own sign-in in a terminal; dragging that line
  into empty space asks first, then runs its own sign-out (`claude auth logout`, `codex logout`).
- **A provider that is gone**: an agent still set to use a local LLM it has no provider for gets a
  red line with **Repair** (connect it again, with preview) and **Remove** (clear that setting).
- **Agent ↔ MCP server**: the server's settings are copied from an agent (or the list) that has it
  into the other agent's own format, for Claude Code, Codex, Copilot, Crush, Cursor, Gemini, Grok,
  Hermes, Muse, Oh My Pi, OpenClaw, OpenCode and Pi. Claude Code's servers are
  written into `~/.claude.json` directly (never with `claude mcp add-json`), so no key from
  env, headers, args or the URL ever appears in a command line.
- **Agent ↔ skill folder**: Pi and Hermes get the folder in their settings list; every other agent
  gets one link per skill in its own skills folder. A folder an agent reads by itself (most read
  `~/.agents/skills`) cannot be taken from that one agent; the page says so.
- **Local LLMs**: right-click to **Start** or **Stop** one or to start it at login (its user
  service, or `lms server` for LM Studio), or **Free GPU memory**: its models are unloaded, it keeps
  running and loads one again when asked (vLLM cannot unload, so it is stopped). A local LLM that
  holds GPU memory gets a stop button on its box that asks first: **Free GPU memory** or **Stop**
  (for LM Studio **Stop server**: the app stays open with its models). LM Studio open without its
  server says *server off* (yellow dot) and offers **Start server**; a model a local LLM has loaded
  but no agent uses is joined to it by a solid light line.

### When a connection fails

Right after a drawn connection is written, AI Agents Map checks whether it will work:
- **Local LLM:** the agent's settings have the provider. The LLM is installed, lists the model, and
  is running or will be started with the agent. When it runs, its port is yours and its API offers
  the model. The model's context is at least 16k tokens.
- **MCP server:** the agent's settings have it, and it answers (initialize and tools/list, as
  **Test** does: a local server is started for this and stopped again).
- **Skills:** the folder is there with skills in it, and the agent's links to them are not broken.

A connection that could not be written at all, or that fails its check, stays on the map as a
**dotted red line** with a **!** (a **dotted yellow** one has warnings only). Click the line or its
**!** for the hints:
- **Test again** checks once more; when it passes, the line is normal again.
- For a connection that was never written, **Try again…** opens the connect page.
- **Remove connection** removes it as usual (for one that was never written, only the line goes).

The hints stay with the line until you remove it by hand, also across restarts
(`~/.config/omarchy/ai-agents-map/failed-links.json`).

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
- KoboldCpp is the exception: it takes about 6 s to start, longer than some agents wait for their
  first answer, and costs no video memory while no model is loaded. So it starts at login (its
  install page preselects that) and the watcher only **unloads** its model when the last agent
  closes (`POST /api/admin/reload_config` with `unload_model`, after checking that the listening
  port belongs to you).
- When the plugin is updated, the watcher notices its own file changed and restarts with the new
  version (what agents still need keeps running).
- What it still has to stop is kept in `~/.local/state/ai-agents-map/provider-watch.json`, so a
  restart (reboot, plugin update) or removing an LLM's last connection never leaves it running.

Above the map, **GPU memory** (in use, free, split into local LLMs, desktop & apps and free) and
**GPU activity** (load, temperature, power; green while a model is generating) show the same as LLM
Monitor. Each local LLM's box shows how much GPU memory it holds, and loaded models get a green dot.

## Adding MCP servers and skills

The Add pages share one layout: a short explanation of what MCP servers or skills are and how they
are used, tabs at the top (the choice is kept per page), and on the web side **Search on**: the default site, or
a site you add with **Add a site…** (kept in the settings until you remove it with the bin):

- models: Hugging Face, or a site with the same API (a mirror such as `https://hf-mirror.com`);
- MCP servers: the official MCP registry, or another registry with its API (`…/v0/servers`);
- skills: skills.sh, a site with its API, or a GitHub repository with skills
  (`https://github.com/owner/repo`: lists its folders with a `SKILL.md`; an empty search lists all).

Only https addresses without user, query or `#`; a site you added is asked only when you search.

**Add MCP server** (the card at the end of Setup's MCP servers) opens a page laid out like the models page: a list
on the left, the chosen server's page on the right. The page says who made it and how much that is
checked, what it does (with things to ask an agent once it has it), how it runs (each way as a row:
a program through npx, uvx or Docker on this PC, or a web service, with what that means and whether
the program it needs is installed), and what it needs (a folder, an API key, with a link to where
you get the key). Below it, **Set it up**: the fields it needs, the agents that get it, its name,
**Command and settings** (folded), **Test** (asks the server for its tools) and **Add** (writes all
ticked agents at once: one preview, one Undo). No agent is ticked unless you selected one.

- **Recommended**: the reference servers of the MCP project (github.com/modelcontextprotocol/servers:
  Filesystem, Fetch, Git, Memory, Sequential Thinking, Time, Everything) and the official servers of
  well-known makers (GitHub, Playwright, Chrome DevTools, Brave Search, Context7), grouped by what
  they are for. Each package is pinned to its newest version on npm or PyPI when you open it
  (`npx -y pkg@1.2.3`, `uvx pkg==1.2.3`); offline it stays unpinned.
- **On this PC**: programs that offer an MCP server, found by reading the files (nothing is
  started): your own programs (`~/.local/bin`, `~/bin`, extracted apps in `~/Applications`, `/opt`)
  and anything on PATH with "mcp" in its name. A program counts when its own usage text has an
  `mcp` mode (`x-cli mcp [--automation-read-root DIR]`) or it holds the MCP protocol's words; read
  and write folders get a folder that fits the app. AppImages are never run by this scan: **Look
  inside…** unpacks each one's `usr/bin` into a temporary folder, reads it and removes it again.
  Adding a server that lives in an AppImage unpacks the AppImage into
  `~/Applications/<app>-<version>/` first. Servers already set up are not offered again.
  **App updates**: when a newer version of an app turns up (`gridcraft-0.7.0-….AppImage` next to
  `~/Applications/gridcraft-0.6.1/`), a note names it when the window opens; nothing in it runs
  until you click **Look inside…** (a newer file name is no proof of a genuine update). Once looked
  into, only the newest is listed. While agents,
  the list of MCP servers or a link in `~/.local/bin` still start the old version, it shows as
  **Update…**: one preview unpacks the new version, points every one of them at it and moves the
  old folder into the backup (deleted after 7 days; Undo within that time brings everything back).
  **Check for updates** (On this PC) does all of it at once after your click: it looks inside the
  new versions, offers every update in one preview with one backup and one Undo, and then tests
  each updated server (a note names any that does not answer).
- **MCP registry**: a search of the official MCP registry (`registry.modelcontextprotocol.io`; its
  search often takes 10 seconds or more). Each result names its publisher as the registry checked it
  (a GitHub account for `io.github.<user>/…`, a domain for `com.example/…`). npm packages run with
  `npx`, PyPI packages with `uvx`, images with `docker` (of the image's own `docker run`
  arguments only `-e NAME=value`, passed as `-e NAME` with the value in docker's environment),
  remote servers by their https address; the variables a server needs (API keys, folders, also
  inside a value such as `Bearer {api_key}`) become fields, and optional ones left empty are left
  out. A secret field is never put on a command line, where other users of the PC could read it:
  a server that would need that has to be set up by hand.
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

### Skills for an MCP server

A skill written for an MCP server (its folder has the server's name, or its SKILL.md calls the
server's tools as `mcp__<server>`) is listed on that server's page with whether it still matches the
server's tools. After an app update moved its server to a new version, the note says which skills
may need an update. **Update skill…** asks the server for its tools, then Claude Code rewrites the
SKILL.md to match them (`claude -p` with no tools at all: `--tools ""`, no MCP servers, no session
kept; else Codex `codex exec` in its read-only sandbox). The old skill and the tool list go in on
stdin; the answer must be a SKILL.md with the same name (size-capped) and is only shown as a preview,
written after your OK with a backup and Undo. Only your own SKILL.md files under your home folder are
offered; a skill an app installed elsewhere is updated with the app.

## Installing local LLMs

Use a local LLM's card on Setup (once installed, also its right-click menu on the map). Not installed (grey card): **Install…** downloads the official release
into a program folder you choose (one folder per version, a `current` link, the command in
`~/.local/bin`) and sets up a user service, so no password is needed; you also choose the model
folder (the one your other local LLMs use is suggested) and whether it starts at login. Installed
this way: **Update…** (new version folder, the previous one is kept as a fallback), **Model folder…**
(change it later) and **Uninstall…** (program, command, service and settings; never the models).
Installs made by hand are shown but never changed. Every download must match the SHA-256 checksum
its GitHub release lists before it is unpacked or run; a release without one, or a file that does
not match, is not installed.

- **Ollama**: the official release (with ROCm for AMD cards), user service `ollama`.
- **llama.cpp**: the official build of your choice (Vulkan, preselected for AMD unless ROCm is
  installed; ROCm; CUDA; CPU), run as a model router (`llama-server` service: every GGUF in the model
  folder, one loaded at a time, per-model settings in `~/.config/llama.cpp/models.ini`).
- **LM Studio**: not installed or updated from here, since LM Studio publishes no checksum or
  signature for its download; get it from lmstudio.ai. A copy installed by an earlier version of
  AI Agents Map can still be uninstalled and its model folder changed. Its server runs with `lms server`.
- **vLLM**: for NVIDIA cards, in its own Python environment (uv); on AMD cards vLLM needs ROCm
  containers, so it is not offered there.
- **Jan**: its AppImage, unpacked (no FUSE needed), with a desktop entry (`jan-desktop`). For agents,
  Jan's own engine (`jan-llama-worker`, a llama.cpp router) runs without the app as the user service
  `jan` on 127.0.0.1:6767, with the models of Jan's data folder (`~/.local/share/Jan/data` unless
  you choose another one when installing) and Jan's per-model settings
  (`llamacpp/router.preset.ini`). Models are added in the Jan app; its own API server (port 1337)
  counts as Jan too. The engine ends when its input closes, so the service keeps it open
  (`sleep infinity |`); systemd stops both.
- **KoboldCpp**: the official single-file Linux build (Vulkan for AMD and Intel, CUDA for NVIDIA,
  or CPU) as a model router (`koboldcpp` service: `--admin --routermode`). Every GGUF in the model
  folder gets a `.kcpps` in `~/.config/koboldcpp/models` with its context (the best fit for your
  card) and the GPU choice; those file names are the model ids agents ask for
  (`gemma-4-E2B-it-Q4_K_M.kcpps`), loaded on the first request. New models get theirs when the
  window opens or after a download; uninstalling removes the ones AI Agents Map made.

### Agents AI Agents Map installs

Three agents Omarchy's menu does not offer are installed the same way (their card on Setup, ⋯ →
**Install…**, **Update…**, **Uninstall…**; a version folder, the command in `~/.local/bin`):

- **DeepSeek Harness** (`dsh`, developer preview): from npm with install scripts switched off
  (`npm install --ignore-scripts`; needs Node.js). Lines to local LLMs go into its web profile's
  patch `~/.dsh/profiles/web/cordis.patch.yml`, the file its own Settings › Models writes: a
  provider per local LLM (OpenAI Chat Completions, with a placeholder `Authorization: Bearer local`
  header because dsh refuses a request without a key) and, for "Use it now", its default model.
  Only those entries and the MCP servers drawn to it are rewritten; everything else in the file
  stays as written. dsh reads `~/.agents/skills` and `~/.dsh/skills` by itself. Only local LLMs
  are set up; DeepSeek's own API (a key) is set up in dsh. Its web server (`dsh web`) counts as
  the agent being open.
- **Antigravity CLI** (`agy`, Google): the release Google's own install script would take, from
  its release manifest (version, a tarball on `storage.googleapis.com` and its SHA-512); only that
  bucket and a full SHA-512 are accepted, and the tarball is checked before anything is unpacked.
  Google's script itself is not run (its last step only adds `~/.local/bin` to shell profiles).
- **LM Studio Bionic**: not installed or updated from here (no checksum or signature, as LM Studio);
  get it from lmstudio.ai. A copy installed by an earlier version can still be uninstalled. Bionic runs local models
  itself with LM Studio's engine and models, so it is always linked to LM Studio and cannot be
  connected to another local LLM; the watcher never starts LM Studio for it, and stopping a hidden
  LM Studio never touches Bionic's processes. Its global skills live in `~/.lmstudio/skills`
  (shared with LM Studio); its MCP servers are set up in Bionic (Settings › Connected apps).

## Downloading models

Right-click an installed local LLM → **Models…** (or the **Add model** card on Setup). The page is
laid out as LM Studio's model search: a list on the left, the chosen model's page on the right, in
three tabs.

- **Get from Hugging Face**: the trending GGUF chat models, or a search (Hugging Face, or a site you
  added with the same API). Each result shows its author, size, downloads, likes, age and what it
  can do (Vision, Tools, Reasoning, MTP: from its task, tags and chat template, only read). The
  model's page shows its numbers, license, parameters (and active parameters for a mixture of
  experts), architecture, context, base model and its model card as plain text; **What the name
  means** explains the parts of its name (35B-A3B, Q4_K_M, IQ4_XS, UD, MTP, QAT, …). **Download
  options** lists every size with how it fits: in the graphics card, partly in system memory
  (works, slower) or likely too large; the one that fits best is chosen, and **Download** gets it
  into the model folder, with progress (Stop download stops it). LM Studio gets the folder layout
  it expects; a llama.cpp model folder that only holds links (a router folder) gets the file next
  to the originals and a link.
- **Installed**: each model with its size, file, context (now, best for your card, trained for;
  **Set context**) and what its name means. **Remove** asks first and moves the model's files to
  the desktop's trash (restorable); a model that is only a link loses only the link, and it says
  when another local LLM reads the same file through a link. Ollama removes its own models (it has
  no trash), after the same check that the port belongs to you.
- **On this PC**: GGUF files the local LLM does not read yet (the models folder, the other local
  LLMs' folders, `~/Downloads`, `~/Models`); **Add** makes one its model without a second copy (a
  hard link on the same disk, else a symbolic one; LM Studio gets it as `local/<name>/` with its
  vision projector; Ollama imports it with `ollama create`, which keeps its own copy).

For Ollama there is also a field for library names (qwen3:8b); a Hugging Face model is pulled as
hf.co/<repo>:<size>, and Ollama is started for it if it is not running. Before anything is sent to
Ollama on 127.0.0.1, AI Agents Map checks that the listening port belongs to you.

Downloads and installs run in the background: they keep going when you leave the page and show
in a strip at the top of the main page (progress, Stop). A broken connection is tried again after
5 s … 2 min and the download resumes where it stopped. A download or install that was still running
when AI Agents Map ended (closed, crashed, logged out) is listed as interrupted, with **Continue**
or **Discard**; it is kept in `~/.local/state/ai-agents-map/jobs.json` (0600; what to download,
never credentials). **Continue** asks the release again and uses its files and checksums, not the
saved ones; files already downloaded are kept only when their SHA-256 matches.

### Moving and deleting a model on the map

Right-click a model of llama.cpp, KoboldCpp or LM Studio → **Move to…** and pick another of these
local LLMs, or drag the model box onto that local LLM's folder (the folders it may go to are
framed). First it says what happens: how much moves, whether the folder is on another disk (then
the files are copied and the originals removed once the copy is complete) and which agent uses the
model. Only your own regular files inside the model folder move, never a link, and nothing at the
target is overwritten: on one disk the file gets a hard link at the new place before the old name
goes, a copy is written to a new `.part` file and linked into place, and a name that already exists
there stops the move. Undo, in the bar that follows, moves the files back. **Delete…** (at the
bottom of the menu, after a line) asks first and moves the files to the desktop's trash, as
**Remove** on the Installed tab does.

### One models folder, and Reorganize models

Settings › **Models folder** (type it or **Choose…**) is one folder for every local LLM's models;
**Reorganize models…** opens a preview of what would change, and nothing changes until you press
**Reorganize**:

- every GGUF a local LLM knows goes to `<folder>/<publisher>/<model>/` (LM Studio's layout), or
  `local/<name>/` for a file without a publisher; Ollama's store goes to `ollama/`; llama.cpp and
  KoboldCpp installed by AI Agents Map get `llama.cpp/`, a folder of links to every model;
- the local LLMs are pointed there: LM Studio's `downloadsFolder`, and the model folder of those
  AI Agents Map installed (they restart if they run; Ollama is stopped while its store moves). A
  local LLM installed by hand keeps its folder: its links follow the moved files, and a file moved
  out of its folder leaves a link behind;
- **Keep old paths working** (on by default) leaves a link at every old place, for scripts and
  programs that use a model's old path;
- on one disk a move is a rename (instant); to another disk it is a copy with progress, the
  original removed once the copy is complete (refused without enough free space);
- every step is written to `~/.local/state/ai-agents-map/reorganize.json` (0600) first:
  **Undo the last reorganize** puts files, links and settings back.

### Model folders on Setup

Setup shows one card per folder that holds models, with the local LLMs that read it.
- **Add folder…** adds one more folder to look in (Settings `model_folders`); nothing is moved.
- **Change folder…** moves the folder's models to another folder after a question that lists what
  changes: files are moved (a hard link on the same disk, else a checked copy) with Undo; links in
  other local LLMs' model folders that led there are replaced (only links of your own, through a
  fresh random name); `~/.config/llama.cpp/models.ini` is rewritten where it names a moved file (a
  copy kept in `~/.local/state/ai-agents-map/backups/` first, written to a new private temporary
  file and renamed); a local LLM whose own model folder it was is pointed there and restarted if it
  runs.

Loading and unloading from the map (a line drawn from a local LLM to its model, or a loaded model's
line moved) asks only your own server (its listening port must belong to your account): llama.cpp
and Jan `POST /models/load` / `/models/unload`, Ollama `POST /api/generate` with `keep_alive`, LM
Studio `lms load -- <model>` / `lms unload -- <model>` (a model name is one plain word, never an
option). A local LLM that is not running is started first (`systemctl --user start` of its user
service, or `lms server start`).

### Context size

Each installed model shows its context now and the best one for your graphics card ("now 96k ·
best 92k"), calculated from the model file's header: its size, and how much video memory every token
of context takes (KV heads × key and value size × the cache type: f16, q8_0, q4_0). Sliding-window
layers (Gemma) only keep their window and recurrent layers nothing, so those models get much more.
The header is read as untrusted input: a file that claims more than 4096 layers, long per-layer
lists or deeply nested values is skipped (default context) instead of being calculated.
**Set** writes it: for llama.cpp `ctx-size` in the model's section of `~/.config/llama.cpp/models.ini`
(backup and Undo, restart llama.cpp for it to take effect), for Ollama the model's `num_ctx`, for
LM Studio `llm.load.contextLength` in the model's own defaults file
`~/.lmstudio/.internal/user-concrete-model-default-config/<model>.json` (what LM Studio writes when
you change it in its model settings; backup and Undo; used the next time LM Studio loads the model).
For Jan, `ctx-size` in the model's section of Jan's `router.preset.ini` and `ctx_size` in its
`model.yml` (so the Jan app keeps it; backup and Undo; restart Jan's service for it to take
effect); for KoboldCpp, `contextsize` in the model's `.kcpps` (backup and Undo; used the next time
it loads the model).
LM Studio's models are listed by the names its server answers to (`gemma-4-12b-it-qat`), read from
its model index, so agents connected to it ask for a model LM Studio knows. LM Studio loads a model
with 8192 tokens unless told otherwise, too few for an agent's prompt: connecting an agent to an LM
Studio model whose context is not set yet sets the best one in the same step (it is in the preview,
the backup and Undo), and the agent is told the same context. A context you set yourself is kept.

## Privacy and capabilities

- AI Agents Map itself never runs as root and never sees a password. The one exception to "no
  administrator rights" is installing an agent that comes as a system package, on **Install** only,
  after a dialog that shows the exact commands: Omarchy's own installers
  (`omarchy-install-hermes-cli --now`, `omarchy-install-openclaw-cli --now`) or
  `pacman -S --needed <package>` / `pacman -R <package>` through `sudo` (package names checked
  against a strict pattern), run in a terminal window you watch (or a terminal tab in the window),
  where the password is asked. Every program it starts is run as an argv list without a shell (the
  terminal's command line is built with every word quoted), and every program's output and every
  web answer is read with a size limit and one overall deadline.
- **Agents are described as data**: `adapters/<command>.toml` (where each agent keeps its settings,
  sessions, MCP servers and skills, how it is installed and signed in), plus a small
  `adapters/<command>.py` for five agents whose facts a file cannot say (Hermes and OpenCode read
  their own session databases read-only; Pi, DeepSeek Harness and OpenClaw their own settings).
  These come with the plugin; nothing is downloaded or loaded from elsewhere.
- **Reads** (to draw the setup): the agents' settings files, mise's install folders, the pacman
  database, user services (`~/.config/systemd/user`), model folders and this user's own processes
  (`/proc`, own UID only, including their GPU memory from DRM fdinfo), LM Studio's model index
  (`~/.lmstudio/.internal/model-index-cache.json`), the GPU's sysfs values
  (memory, load, temperature, power) and `/proc/net/tcp` (who listens on or is connected to a local
  LLM's port). From settings it takes names, model ids, folders and addresses; it never
  opens credential files (`auth.json`, `.credentials.json`). MCP server entries are read whole so
  they can be copied to another agent; their environment and header values are never shown.
- **Used from outside the map** reads, every 2 s while the window is open and for 1.5 s of that
  (100 ms apart, since a request holds its connection for a moment only), `/proc/net/tcp` and
  `tcp6` (the connections to a running local LLM's port that are established and yours, by their
  uid column; at most 8 MiB each), then `/proc/<pid>/fd` of your own processes to find which one
  holds such a connection, and that process's command line (and its parents' while they are your
  own helpers such as `sh`, `python3` or `curl`) to name it: a plugin by the `name` in its
  `manifest.json` (a plain plugin id only), an agent by its name, else the program's name. Nothing
  of what they send is read. AI Agents Map itself and LLM Monitor (status reads only) are left out.
  At most 16 names per local LLM, each forgotten 30 s after its last request; shown as plain text.
- **The AI Playground button** runs only `~/.config/omarchy/plugins/ubruckhaus.ai-playground/ai-playground`,
  and only when that file is a regular executable of your own (not a link) that no other account may
  change; on other desktops `ai-playground` on `PATH`, owned by you or root, likewise unwritable for
  others. It is started as an argv list without a shell, with `--agent <command>` of the agent selected
  on the map (letters, digits, `-`, `_`, `.` only). Nothing else of that plugin is read or run.
- **ⓘ Information** opens fixed `https://plugins.omarchy.org/plugin.html?id=…` addresses in your
  browser on a click; nothing is fetched by AI Agents Map.
- **LM Studio without its server**: while the LM Studio app runs but nothing of yours listens on its
  port, `lms ps --json` is read (at most every 20 s, 64 KiB, 5 s) to mark the loaded model.
- **Healthy & Security check** reads only the security keys of the agents' settings files named in
  the card (never credential files). **Disk space** walks the agents' program and data folders and
  the local LLMs' program, data and model folders with `lstat` only (no file is opened, links are
  not followed), at most 400 000 entries per folder and 60 s in all, plus `statvfs` of their disks.
- **Working now** reads, every 2 s while the window is open, the CPU time of the running agents'
  own processes (`/proc/<pid>/stat`, your own UID only) and the modification times of their session
  files (no contents).
- **Details pages** (ⓘ on an agent or local LLM card) read usage as numbers and times only:
  Omarchy's usage records (Claude Code, Codex), the token, model, cost, folder, lines-changed,
  parent-session and time columns of Hermes's (`~/.hermes/state.db`) and OpenCode's
  (`~/.local/share/opencode/opencode.db`) session databases and the session id and time of their
  messages (never titles or message content), opened read-only, and the names and dates of Pi's
  session files (never opened). No
  conversation text is read. Whether an agent is signed in is judged from whether its credential
  file exists (never opened) or an API-key variable is set (its name only); Cursor CLI is asked
  with its own `cursor-agent status`. Claude Code's session files (`~/.claude/projects/…/*.jsonl`,
  subagent runs included) are read for numbers only: of model replies the model, token counts, time
  and folder, of its cost summaries lines changed and cost, of a subagent run's `.meta.json` only
  its agent type; your own regular files only (no links), lines over 8 MiB skipped, at most 2 GB per
  run, then on from where it stopped. The numbers are cached owner-only in
  `~/.cache/ai-agents-map/claude-sessions.json`. herdr's live state comes from
  `herdr api snapshot` (per running session), only when that session's socket exists and is
  yours; of its answer (capped at 4 MiB) only the agent, its state, the folder and the workspace
  name are kept, no terminal titles or screen text.
- **Network** (each only on a click, except where noted): GitHub's release API and release
  downloads for Ollama, llama.cpp, Jan and KoboldCpp (each checked against the release's SHA-256;
  every download, models included, goes into a `.part` file that is created new without following
  links, or resumed only when it is your own regular file, and curl writes into that open file),
  the npm registry for DeepSeek Harness, PyPI
  (and `uv`) for vLLM; `huggingface.co` for model search, a model's page (its numbers and GGUF facts) and its model card (`/raw/main/README.md`, at most 200 kB, shown as plain text), and downloads; for Antigravity CLI Google's release manifest (`antigravity-cli-auto-updater-…run.app/manifests/linux_<arch>.json`) and its tarball on `storage.googleapis.com` (checked against the manifest's SHA-512); the local Ollama on
  127.0.0.1 only after checking that the listening socket belongs to you; the same for each running
  local LLM's own API every 2 s while the window is open (which models are loaded) and for
  **Free GPU memory** (unload); answers are capped at 1 MiB and only shown, never run;
  `registry.modelcontextprotocol.io`; `skills.sh`, `api.github.com` and
  `raw.githubusercontent.com` for skills; the update check (automatic when the window opens, at
  most every 6 h): the npm registry's dist-tags of `@anthropic-ai/claude-code`, the release API
  of the local LLMs and agents AI Agents Map installed, and what `mise outdated` asks; a search site you added yourself (Add a site…: https
  only, no redirects, only when you search on it); an MCP server's own address when you click **Test**; and,
  for a newly installed unknown agent (automatic, see above), whatever Claude Code or Codex
  searches. All web requests go through `curl` with https only, at most a few https redirects
  (none for skills and the registry), size and time limits, and request headers handed over on
  stdin, never on the command line.
- **Install, update, uninstall an agent** (its card on Setup, after the dialog with the commands):
  `mise --yes use -g <tool>` / `mise --yes unuse -g <tool>` and `mise --yes uninstall --all <tool>`
  in a terminal you watch (the tool from the plugin's adapters, every word checked), or the system
  package way above.
- **Sign in…** (an agent's card or box): the agent's own sign-in command from its adapter
  (`claude auth login`, `codex login`, `cursor-agent login`, `opencode auth login`, `hermes setup`,
  or the agent itself for its `/login`), in a terminal you watch. An API key you type is checked (one
  word of plain characters) and then either handed on stdin to the agent's own
  `codex login --with-api-key` (its answer read with a 4 KiB cap and one deadline, the key masked in
  it) or written into the `.env` file the agent reads (`~/.gemini/.env`: only that variable
  replaced, written as a new 0600 file with `mkstemp` and moved over the old one, never through a
  link). The key never goes into an argv, a log, a backup or AI Agents Map's own settings.
- **Starts**: `systemctl --user` start/stop/enable/disable/daemon-reload for the services it
  installed or that start a local LLM (service-management); the installers' `curl`, `tar`, the Jan
  AppImage's `--appimage-extract` (after its SHA-256 matched), `uv venv`/`uv pip install`,
  `npm install --ignore-scripts` for DeepSeek Harness (installer);
  the watcher service described above (service-management: `systemctl --user start/stop` of the
  connected LLMs' services, `lms server start/stop`, `lms daemon down`; a hidden LM Studio it
  started is signalled through a pidfd taken when it is found, so a reused PID is never hit);
  `lms unload --all` for **Free GPU memory**;
  `ollama create` when you add a GGUF from this PC to Ollama (the file's path must have no quotes,
  backslashes or control characters);
  `mise outdated --json` for the update check; on **Update** only, in a terminal window of its own
  (`omarchy-launch-floating-terminal-with-presentation`, else `xdg-terminal-exec`): `mise upgrade
  <tool>` or `claude update` (fixed words, the tool name checked against a strict pattern);
  `omarchy-agent-usage-update <agent>` when an agent card's usage is older than ten minutes; an MCP
  server's command when you click **Test** or ask for its tools, or right after you connect an agent
  to it (the check above; in its own process group, stopped afterwards); an AppImage's `--appimage-extract` only on **Look inside…**, when adding a server
  from it, or for a newer version of an app looked into before; `claude -p` or `codex exec` for a new agent and on **Update skill…**; `wl-paste`,
  `lspci`, `hyprctl`; `herdr [--session <name>] api snapshot` while an agent's details page is open
  and a herdr server of yours runs (never starts one), and `herdr agent focus <pane>` on a click on
  a herdr pane (the pane id checked against a strict pattern), then `hyprctl` focuses the window of
  the terminal that runs herdr (found through the parents of your own herdr processes).
- **Removes** a model only on **Remove** and after you confirm it: its files (your own, inside the
  local LLM's model folder, never through a link) go to the desktop's trash with GIO, a link only as
  the link; for Ollama its own `DELETE /api/delete` on 127.0.0.1 after the port-owner check.
- **Stops** an agent started here on **Stop**, on **Stop and close**, or when AI Agents Map ends:
  SIGHUP to its own process group (it was started here as a session of its own), SIGTERM after 3 s
  and SIGKILL after 6 s if it is still there.
- **Writes**, each only after you confirm it with its preview (except AI Agents Map's own state):
  the agents' settings files named above, security settings included (`~/.claude/settings.json`,
  `~/.codex/config.toml`, `~/.copilot/settings.json`, `~/.config/cursor/cli-config.json`,
  `~/.hermes/config.yaml`, `~/.config/opencode/opencode.json`, `~/.pi/agent/settings.json`: only the
  one key changed; always written owner-only, 0600, since they may hold API keys or MCP secrets), links in the
  agents' skills folders, fetched skill files, programs you install into a folder you choose (one
  folder per version, a `current` link, the command in `~/.local/bin`), user services in
  `~/.config/systemd/user`, their settings in `~/.config/<runtime>/`, LM Studio and Jan desktop
  entries, Jan's `settings.json` (its data folder, only on install), `~/.config/llama.cpp/models.ini`,
  LM Studio's per-model defaults, Jan's `router.preset.ini` and `model.yml` (context sizes) and
  KoboldCpp's `.kcpps` files in `~/.config/koboldcpp/models`, and DeepSeek Harness's
  `~/.dsh/profiles/web/cordis.patch.yml` (never its `.credentials.yaml`); model files and links
  in the model folders (Add from this PC: hard or symbolic links; Reorganize models: moves, links
  and the local LLMs' model folder settings, journalled for Undo in
  `~/.local/state/ai-agents-map/reorganize.json`, 0600; only files owned by you are moved, and
  copies to another disk create their file anew, never through an existing link). Generated
  service files and the models folder refuse paths with `%`,
  quotes, `$`, backslashes or control characters. Own state: `~/.config/omarchy/ai-agents-map/`
  (settings, the MCP list, looked-up agents, `providers-with-agents.json` for the watcher,
  `failed-links.json` with the hints of failed connections; private files), the watcher's unit `~/.config/systemd/user/ai-agents-map-providers.service` and its state
  `~/.local/state/ai-agents-map/provider-watch.json`, `~/.local/state/ai-agents-map/backups/`
  (0700), `~/.local/state/ai-agents-map/jobs.json` (interrupted downloads and installs),
  `~/.cache/ai-agents-map/` (scan results, tool lists, `updates.json`).
- Text pasted into its fields is read with `wl-paste` (at most 8 KiB, 2 s deadline) instead of
  GTK's built-in paste, which buffers whatever another app offers with no limit; middle-click paste
  is off and the fields take no drops.
- The PID file lives in `$XDG_RUNTIME_DIR` (or a private 0700 fallback); the popup is only signalled
  after full process identity checks (uid, interpreter, exact script path, kernel start time).

## Languages and settings

**?** (F1), left of the gear, opens **Help**: **Open user manual** (the PDF manual of this version,
`assets/user-manual.pdf`, opened in the desktop's PDF viewer through GTK's file launcher), an animation of setting up by drawing (an agent, a
provider and its model, an MCP server and a skill folder, with made-up boxes), started with
**Start**, **Disk space** (below), and LLM Monitor, the bar plugin that shows what a local LLM is doing (a small
animation of its bar item and panel, its marketplace page and its install command with a copy button).
The animations here and in Settings are drawn in the theme's colours and follow a theme change at once;
LLM Monitor's runs only while it is on screen and stands still when the desktop turns animations off.

**Disk space: agents, local LLMs and models** (in Help, measured in the background when Help opens,
again after 10 minutes or on **Measure again**): its heading sums it up (agents, local LLMs, models,
in all); inside, tiles and a bar split the total into agent programs, agent data, local LLMs and
models, then **Disks** (space for AI and free space per disk), **Agents**, **Local LLMs** and
**Models folders**, each folded with its sum and largest entries. Opened, every entry lists its
folders with size and file count and opens them in the file manager; agents that keep many old mise
versions get a `mise prune` tip. Sizes are space on disk, each file counted once (hard links too),
links not followed; nothing is deleted from here.

The gear (Ctrl+,) opens the settings: **Language** (system or one of English, Deutsch, Français,
Español, Italiano, Português, Nederlands, Polski), **Look** (the colours from the Omarchy theme,
or GNOME's Adwaita with the system's light/dark and accent colour), **Map animations** (agents and other programs, only my agents, off; with a moving preview), **Terminal tabs above the map**, **Corners** (like windows, rounded or square, as
in App Manager and Theme Manager), **Look up new agents**, **Start local LLMs with their agents**, **Too narrow for the map** (what the header's "Maximize for more room" does: move the window to a
free workspace, maximize it where it is, or show no hint; with an animated preview), **In the app
launcher** (the launcher entry; off removes it) and **Agents start in** (the folder for Start). Backspace or Esc goes back. Settings
live in `~/.config/omarchy/ai-agents-map/settings.json`.

## Remove

```sh
omarchy plugin remove ubruckhaus.ai-agents-map
```

This removes the bar button and the window. The launcher entry
(`~/.local/share/applications/org.omarchy.aiagentsmap.desktop`) is ignored by launchers once the
plugin is gone (its `TryExec` points into the plugin); switch off **In the app launcher** first to
delete it, or `rm` it. Switch off **Start local LLMs with their agents**
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
| `ai_popup.py` | The GTK4 app window: cards, map, pages, agents' terminals |
| `settings_pages.py`, `theme_css.py`, `ui_helpers.py` | The window's Settings, Reorganize and Help pages; its theme colours and CSS; small shared drawing helpers |
| `previews.py` | The animated sketches in Settings and Help (drawn with cairo in the theme's colours) |
| `app_entry.py`, `assets/ai-agents-map.svg` | Launcher entry and icon; bringing the window to the front |
| `terminal_pane.py`, `agent_status.py` | An agent's own terminal (VTE); details pages: usage and setup checks |
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
| `agent_activity.py`, `model_names.py` | Which agents are working now; what a model's name means |
| `assets/drawing-demo.gif` | The animation above (rendered from `previews.SetupSketch`) |
| `claude_sessions.py`, `herdr_state.py` | Claude Code's numbers per project and subagent; herdr's live state and pane focus |
| `harnesses.py`, `harness_claude.py`, `harness_opencode.py`, `harness_hermes.py`, `edition.py` | One adapter per agent harness (session numbers per project for now); what this edition may show (numbers only in this plugin) |
| `desktop.py`, `desktop_omarchy.py`, `desktop_generic.py` | Everything desktop-specific behind one interface: Omarchy/Hyprland here; the generic one is used only outside Omarchy (the same code as the stand-alone app Asterism) |
| `ai_core.py`, `palette.py`, `corners.py`, `pidfile.py`, `i18n.py`, `I18n.js` | Shared helpers, theme colours, corners, PID file, languages |
| `tests/` | Unit tests (`python3 -m unittest discover -s tests`) |
