# AI Manager

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
- **Map** (Ctrl+2): the same as a picture, drawn at the largest size: MCP servers and skills ←
  agents → LLM providers → models. Hover a box to light up everything it is joined to. Leaving the
  Map gives the card its size back.

## What is read

Nothing is started and nothing is sent: AI Manager reads the agents' settings files, mise's install
folders, the pacman database, user services (`~/.config/systemd/user`), model folders and this
user's processes. It takes only names, model ids, folders and local addresses from those files,
never keys, tokens, environment values or headers, and it does not open credential files. The only
outside data is Omarchy's usage record (`omarchy agent usage update`, which Omarchy's own agents
panel runs too); AI Manager asks for it when you open an agent card whose record is older than
ten minutes.

## Using the popup

- Click the robot icon in the bar to open or close it. Esc, a click beside the card or switching to
  another window closes it.
- Drag the header to move the card, drag the grip in the bottom-right corner to resize it.
  Position and size are remembered.
- Double-click the header: at the default size it maximizes, at the largest size it goes back to
  the default; in between a left double-click restores the default size and a right double-click
  maximizes. Double-click the grip for the default size.

## Languages and settings

The gear (Ctrl+,) opens the settings: **Language** (system or one of English, Deutsch, Français,
Español, Italiano, Português, Nederlands, Polski) and **Open on** (Overview, Map or the last used
view). Backspace or Esc goes back.

Settings live in `~/.config/omarchy/ai-manager/settings.json`.
