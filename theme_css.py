"""AI Agents Map: the window's colours: follows the Omarchy theme and the
desktop's light/dark style (a mixin of ai_popup.AgentsMap, split off to keep
ai_popup.py small)."""

import colorsys
import os
import signal

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

import ai_core
import corners
import desktop
from palette import outside_color


class ThemeCss:
    # --- theme ---------------------------------------------------------------------

    def watch_theme(self):
        # The system's light/dark style and accent colour: Adwaita's look (other desktops, or
        # Settings › Look on Omarchy).
        manager = Adw.StyleManager.get_default()
        for signal in ("notify::dark", "notify::accent-color"):
            manager.connect(signal, lambda *_: GLib.timeout_add(100, self.reload_theme))
        path = str(desktop.theme_dir() or "")
        if not path:
            return
        self.theme_monitor = Gio.File.new_for_path(path).monitor(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.theme_monitor.connect("changed", lambda *_: GLib.timeout_add(300, self.reload_theme))
        parent = Gio.File.new_for_path(os.path.dirname(path))
        self.theme_parent_monitor = parent.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.theme_parent_monitor.connect("changed", lambda *_: GLib.timeout_add(300, self.reload_theme))

    def reload_theme(self):
        if self.theme.reload():
            self.apply_theme_css()
        return False

    def apply_style_scheme(self, background: str):
        """Omarchy: light or dark as its theme's background; other desktops: as the system."""
        manager = Adw.StyleManager.get_default()
        if desktop.NAME != "omarchy" or ai_core.saved_look() == "adwaita":
            manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
            return
        try:
            r, g, b = (int(background.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
            light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5
        except (ValueError, IndexError):
            light = False
        manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if light else Adw.ColorScheme.FORCE_DARK)

    def adw_colors_css(self, bg: str, fg: str, accent: str, red: str, green: str, yellow: str,
                       radius: int = 0) -> str:
        """libadwaita's named colours from the Omarchy theme (other desktops keep their own)."""
        if desktop.NAME != "omarchy":
            return ""
        if ai_core.saved_look() == "adwaita":
            # GNOME's look on Omarchy: Adwaita's own colours and buttons; Hyprland still rounds the window.
            return "window#aiagentsmap-window { border-radius: 0; }\n"
        pairs = {"window-bg-color": bg, "window-fg-color": fg, "view-bg-color": bg, "view-fg-color": fg,
                 "headerbar-bg-color": bg, "headerbar-fg-color": fg, "popover-bg-color": bg,
                 "popover-fg-color": fg, "dialog-bg-color": bg, "dialog-fg-color": fg,
                 "card-bg-color": f"color-mix(in srgb, {fg} 5%, {bg})", "card-fg-color": fg,
                 "sidebar-bg-color": bg, "sidebar-fg-color": fg, "accent-color": accent,
                 "accent-bg-color": accent, "accent-fg-color": bg, "destructive-color": red,
                 "destructive-bg-color": red, "destructive-fg-color": bg, "success-color": green,
                 "success-bg-color": green, "success-fg-color": bg, "warning-color": yellow,
                 "warning-bg-color": yellow, "warning-fg-color": bg, "error-color": red,
                 "error-bg-color": red, "error-fg-color": bg}
        # Omarchy's look as before libadwaita: Hyprland rounds (or not) the window itself, and
        # buttons keep the regular weight (libadwaita makes every button label bold).
        omarchy_look = ("window#aiagentsmap-window { border-radius: 0; }\n"
                        "window#aiagentsmap-window button { font-weight: normal; }\n"
                        # Toasts as the note bar was: the theme's background, an accent border.
                        f"toast {{ background: {bg}; color: {fg}; border: 1px solid {accent}; "
                        f"border-radius: {radius}px; }}\n"
                        f"toast button {{ color: {accent}; }}\n")
        return ":root { " + " ".join(f"--{k}: {v};" for k, v in pairs.items()) + " }\n" + omarchy_look

    def apply_theme_css(self):
        colors = self.theme.colors
        display = Gdk.Display.get_default()
        if self.style_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self.style_provider)
        self.window_rounding = desktop.window_rounding()
        radius = corners.radius(ai_core.saved_corners(), self.window_rounding)
        bg_hex = colors.get("background", "#1e1e2e")
        fg_hex = colors.get("foreground", "#cdd6f4")
        muted_hex = colors.get("muted", "#888894")
        accent_hex = colors.get("accent", "#89b4fa")
        green_hex = colors.get("green", "#a6e3a1")
        yellow_hex = colors.get("yellow", "#f9e2af")
        orange_hex = colors.get("orange", "#fab387")
        red_hex = colors.get("red", "#f04452")
        outside_hex = colors.get("outside") or outside_color(colors)
        # The settings labels' grey in the QML plugins: Qt.darker(foreground, 1.45).
        try:
            r, g, b = (int(fg_hex.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
            hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
            dim_hex = "#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, sat, val / 1.45))
        except (ValueError, IndexError):
            dim_hex = muted_hex
        # Settings › Text in choice boxes: the chosen value in drop-down boxes. Bright: white on a dark
        # background, the theme's text colour on a light one; dimmed: the hint grey (hard to read on
        # some themes).
        try:
            r, g, b = (int(bg_hex.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
            dark_bg = 0.2126 * r + 0.7152 * g + 0.0722 * b <= 0.5
        except (ValueError, IndexError):
            dark_bg = True
        choice_hex = dim_hex if ai_core.setting("choice_text") == "dim" else ("#ffffff" if dark_bg else fg_hex)
        pill = 999 if radius else 0      # chips and switches: round pills, or square with Square corners
        css = f"""
        window#aiagentsmap-window {{ background: {bg_hex}; }}
        .am-card {{ background: {bg_hex}; color: {fg_hex}; border: none;
            border-radius: 0; padding: 17px; }}
        /* Four text styles (fewer formats read easier): the title; controls and values (Map, Setup,
           Open ›, Refresh, "1.3 of 24.0 GB in use", card names); group headings (AI SETUP, GPU MEMORY);
           hints. The header sits outside .am-card in a header bar (Asterism), so these are unscoped. */
        .am-title {{ font-size: 17px; font-weight: 700; }}
        .am-headerbar {{ background: {bg_hex}; color: {fg_hex}; }}
        .am-header-row {{ padding: 8px 17px; min-height: 34px; }}   /* 17 px: the card's padding (AI SETUP) */
        button.am-view-btn label, headerbar button.am-view-btn label, .am-control-text,
        button.am-control-btn label, .am-card button.am-small-btn.am-control-btn label {{ font-size: 13px;
            font-weight: 600; }}
        button.am-view-btn:checked, headerbar button.am-view-btn:checked {{ color: {accent_hex};
            background: alpha({accent_hex}, 0.14); }}
        .am-header-hint {{ font-size: 10px; color: {dim_hex}; }}
        .am-hint {{ color: {muted_hex}; font-size: 13px; }}
        .am-card button.am-header-btn {{ padding: 4px; min-width: 0; min-height: 0; margin-right: 4px; }}
        .am-card button.am-view-btn {{ margin-right: 0; }}
        .am-view-switch {{ margin-right: 4px; }}
        .am-card button.am-gear-btn {{ border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px;
            padding: 1px 6px; margin-right: 0; }}
        .am-gear {{ font-family: "JetBrainsMono Nerd Font", "Symbols Nerd Font", monospace; font-size: 13px; }}
        .am-card button.am-back-bordered {{ border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px;
            padding: 4px 10px; }}
        .am-section-title {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
        .am-tab-legend {{ font-size: 10px; }}
        .am-legend-text {{ font-size: 13px; }}
        .am-card menubutton.am-gear-btn {{ margin-right: 4px; }}
        .am-card menubutton.am-gear-btn > button {{ border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px;
            padding: 1px 6px; min-width: 0; min-height: 0; background: none; box-shadow: none; }}
        .am-card menubutton.am-gear-btn > button:checked {{ color: {accent_hex}; background: alpha({accent_hex}, 0.14); }}
        button.am-legend-help {{ font-size: 13px; padding: 2px 6px; margin-top: 4px; }}
        .am-card .am-tab-working {{ color: {accent_hex}; }}
        .am-card .am-tab-running {{ color: {green_hex}; }}
        .am-card .am-tab-ended {{ color: {muted_hex}; }}
        .am-term-tab {{ border: 1px solid alpha({fg_hex}, 0.2); border-radius: {radius}px; }}
        .am-term-tab-current {{ border-color: {accent_hex}; background: alpha({accent_hex}, 0.14); }}
        .am-term-tab button {{ padding: 2px 8px; min-height: 0; min-width: 0; }}
        .am-tab-dot {{ font-size: 10px; }}
        separator.am-settings-rule {{ background-color: alpha({fg_hex}, 0.12); background-image: none;
            min-height: 1px; margin: 0; }}
        .am-card label.am-line-label {{ font-size: 11px; color: {dim_hex}; }}
        .am-setting-heading {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; color: {fg_hex};
            margin-top: 6px; }}
        .am-settings {{ padding: 2px 4px 12px 0; }}
        .am-line dropdown > button {{ min-height: 0; padding: 2px 8px; border: 1px solid alpha({fg_hex}, 0.35); }}
        .am-line dropdown label {{ font-size: 11px; }}
        .am-line switch {{ min-height: 16px; min-width: 36px; }}
        .am-line switch slider {{ min-width: 14px; min-height: 14px; margin: 1px; }}
        .am-line switch:checked {{ background: {accent_hex}; }}
        .am-empty-icon {{ color: {muted_hex}; opacity: 0.6; }}
        .am-empty-title {{ font-size: 14px; font-weight: 600; }}
        .am-status-hero {{ border-radius: {radius}px; padding: 12px 14px; }}
        .am-status-mark {{ font-size: 22px; font-weight: 700; min-width: 30px; }}
        .am-status-title {{ font-size: 14px; font-weight: 600; }}
        .am-state-ok {{ color: {green_hex}; }}
        .am-state-warn {{ color: {yellow_hex}; }}
        .am-state-missing {{ color: {red_hex}; }}
        .am-state-info {{ color: {dim_hex}; }}
        .am-check-mark {{ font-weight: 700; min-width: 14px; }}
        .am-check-step {{ font-size: 11px; }}
        .am-check-step.am-state-info {{ color: {dim_hex}; }}
        .am-stat-tile {{ border-radius: {radius}px; padding: 8px 10px; }}
        .am-command-line {{ background: alpha({fg_hex}, 0.06); border-radius: {min(radius, 6)}px; padding: 2px 4px 2px 10px;
            margin-top: 3px; }}
        .am-command {{ font-family: "JetBrainsMono Nerd Font", monospace; font-size: 12px; }}
        button.am-widen-hint, headerbar button.am-widen-hint {{ color: {dim_hex};
            border: 1px solid alpha({fg_hex}, 0.18); padding: 1px 8px; min-height: 0; }}
        button.am-widen-hint label, headerbar button.am-widen-hint label {{ font-size: 11px; font-weight: 400;
            color: {dim_hex}; }}
        button.am-widen-hint image, headerbar button.am-widen-hint image {{ color: {dim_hex}; -gtk-icon-size: 12px; }}
        button.am-widen-hint:hover label, button.am-widen-hint:hover image {{ color: {fg_hex}; }}
        .am-undo button.suggested-action {{ background: {accent_hex}; color: {bg_hex}; border: none; }}
        .am-help-card {{ background: alpha({fg_hex}, 0.03); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 8px 12px; }}
        .am-help-card.am-group-card {{ padding: 4px 12px; }}   /* group cards: a low header, to save room */
        .am-help-mark {{ font-weight: 700; }}
        .am-addon-card {{ background: alpha({fg_hex}, 0.04); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 10px 12px; min-height: 96px; }}
        .am-addon-card:hover {{ background: alpha({fg_hex}, 0.07); }}
        .am-addon-suggested {{ border-color: alpha({accent_hex}, 0.7); }}
        .am-addon-refused {{ border-color: alpha({red_hex}, 0.6); }}
        .am-addon-name {{ font-size: 13px; font-weight: 600; }}
        .am-addon-badge {{ font-size: 10px; font-weight: 600; border-radius: {min(radius, 4)}px; padding: 1px 6px; }}
        .am-addon-builtin {{ color: {dim_hex}; background: alpha({fg_hex}, 0.07); }}
        .am-addon-on {{ color: {bg_hex}; background: {accent_hex}; }}
        .am-addon-off {{ color: {fg_hex}; background: alpha({fg_hex}, 0.12); }}
        .am-addon-failed {{ color: {red_hex}; background: alpha({red_hex}, 0.16); }}
        .am-addon-available {{ color: {accent_hex}; border: 1px solid alpha({accent_hex}, 0.5); }}
        .am-setup-tile {{ background: alpha({fg_hex}, 0.04); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 12px 14px; color: {fg_hex}; }}
        .am-setup-absent .am-setup-glyph {{ color: {dim_hex}; }}
        .am-setup-absent {{ background: transparent; border-style: dashed; }}
        .am-setup-absent .am-addon-name, .am-setup-absent .am-agent-detail {{ color: {dim_hex}; }}
        button.am-setup-section {{ padding: 2px 6px; min-height: 0; }}
        button.am-setup-section:hover {{ background: alpha({fg_hex}, 0.08); }}
        /* Setup's categories: group cards (heading and content in one frame). */
        .am-help-card.am-group-card.am-setup-card {{ padding: 4px 10px 8px 4px; }}
        .am-setup-card > button.am-setup-section {{ padding: 4px 6px; }}
        button.am-setup-action {{ min-width: 84px; }}
        .am-setup-explain {{ background: alpha({fg_hex}, 0.04); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 2px 12px 2px 6px; }}   /* low like the group cards */
        .am-step-number {{ min-width: 26px; min-height: 26px; border-radius: 13px; font-size: 13px; font-weight: 700;
            color: {accent_hex}; background: alpha({accent_hex}, 0.16); }}
        .am-step-title {{ font-size: 14px; font-weight: 600; }}
        .am-step-text {{ font-size: 13px; color: {muted_hex}; }}
        .am-agent-detail.am-setup-here {{ color: {green_hex}; }}
        .am-setup-tile:hover {{ background: alpha({fg_hex}, 0.08); }}
        /* Highlighted only when it is installed; a tick (switched on) shows as the ✓ alone. */
        .am-setup-tile:checked {{ background: alpha({fg_hex}, 0.04); color: {fg_hex}; }}
        .am-setup-tile:checked:hover {{ background: alpha({fg_hex}, 0.08); }}
        .am-setup-tile.am-setup-installed {{ background: alpha({accent_hex}, 0.14); border: 2px solid {accent_hex};
            padding: 11px 13px; color: {fg_hex}; }}
        .am-setup-tile.am-setup-installed.am-setup-small {{ padding: 7px 9px; }}
        .am-setup-tile.am-setup-installed:hover {{ background: alpha({accent_hex}, 0.20); }}
        .am-agent-icon.am-setup-glyph {{ font-size: 30px; min-width: 36px; }}
        .am-setup-check {{ color: {bg_hex}; background: {accent_hex}; border-radius: {pill}px; min-width: 20px;
            min-height: 20px; font-size: 11px; font-weight: 700; }}
        .am-setup-title {{ font-size: 22px; font-weight: 700; }}
        .am-setup-small {{ padding: 8px 10px; }}
        .am-agent-icon.am-setup-glyph-sm {{ font-size: 20px; min-width: 24px; }}
        .am-setup-add {{ background: transparent; border: 1px solid alpha({fg_hex}, 0.16); box-shadow: none; }}
        .am-setup-add .am-setup-glyph, .am-setup-add .am-setup-glyph-sm {{ color: {dim_hex}; }}
        .am-setup-add:hover {{ border-color: {accent_hex}; background: alpha({accent_hex}, 0.06); }}
        .am-setup-add:hover .am-setup-glyph, .am-setup-add:hover .am-setup-glyph-sm {{ color: {accent_hex}; }}
        button.am-problem-btn, headerbar button.am-problem-btn {{ color: {accent_hex}; font-weight: 800; font-size: 17px;
            min-width: 26px; }}
        button.am-problem-btn.am-problem-open {{ color: {red_hex}; }}
        .am-setup-problem {{ color: {bg_hex}; background: {red_hex}; border-radius: 9px; min-width: 18px;
            min-height: 18px; font-size: 12px; font-weight: 800; }}
        .am-setup-update {{ color: {bg_hex}; background: {yellow_hex}; border-radius: {pill}px; padding: 1px 8px;
            font-size: 11px; font-weight: 700; }}
        button.am-setup-more {{ min-height: 0; min-width: 0; padding: 0 8px; font-size: 14px; color: {dim_hex};
            border-radius: {radius}px; }}
        button.am-setup-more:hover {{ color: {fg_hex}; background: alpha({fg_hex}, 0.10); }}
        list.am-pick-cards {{ background: transparent; }}
        list.am-pick-cards > row {{ background: alpha({fg_hex}, 0.04); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; margin: 0 2px 6px 2px; padding: 2px 4px; }}
        list.am-pick-cards > row:hover {{ background: alpha({fg_hex}, 0.08); }}
        list.am-pick-cards > row:selected {{ background: alpha({accent_hex}, 0.14); border: 2px solid {accent_hex};
            padding: 1px 3px; }}
        list.am-pick-cards > row.am-pick-heading {{ background: none; border: none; margin-bottom: 0; }}
        .am-agent-icon.am-pick-glyph {{ font-size: 22px; min-width: 30px; }}
        .am-choice.am-choice-card {{ background: alpha({fg_hex}, 0.04); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 8px 10px; margin-bottom: 2px; }}
        .am-choice.am-choice-card:hover {{ background: alpha({fg_hex}, 0.08); }}
        .am-addon-privacy {{ font-size: 10px; color: {dim_hex}; }}
        .am-addon-chip {{ font-size: 11px; min-height: 0; padding: 2px 12px; border-radius: 999px; }}
        .am-addon-chip:checked {{ background: {accent_hex}; color: {bg_hex}; }}
        .am-stat-value {{ font-size: 17px; font-weight: 600; }}
        .am-group-heading {{ font-size: 10px; font-weight: 700; letter-spacing: 1px; color: {dim_hex};
            margin-top: 4px; }}
        .am-agent {{ background: alpha({fg_hex}, 0.05); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 10px 12px; }}
        .am-agent-missing {{ opacity: 0.5; }}
        .am-agent.am-agent-selected {{ background: alpha({accent_hex}, 0.18); border: 2px solid {accent_hex}; }}
        .am-agent-icon.am-status-running {{ color: {green_hex}; }}
        .am-agent-icon.am-status-stopped {{ color: {yellow_hex}; }}
        .am-agent-icon.am-status-missing {{ color: {muted_hex}; }}
        .am-card textview.am-diff, .am-card textview.am-diff text {{ background: alpha({fg_hex}, 0.04);
            color: {fg_hex}; font-size: 11px; }}
        .am-card button.am-primary {{ background: alpha({accent_hex}, 0.25); border-color: {accent_hex}; }}
        .am-card button.am-danger {{ color: {red_hex}; border-color: alpha({red_hex}, 0.6); }}
        popover button.am-danger {{ color: {red_hex}; border-color: alpha({red_hex}, 0.6); }}
        .am-hint-error {{ color: {red_hex}; font-weight: bold; }}
        .am-an-mark {{ font-size: 22px; font-weight: 800; min-width: 28px; color: {dim_hex}; }}
        .am-an-mark.yes {{ color: {green_hex}; }}
        .am-an-mark.no {{ color: {red_hex}; }}
        .am-an-mark.slow {{ color: {yellow_hex}; }}
        .am-model-name {{ font-weight: 600; }}
        .am-help-picture {{ border-radius: {min(radius, 8)}px; }}
        button.am-download-btn {{ padding: 6px 18px; font-weight: 700; }}
        .am-model-title {{ font-size: 17px; font-weight: 700; }}
        .am-model-card {{ background: alpha({fg_hex}, 0.05); border-radius: {min(radius, 8)}px; padding: 12px 14px; }}
        .am-fact-key {{ font-size: 9px; font-weight: 700; letter-spacing: 1px; color: {dim_hex}; }}
        .am-fact-value {{ font-family: monospace; font-weight: 600; }}
        .am-cap {{ font-size: 10px; font-weight: 600; border-radius: {pill}px; padding: 1px 8px;
                   border: 1px solid alpha({fg_hex}, 0.25); }}
        .am-cap-vision {{ color: {yellow_hex}; border-color: alpha({yellow_hex}, 0.5); }}
        .am-cap-tools {{ color: {accent_hex}; border-color: alpha({accent_hex}, 0.5); }}
        .am-cap-reasoning {{ color: {green_hex}; border-color: alpha({green_hex}, 0.5); }}
        .am-cap-mtp {{ color: {orange_hex}; border-color: alpha({orange_hex}, 0.5); }}
        .am-name-part {{ font-family: monospace; font-weight: 700; color: {accent_hex}; }}
        .am-readme {{ font-size: 12px; color: alpha({fg_hex}, 0.85); }}
        /* Add pages, MCP and skills pages: five sizes only. Section heading 11 bold capitals (dim),
           the chosen thing's name 15 bold, names and titles 13 semibold, text and fields 12, hints 11 (dim). */
        /* Add MCP server… / Add skills… (in a bold section header) and the hint area's Update / Show:
           one size, normal weight, grey until hovered. */
        button.am-small-btn.am-quiet-btn {{ font-size: 11px; font-weight: 400; min-height: 0; padding: 2px 10px;
            color: {dim_hex}; }}
        button.am-small-btn.am-quiet-btn label {{ font-size: 11px; font-weight: 400; color: {dim_hex}; }}
        button.am-small-btn.am-quiet-btn image {{ color: {dim_hex}; }}
        button.am-refresh-btn {{ color: {dim_hex}; margin-right: 0; }}
        button.am-refresh-btn:hover {{ color: {fg_hex}; }}
        button.am-small-btn.am-quiet-btn:hover, button.am-small-btn.am-quiet-btn:hover label,
        button.am-small-btn.am-quiet-btn:hover image {{ color: {fg_hex}; }}
        .am-section-heading {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; color: {dim_hex}; }}
        .am-add-page .am-model-title {{ font-size: 15px; font-weight: 700; }}
        .am-add-page label.am-model-name, .am-add-page .am-intro-title,
        .am-add-page .am-choice label.am-detail-value {{ font-size: 13px; font-weight: 600; }}
        .am-add-page label.am-detail-value, .am-add-page row .title, .am-add-page entry, .am-add-page text,
        .am-add-page checkbutton label, .am-add-page dropdown label, .am-add-page expander title label,
        .am-add-page button.link label, .am-add-page textview {{ font-size: 12px; }}
        .am-add-page .am-agent-detail, .am-add-page row .subtitle {{ font-size: 11px; }}
        /* The tabs (Recommended | MCP registry | On this PC | By hand): the page's main choice, so a step bigger. */
        .am-add-page .am-segmented button.am-segment {{ font-size: 13px; padding: 5px 16px; }}
        .am-add-page .am-segmented button.am-segment:checked {{ font-weight: 600; }}
        /* "Search on": a quiet setting, as small as its row (and its open list) */
        .am-add-page .am-line dropdown label, .am-add-page .am-line dropdown popover label {{ font-size: 11px; }}
        .am-add-page .am-line dropdown > button {{ padding: 0 6px; min-height: 0; }}
        .am-add-page .am-line dropdown > button label {{ color: {choice_hex}; }}
        .am-add-page row .subtitle {{ color: {dim_hex}; }}
        .am-add-page button.link {{ padding: 0 4px; min-height: 0; }}
        .am-warning-text {{ color: {yellow_hex}; }}
        row.am-fit-fits .am-agent-detail {{ color: {green_hex}; }}
        row.am-fit-partly .am-agent-detail {{ color: {yellow_hex}; }}
        row.am-fit-too_large .am-agent-detail {{ color: {red_hex}; }}
        .am-hint-warning {{ color: {yellow_hex}; font-weight: bold; }}
        .am-hint-ok {{ color: {green_hex}; font-weight: bold; }}
        .am-undo {{ background: {bg_hex}; border: 1px solid {accent_hex}; border-radius: {radius}px;
            padding: 8px 14px; }}
        popover.am-provider-popover > contents, .am-provider-list {{ padding: 6px; }}
        .am-provider-list checkbutton label {{ font-size: 12px; }}
        .am-provider-list checkbutton check {{ border: 1px solid alpha({fg_hex}, 0.4); background: transparent; }}
        .am-provider-list checkbutton check:checked {{ background: {accent_hex}; border-color: {accent_hex};
            color: {bg_hex}; }}
        .am-provider-list checkbutton check:indeterminate {{ background: alpha({accent_hex}, 0.45);
            border-color: {accent_hex}; color: {bg_hex}; }}
        .am-provider-list checkbutton.am-list-group {{ margin-top: 6px; }}
        .am-provider-list checkbutton.am-list-group label {{ font-size: 11px; font-weight: 700; color: {dim_hex}; }}
        .am-card entry.am-path {{ font-family: monospace; font-size: 12px; min-height: 0; padding: 3px 8px; }}
        .am-card entry.am-path.error {{ border-color: {red_hex}; }}
        button.am-health-line {{ padding: 2px 12px; min-height: 0; border-radius: {radius}px; background: alpha({fg_hex}, 0.04);
                                 border: 1px solid alpha({fg_hex}, 0.10); }}
        .am-health-mark {{ font-size: 15px; font-weight: 700; min-width: 18px; }}
        .am-health-ok {{ color: {green_hex}; }}
        .am-health-hint {{ color: {accent_hex}; }}
        .am-health-warn {{ color: {yellow_hex}; }}
        .am-use-tile {{ padding: 12px; border-radius: {radius}px; }}
        .am-use-icon {{ font-family: "JetBrainsMono Nerd Font", "Symbols Nerd Font", monospace; font-size: 20px;
                        color: {accent_hex}; min-width: 26px; }}
        .am-use-pill {{ font-size: 10px; font-weight: 700; padding: 1px 8px; border-radius: {pill}px; }}
        .am-use-ready {{ color: {green_hex}; background: alpha({green_hex}, 0.14); }}
        .am-use-almost {{ color: {accent_hex}; background: alpha({accent_hex}, 0.14); }}
        .am-use-missing {{ color: {yellow_hex}; background: alpha({yellow_hex}, 0.14); }}
        .am-use-have {{ font-size: 11px; color: {green_hex}; }}
        .am-finding-mark {{ font-size: 13px; font-weight: 700; min-width: 16px; }}
        .am-finding-title {{ font-size: 12px; font-weight: 600; }}
        .am-state-hint {{ color: {accent_hex}; }}
        .am-job {{ padding: 6px 10px; border-radius: {radius}px; background: alpha({fg_hex}, 0.04);
                   border: 1px solid alpha({fg_hex}, 0.10); }}
        .am-job-mark {{ font-size: 16px; min-width: 22px; color: {accent_hex}; }}
        .am-job-waiting .am-job-mark, .am-job-interrupted .am-job-mark, .am-job-stopped .am-job-mark {{ color: {yellow_hex}; }}
        .am-job-failed .am-job-mark {{ color: {red_hex}; }}
        .am-job-done .am-job-mark {{ color: {green_hex}; }}
        progressbar.am-progress trough {{ min-height: 6px; background: alpha({fg_hex}, 0.10); border-radius: {min(radius, 3)}px; }}
        progressbar.am-progress progress {{ min-height: 6px; background: {accent_hex}; border-radius: {min(radius, 3)}px; }}
        .am-card button.am-menu-item {{ padding: 3px 8px; font-size: 12px; }}
        popover.am-menu > contents {{ border: 1px solid alpha({fg_hex}, 0.30); border-radius: {radius}px;
            padding: 6px; }}
        popover.am-menu button.am-menu-item {{ padding: 4px 10px; font-size: 12px; font-weight: normal;
            min-height: 0; border-radius: {min(radius, 6)}px; }}
        popover.am-menu checkbutton.am-menu-check {{ padding: 2px 6px; min-height: 0; }}
        popover.am-menu checkbutton.am-menu-check label {{ font-size: 12px; }}
        popover.am-menu .am-menu-title {{ padding: 2px 10px 2px 10px; }}
        popover.am-menu .am-menu-note {{ padding: 2px 10px; }}
        popover.am-menu separator.am-menu-rule {{ background-color: alpha({fg_hex}, 0.14); background-image: none;
            min-height: 1px; margin: 4px 2px; }}
        .am-form-list row .title {{ min-width: 110px; }}
        /* A combo row's chosen value (its inline list): the size and colour of the subtitle, not of the title. */
        row.combo listview.inline label {{ font-size: smaller; color: {choice_hex}; }}
        row.combo popover listview label {{ font-size: smaller; }}
        /* The chosen value and its arrow framed like the other controls, well apart from a long explanation. */
        row.combo listview.inline {{ min-width: 128px; }}    /* the chosen value stays readable; the explanation wraps */
        row.combo box.suffixes {{ margin-left: 24px; padding: 3px 6px 3px 10px; border: 1px solid alpha({fg_hex}, 0.28);
            border-radius: {min(radius, 8)}px; }}
        row.combo:hover box.suffixes {{ border-color: alpha({fg_hex}, 0.45); }}
        .am-card list.am-list:not(.boxed-list):not(.am-pick-cards) {{ background: alpha({fg_hex}, 0.03); border-radius: {radius}px; }}
        .am-card list.am-list row:selected {{ background: alpha({accent_hex}, 0.22); }}
        .am-card-check {{ color: {accent_hex}; font-weight: 700; }}
        /* Omarchy's default agent: its "Default" badge says so; no border that looks like a selection. */
        .am-agent-icon {{ font-family: "JetBrainsMono Nerd Font", "Symbols Nerd Font", monospace;
            font-size: 22px; color: {accent_hex}; min-width: 28px; }}
        .am-agent-icon.am-agent-icon-omarchy {{ font-family: "omarchy"; }}
        .am-agent-missing .am-agent-icon {{ color: {fg_hex}; }}
        image.am-icon {{ min-width: 0; }}
        .am-agent-name {{ font-size: 13px; font-weight: 600; }}
        .am-agent-detail {{ font-size: 11px; color: {dim_hex}; }}
        .am-agent-badge {{ font-size: 10px; font-weight: 600; color: {bg_hex}; background: {accent_hex};
            border-radius: {min(radius, 4)}px; padding: 0 5px; }}
        .am-risk-badge {{ font-size: 10px; font-weight: 600; border-radius: {min(radius, 4)}px; padding: 1px 6px; }}
        .am-risk-high {{ color: {red_hex}; background: alpha({red_hex}, 0.16); }}
        .am-risk-medium {{ color: {yellow_hex}; background: alpha({yellow_hex}, 0.16); }}
        .am-risk-low {{ color: {dim_hex}; background: alpha({fg_hex}, 0.07); }}
        .am-risk-ok {{ color: {green_hex}; background: alpha({green_hex}, 0.14); }}
        .am-space-agents {{ color: {accent_hex}; }}
        .am-space-agent_data {{ color: {green_hex}; }}
        .am-space-runtimes {{ color: {yellow_hex}; }}
        .am-space-models {{ color: {outside_hex}; }}
        .am-space-other {{ color: {muted_hex}; }}
        .am-space-free {{ color: {dim_hex}; }}
        .am-agent {{ padding: 0; }}
        .am-agent > box:first-child {{ padding: 10px 12px; }}
        .am-agent-chevron {{ color: {muted_hex}; }}
        .am-agent-details {{ padding: 2px 12px 12px 50px; }}
        .am-card label.am-detail-value {{ font-size: 12px; }}
        levelbar.am-limit trough {{ min-height: 5px; background: alpha({fg_hex}, 0.10); border: none;
            border-radius: {min(radius, 3)}px; padding: 0; }}
        levelbar.am-limit block {{ min-height: 5px; border-radius: {min(radius, 3)}px; border: none; }}
        levelbar.am-limit block.filled {{ background: {accent_hex}; }}
        levelbar.am-limit block.am-ok {{ background: {green_hex}; }}
        levelbar.am-limit block.am-warn {{ background: {yellow_hex}; }}
        levelbar.am-limit block.am-hot {{ background: {orange_hex}; }}
        levelbar.am-limit block.am-full {{ background: {red_hex}; }}
        levelbar.am-limit block.empty {{ background: transparent; }}
        .am-card button.am-small-btn {{ font-size: 11px; min-height: 0; padding: 1px 10px;
            border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px; }}
        .am-card button.am-action-btn {{ font-size: 13px; font-weight: 600; min-height: 34px; min-width: 120px;
            padding: 4px 22px; border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px; }}
        .am-card button.am-action-btn:disabled {{ opacity: 0.45; }}
        .am-card button.am-chip {{ font-size: 11px; min-height: 0; padding: 1px 10px; border-radius: {pill}px;
            border: 1px solid alpha({fg_hex}, 0.25); background: transparent; color: {dim_hex}; }}
        .am-card button.am-chip:checked {{ background: alpha({accent_hex}, 0.18); border-color: {accent_hex};
            color: {fg_hex}; }}
        .am-card button.am-chip:disabled {{ opacity: 0.4; }}
        .boxed-list .am-agent {{ background: none; border: none; border-radius: 0; }}
        .boxed-list .am-agent.am-agent-selected {{ background: alpha({accent_hex}, 0.18);
            box-shadow: inset 3px 0 {accent_hex}; }}
        .am-hint-area {{ border-bottom: 1px solid alpha({fg_hex}, 0.10); padding: 4px 2px 8px 2px; }}
        .am-segmented {{ border: 1px solid alpha({fg_hex}, 0.22); border-radius: {pill}px; padding: 2px; }}
        .am-card button.am-segment {{ font-size: 11px; min-height: 0; padding: 2px 12px; border-radius: {pill}px;
            border: none; background: none; box-shadow: none; color: {dim_hex}; }}
        .am-card button.am-segment:checked {{ background: alpha({accent_hex}, 0.22); color: {fg_hex}; }}
        .am-card button.am-segment:hover:not(:checked) {{ color: {fg_hex}; }}
        .am-card button.am-filter-btn {{ font-size: 11px; }}
        .am-card .am-leader {{ min-height: 0; border-bottom: 1px dotted alpha({fg_hex}, 0.35); margin: 0 4px; }}
        .am-card .am-choice {{ padding: 4px 6px; border-radius: {radius}px; }}
        .am-card .am-choice:hover {{ background: alpha({fg_hex}, 0.05); }}
        .am-card .am-choice:hover .am-leader {{ border-bottom-color: alpha({accent_hex}, 0.8); }}
        .am-canvas {{ border: 1px dashed alpha({fg_hex}, 0.25); border-radius: {radius}px; padding: 12px; }}
        /* The chess page: a card per kind of piece above and below the board, the agent's words. */
        .am-chess-card {{ background: alpha({fg_hex}, 0.04); border: 1px solid alpha({fg_hex}, 0.12);
            border-radius: {radius}px; padding: 4px 2px; }}
        button.am-chess-card:hover {{ background: alpha({fg_hex}, 0.09); }}
        .am-chess-card.am-chess-picked {{ background: alpha({accent_hex}, 0.16); border: 2px solid {accent_hex}; }}
        .am-chess-card.am-chess-gone {{ opacity: 0.4; }}
        .am-chess-glyph {{ font-family: "DejaVu Sans", "Noto Sans Symbols 2", sans-serif; font-size: 34px; }}
        .am-chess-white {{ color: #f7f5ee; text-shadow: 0 0 1px #1e1e24, 0 0 2px #1e1e24; }}
        .am-chess-black {{ color: #19191e; text-shadow: 0 0 1px #d8d6cc, 0 0 2px #d8d6cc; }}
        .am-chess-count {{ font-size: 11px; color: {dim_hex}; }}
        .am-chess-glyph.am-chess-taken-glyph {{ font-size: 24px; margin: -3px 0; }}
        .am-chess-answer {{ background: alpha({accent_hex}, 0.08); border-left: 3px solid {accent_hex};
            border-radius: {radius}px; padding: 8px 10px; }}
        .am-chess-answer-title {{ font-weight: 700; font-size: 12px; }}
        .am-chess-status {{ font-size: 13px; font-weight: 600; }}
        .am-chess-said {{ font-size: 13px; }}
        /* Its dropdowns as the other pages' (settings lines): small, not bold, in the choice text colour. */
        .am-chess-controls dropdown > button {{ min-height: 0; padding: 2px 8px; border: 1px solid alpha({fg_hex}, 0.35); }}
        .am-chess-controls dropdown label {{ font-size: 11px; font-weight: normal; color: {choice_hex}; }}
        .am-chess-controls > label {{ font-size: 12px; }}
        /* The Board and pieces menu: preview tiles, the chosen one framed in the accent colour. */
        button.am-look-tile {{ padding: 4px; border: 2px solid transparent; border-radius: {radius}px; background: none; }}
        button.am-look-tile:hover {{ background: alpha({fg_hex}, 0.06); }}
        button.am-look-tile:checked {{ border-color: {accent_hex}; background: alpha({accent_hex}, 0.12); }}
        .am-look-name {{ font-size: 11px; }}
        /* AI Playground's chess tab: what can be dropped on it, and the frame while something is dragged over it. */
        .am-warning-text {{ color: {yellow_hex}; font-size: 12px; }}
        .am-drop-hint {{ padding: 6px 10px; border: 1px dashed alpha({fg_hex}, 0.30); border-radius: {radius}px;
            color: {dim_hex}; font-size: 12px; }}
        .am-import-bar {{ padding: 8px 12px; border: 1px solid {accent_hex}; border-radius: {radius}px;
            background: alpha({accent_hex}, 0.10); }}
        .am-drop-zone.am-drop-active {{ box-shadow: inset 0 0 0 2px {accent_hex}; background: alpha({accent_hex}, 0.06);
            border-radius: {radius}px; }}    /* "Opponent", "compared with": as small as the row */
        button.am-chess-promote {{ font-family: "DejaVu Sans", "Noto Sans Symbols 2", sans-serif; font-size: 28px;
            padding: 2px 8px; }}
        /* AI Playground's question fields. */
        textview.am-prompt {{ border: 1px solid alpha({fg_hex}, 0.25); border-radius: {radius}px; }}
        textview.am-prompt:focus-within {{ border-color: {accent_hex}; }}
        """
        import more_plugins                  # the Information page's cards (info_page, AI Playground)
        css += more_plugins.css("am", fg_hex, dim_hex, accent_hex, radius)
        for view in (getattr(self, "map_view", None), getattr(self, "side_map", None)):
            if view is not None:
                view.set_palette({"bg": bg_hex, "fg": fg_hex, "dim": dim_hex, "muted": muted_hex,
                                  "accent": accent_hex, "green": green_hex, "yellow": yellow_hex, "red": red_hex,
                                  "outside": outside_hex, "hook": orange_hex},
                                 radius)
        self.apply_style_scheme(bg_hex)
        css = self.adw_colors_css(bg_hex, fg_hex, accent_hex, red_hex, green_hex, yellow_hex, radius) + css
        if radius == 0:
            # Square: libadwaita's own controls too (lists, dropdowns, entries, switches, popovers).
            css += "window#aiagentsmap-window *:not(radio):not(.am-tab-dot) { border-radius: 0; }\n"
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        import icons
        icons.setup(display)              # the app's own icons (icons/Asterism), for this process only
        self.style_provider = provider
