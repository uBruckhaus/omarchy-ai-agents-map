# AI Agents Map

An easy overview of the AI setup on this computer: which coding agents are installed, which LLMs
they use (cloud services and local runtimes) and the models those serve, which MCP servers and
skills each agent has. For people new to it as much as for checking your own setup.

## Two views

- **Overview** (Ctrl+1): one column of cards in four sections. Click a card to open its details.
  - **Agents**: every coding agent Omarchy offers (its Setup → Defaults → Agent list), installed
    ones first with their version, Omarchy's default agent marked. Details: plan, rate limits with
    reset times and today's usage (from Omarchy's own usage data, refreshed when you open the card),
    model, providers (local ones named by runtime), MCP servers, skills, tools, last used, settings
    folder.
  - **Local LLMs**: llama.cpp, Ollama, LM Studio and vLLM when installed: running or stopped, the
    address, the user service that starts it, the models folder and every model it can serve, and
    which agents use it.
  - **MCP servers**: each server with what it runs (a program or a remote host) and which agents
    use it.
  - **Skills**: each skill folder with its skills and the agents that load it (linked skills count
    where they really live, so a shared `~/.agents/skills` shows up once).
- **Map beside the cards**: the popup fills the screen, the cards on the left and the map on the
  right (on a screen narrower than about 1000 px only the cards). A click on a card selects it there (✓) and opens its details, Shift+click
  adds or removes more, a click on the only selected card lets it go; clicking boxes in the map
  selects the matching cards.
- **Map** (Ctrl+2): the same as a picture, drawn at the largest size: MCP servers and skills ←
  agents → LLM providers → models. Hover a box to light up everything it is joined to. A click
  selects a box (an agent, a provider such as llama.cpp, an MCP server, …), Shift+click adds or
  removes more. The category you select in (agents, providers, models, MCP servers and skills) then
  shows only the selected boxes; the other columns keep every box, but only the connections of the
  selection are drawn and the rest fades. Hold Shift to bring the hidden boxes back and add one. Selected boxes are filled and carry a ✓; Esc or **Clear selection** clears it.
  The **Show** toggles above it shorten the map to the categories you are working on (Agents,
  Online providers, Local LLMs, Models, MCP servers, Skills): models need a provider category, MCP
  servers and skills need Agents; empty columns make room for the others. The choice is remembered.
  

The **LLM PROVIDERS ▾** heading opens a list of the online and local providers (with each local
one's state): untick one to leave it and its models out of the map. The heading shows how many are
shown (e.g. 6/9); the choice is remembered.

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

Right-click an installed local LLM → **Models…**: the installed models on the left; on the right
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
(backup and Undo, restart llama.cpp for it to take effect), for Ollama the model's `num_ctx`. LM
Studio keeps its context in its own settings; set it there.

## Connecting by drawing

On the map, press on an agent and drag to a local LLM (or the other way round): the boxes you can
connect to light up, and letting go opens **Connect**. Pick the model, choose whether the agent
should use it right away, and see exactly which lines of the agent's settings change. **Connect**
writes them (the old file is kept in `~/.local/state/ai-agents-map/backups/`); **Undo** in the bar
that follows puts it back. Drawing to a provider the agent already has offers **Update** and
**Remove connection**. To change a connection, click its line: it gets a handle at the LLM end. Drag that
end to another local LLM to move the connection there (the old provider is removed in the same
step), or drop it on empty space to remove the connection; both show the change first.
Right-click a local LLM to **Start** or **Stop** it, or to start it at login (its user service, or
`lms server` for LM Studio; no password needed). Supported now: Hermes (`~/.hermes/config.yaml`), Pi (`~/.pi/agent/`) and
OpenCode (`~/.config/opencode/opencode.json`); speech and embedding models are left out.

## What is read

Nothing is started and nothing is sent. To show the setup, AI Agents Map reads the agents' settings files, mise's install
folders, the pacman database, user services (`~/.config/systemd/user`), model folders and this
user's processes. It takes only names, model ids, folders and local addresses from those files,
never keys, tokens, environment values or headers, and it does not open credential files. The only
outside data is Omarchy's usage record (`omarchy agent usage update`, which Omarchy's own agents
panel runs too); AI Agents Map asks for it when you open an agent card whose record is older than
ten minutes.

## Using the popup

- Click the robot icon in the bar to open or close it. Esc, a click beside the card or switching to
  another window closes it. It always fills the screen; it is not moved or resized.

## Languages and settings

The gear (Ctrl+,) opens the settings: **Language** (system or one of English, Deutsch, Français,
Español, Italiano, Português, Nederlands, Polski) and **Open on** (Overview, Map or the last used
view). Backspace or Esc goes back.

Settings live in `~/.config/omarchy/ai-agents-map/settings.json`.
