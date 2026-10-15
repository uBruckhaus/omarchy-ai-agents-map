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
  

## What is read

Nothing is started and nothing is sent: AI Agents Map reads the agents' settings files, mise's install
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

Settings live in `~/.config/omarchy/ai-manager/settings.json`.
