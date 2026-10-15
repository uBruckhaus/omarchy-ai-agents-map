# AI Manager

An Omarchy bar widget that opens a popup with two views:

- **AI Manager** (Ctrl+1): the overview.
- **Builder** (Ctrl+2): the builder canvas.

Work in progress: this is the canvas (layout, views, settings), no content yet.

## Using the popup

- Click the robot icon in the bar to open or close it. Esc closes it.
- Drag the header to move the card, drag the grip in the bottom-right corner to resize it.
  Position and size are remembered.
- Double-click the header: at the default size it maximizes, at the largest size it goes back to
  the default; in between a left double-click restores the default size and a right double-click
  maximizes. Double-click the grip for the default size.

## Languages and settings

The gear (Ctrl+,) opens the settings: **Language** (system or one of English, Deutsch, Français,
Español, Italiano, Português, Nederlands, Polski) and **Open on** (AI Manager view, Builder view or
the last used view). Backspace or Esc goes back.

Settings live in `~/.config/omarchy/ai-manager/settings.json`.
