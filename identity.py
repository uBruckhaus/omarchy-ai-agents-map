"""The application's identity: the AI Agents Map Omarchy plugin.

The core modules come from Maprisma (the Flutter app, docs/plan-maprisma-parity.md); they ask this module
for the app's name and folders, so the plugin keeps its own: ~/.config/omarchy/ai-agents-map (the bar
widget reads `language` there), ~/.cache/ai-agents-map, the PID file ai-agents-map.pid.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()


def name() -> str:
    return "AI Agents Map"


def app_id() -> str:
    return "org.omarchy.aiagentsmap"


def short_name() -> str:
    return "ai-agents-map"


def config_dir() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(configured) if configured and Path(configured).is_absolute() else HOME / ".config"
    return root / "omarchy" / short_name()


def cache_dir() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME", "")
    root = Path(configured) if configured and Path(configured).is_absolute() else HOME / ".cache"
    return root / short_name()
