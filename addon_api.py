"""The add-on API (Maprisma's, shared by the AI Agents Map plugin: the same signed add-ons load in both).

An add-on's addon.py defines register(api); `api` is an AddonAPI. Everything an add-on may do goes
through it, so the app's internals can change without breaking add-ons. API_VERSION changes only on
a breaking change; additions keep it.

Hook points (the only places an add-on appears):
    api.add_session_titles(command, reader)       reader() -> {session id: title}   (TITLES)
    api.add_details_section(builder, privacy, card="topics", page="agent")
                                                  a section in the details card `card` ("statistics",
                                                  "topics" (agents only), "health" or a key of its own: a
                                                  card it names with its first ui.heading); page "agent":
                                                  builder(ui, box, harness, sessions, since); page "llm"
                                                  (a local LLM's page): builder(ui, box, provider, runtime)
    api.add_help_card(builder)                    builder(ui, box)
    api.add_settings_group(builder)               builder(ui, box)
    api.add_harness(api.Harness(...))             a new agent: cards, map, details     ("agent")
    api.add_provider(api.Provider(...))           a new local LLM or online provider    ("provider")
    api.extend_harness(command, **fields)         facts added to an existing agent      (kind "extension",
                                                  the command in its "extends"; harnesses.EXTENDABLE
                                                  fields only, used where the agent left them empty)
    api.extend_provider(key, **fields)            facts added to an existing provider   (kind "extension",
                                                  the key in its "extends"; providers.EXTENDABLE fields)

Agents and providers need the capability in quotes declared in addon.json ("capabilities"), and
one that brings an installer also "installer". A built-in id cannot be replaced, and an add-on's
agent cannot bring an updater of its own (agent_updates only runs built-in updaters).

An add-on may never do more than the privacy level its addon.json declares (edition.NUMBERS,
TITLES or SUMMARY); registering a hook above it raises AddonError. Callbacks run inside try/except:
an add-on that fails is reported in Settings, the page goes on without it.
"""

from __future__ import annotations

import re
import threading
import traceback
from dataclasses import dataclass, field

import edition as _edition
import harnesses

API_VERSION = 1
PRIVACY = {"numbers": _edition.NUMBERS, "titles": _edition.TITLES, "summary": _edition.SUMMARY}
CAPABILITIES = ("agent", "provider", "installer", "updater")


class AddonError(Exception):
    pass


@dataclass
class Hooks:
    titles: dict = field(default_factory=dict)        # command -> [(addon id, reader)]
    details: list = field(default_factory=list)       # [(addon id, builder, privacy, card, page)]
    help: list = field(default_factory=list)          # [(addon id, builder)]
    settings: list = field(default_factory=list)      # [(addon id, builder)]
    privacy: dict = field(default_factory=dict)       # addon id -> level of a loaded add-on
    errors: dict = field(default_factory=dict)        # addon id -> last error text

    def clear(self):
        self.__init__()


HOOKS = Hooks()


def loaded_privacy() -> int:
    """The highest privacy level of the add-ons loaded now (0 without any)."""
    return max(HOOKS.privacy.values(), default=0)


def failed(addon_id: str, error: BaseException | str, hooks: Hooks | None = None):
    text = error if isinstance(error, str) else "".join(traceback.format_exception_only(type(error), error)).strip()
    (hooks or HOOKS).errors[addon_id] = text[:500]


def call(addon_id: str, fn, *args):
    """Run an add-on's callback; a failure is noted, never raised."""
    try:
        return fn(*args)
    except Exception as error:          # an add-on's bug must not break the page
        failed(addon_id, error)
        return None


class AddonAPI:
    """What register(api) gets: one per add-on, knowing its id, privacy and translations."""

    version = API_VERSION
    NUMBERS, TITLES, SUMMARY = _edition.NUMBERS, _edition.TITLES, _edition.SUMMARY

    def __init__(self, addon_id: str, privacy: int, strings: dict | None = None, hooks: Hooks = HOOKS,
                 capabilities=(), kind: str = "feature", extends=(), icon: str = ""):
        self.addon_id = addon_id
        self.privacy = privacy
        self.capabilities = frozenset(capabilities)
        self.kind = kind
        self.extends = frozenset(extends)          # agent commands an extension add-on may add to
        self.icon = icon                           # its icon.svg as an icon name, or "" (icons.py)
        self._strings = strings or {}       # language -> {English text: translation}
        self._hooks = hooks

    # --- what the add-on may read ---
    @property
    def edition(self) -> int:
        return _edition.maximum()

    @property
    def harnesses(self):
        return harnesses

    def session_titles(self, command: str) -> dict:
        """Titles other add-ons provide for a harness (merged)."""
        found = {}
        for addon_id, reader in self._hooks.titles.get(command, []):
            result = call(addon_id, reader)
            if isinstance(result, dict):
                found.update({str(k): str(v) for k, v in result.items()})
        return found

    def tr(self, text: str) -> str:
        """The add-on's own translation of `text` in the app's language (English when missing)."""
        import i18n
        found = self._strings.get(i18n.current(), {}).get(text, text)
        return found if isinstance(found, str) else text

    def trn(self, one: str, other: str, n: int) -> str:
        """Plural, as the app's ngettext: the forms under "\\x04" + `one` in the add-on's tables."""
        import i18n
        lang = i18n.current()
        return i18n.pick_plural(lang, self._strings.get(lang, {}).get(i18n.PLURAL_PREFIX + one), n, one, other)

    # --- helpers an add-on may use (bounded, owner-only, read-only where it reads) ---
    @staticmethod
    def run_bounded(argv: list, max_bytes: int = 64 * 1024, timeout: float = 5.0, stdin: bytes | None = None,
                    cwd: str | None = None) -> tuple:
        """(output bytes, ok): no shell, output capped, one deadline (ai_core.read_bounded_output)."""
        import ai_core
        return ai_core.read_bounded_output(list(argv), max_bytes=max_bytes, timeout=timeout, stdin=stdin, cwd=cwd)

    @staticmethod
    def atomic_write(path, text: str) -> None:
        """Write a file owner-only in one step (ai_core.atomic_write)."""
        import ai_core
        ai_core.atomic_write(path, text)

    @staticmethod
    def open_readonly(path):
        """A harness's SQLite database read-only (never created or changed), or None."""
        from pathlib import Path
        return harnesses.connect(Path(path))

    def cache_dir(self):
        """The add-on's own cache folder (owner-only), created on first use."""
        import os
        from pathlib import Path
        folder = Path.home() / ".cache/ai-agents-map/addons" / self.addon_id
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        return folder

    @property
    def claude_projects(self):
        """Where Claude Code keeps its session files (~/.claude/projects, or CLAUDE_CONFIG_DIR's)."""
        import claude_sessions
        return claude_sessions.ROOT

    # --- hook points ---
    def _allow(self, level: int, what: str):
        if level > self.privacy:
            raise AddonError(f"{what} needs a higher privacy level than {self.addon_id} declares")

    def add_session_titles(self, command: str, reader):
        self._allow(_edition.TITLES, "add_session_titles")
        self._hooks.titles.setdefault(command, []).append((self.addon_id, reader))

    CARDS = ("statistics", "topics", "health")      # the details page's cards an add-on can add to

    PAGES = ("agent", "llm")                     # agents' details pages, local LLMs' details pages

    def add_details_section(self, builder, privacy: int = _edition.NUMBERS, card: str = "topics",
                            page: str = "agent"):
        """A section on details pages (`page`: "agent" or "llm"), in the card `card`: one of CARDS
        ("topics" on agents' pages only), or a key of the add-on's own ("[a-z0-9-]", a card of its
        own that its first ui.heading names)."""
        self._allow(privacy, "add_details_section")
        if not isinstance(card, str) or not card or len(card) > 40 or \
                not all(c.islower() or c.isdigit() or c == "-" for c in card):
            raise AddonError("card must be a short key like \"statistics\"")
        if page not in self.PAGES:
            raise AddonError("page must be \"agent\" or \"llm\"")
        if page == "llm" and card == "topics":
            raise AddonError("Topics is on agents' pages only")
        self._hooks.details.append((self.addon_id, builder, privacy, card, page))

    def add_help_card(self, builder):
        self._hooks.help.append((self.addon_id, builder))

    def add_settings_group(self, builder):
        self._hooks.settings.append((self.addon_id, builder))

    # --- new agents and providers ---
    from harnesses import Download, Harness, LocalLlm, Mcp, Sessions, Skills, Way   # noqa: E402  (classes for add-ons)
    from providers import Provider                                 # noqa: E402

    @staticmethod
    def ref(module: str, name: str):
        """harnesses.ref: a function of another module, imported when first called."""
        return harnesses.ref(module, name)

    @property
    def Runtime(self):
        """inventory.Runtime: what a local provider's detect(processes) returns."""
        import inventory
        return inventory.Runtime

    def _need(self, capability: str, what: str):
        if capability not in self.capabilities:
            raise AddonError(f"{what} needs \"{capability}\" in {self.addon_id}'s capabilities")

    @staticmethod
    def _check_commands(download, ways, self_update):
        """What an add-on declares to install, update and remove its program is data the app runs:
        it must be of the expected form (install_ctl.check_download; plain words, as
        install_recipes runs them)."""
        import install_ctl
        import install_recipes
        if download is not None:
            try:
                install_ctl.check_download(download)
            except (ValueError, re.error) as error:
                raise AddonError(f"download: {error}") from error
        for way in ways or ():
            for argv in (way.argv, way.update):
                if argv and install_recipes._plain(argv) is None:
                    raise AddonError(f"{' '.join(map(str, argv))[:80]}: only plain words can run")
        if self_update and install_recipes._plain(self_update) is None:
            raise AddonError("self_update: only plain words can run")

    def add_harness(self, harness):
        self._need("agent", "add_harness")
        if harness.installer is not None or harness.download is not None:
            self._need("installer", "an agent with an installer")
        self._check_commands(harness.download, harness.install, harness.self_update)
        if self.icon and not harness.icon:
            # The add-on's own icon.svg for its agent (cards and its box on the map).
            import dataclasses
            harness = dataclasses.replace(harness, icon=self.icon, icon_font="icon")
        updates = bool(harness.self_update or harness.latest is not None)
        if updates:
            self._need("updater", "an agent with an updater of its own")
        harnesses.add(harness, self.addon_id, updates=updates)

    def extend_harness(self, command: str, **fields):
        """Facts for an agent another add-on (or the app) brings: only for an extension add-on and
        an agent its addon.json "extends"; only harnesses.EXTENDABLE fields."""
        if self.kind != "extension":
            raise AddonError("extend_harness needs kind \"extension\"")
        if command not in self.extends:
            raise AddonError(f"{command} is not in {self.addon_id}'s \"extends\"")
        refused = harnesses.extend(command, fields, self.addon_id)
        if refused:
            raise AddonError(f"cannot extend {', '.join(sorted(refused))}")

    def extend_provider(self, key: str, **fields):
        """Facts for a provider another add-on (or the app) brings, as extend_harness for agents."""
        if self.kind != "extension":
            raise AddonError("extend_provider needs kind \"extension\"")
        if key not in self.extends:
            raise AddonError(f"{key} is not in {self.addon_id}'s \"extends\"")
        import providers
        refused = providers.extend(key, fields, self.addon_id)
        if refused:
            raise AddonError(f"cannot extend {', '.join(sorted(refused))}")

    def add_provider(self, provider):
        self._need("provider", "add_provider")
        if provider.installer is not None or provider.download is not None:
            self._need("installer", "a provider with an installer")
        self._check_commands(provider.download, provider.install, ())
        import providers
        providers.add(provider, self.addon_id)


class UI:
    """The few widgets and helpers add-ons build with (a stable face over the app)."""

    def __init__(self, app, addon_id: str = ""):
        self._app = app
        self.addon_id = addon_id

    def heading(self, box, text: str):
        """A heading. The first one inside a card the add-on fills (Topics on a details page) names
        that card instead."""
        if getattr(self._app, "name_group", None) is not None and self._app.name_group(box, text):
            return
        self._app.settings_heading(box, text)

    def boxed_list(self):
        return self._app.boxed_list()

    @staticmethod
    def box(spacing: int = 8):
        """A vertical box to put widgets in (e.g. a section filled once its data is read)."""
        from gi.repository import Gtk
        return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)

    def plain_row(self, title: str, subtitle: str = ""):
        return self._app.plain_row(title, subtitle)

    def share_row(self, title: str, subtitle: str, value: str, share: float):
        """A row with `value` and a bar filled to `share` (0..1) at its right: one part of a whole."""
        from gi.repository import Adw, Gtk
        row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
        side = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
        text = Gtk.Label(label=value, xalign=1)
        text.add_css_class("dim-label")
        text.add_css_class("numeric")
        bar = Gtk.LevelBar(min_value=0, max_value=1, value=max(0.0, min(1.0, share)), valign=Gtk.Align.CENTER)
        bar.set_size_request(90, -1)
        for name in ("low", "high", "full"):          # one colour: a share, not a good or bad level
            bar.remove_offset_value(name)
        side.append(text)
        side.append(bar)
        row.add_suffix(side)
        return row

    def expander_row(self, title: str, subtitle: str = ""):
        from gi.repository import Adw
        return Adw.ExpanderRow(title=title, subtitle=subtitle, use_markup=False)

    def note(self, box, text: str):
        self._app.note(box, text)

    def label(self, text: str = "", selectable: bool = True, card: bool = False):
        """Plain text (never markup); card=True: in a framed box, as a written answer."""
        from gi.repository import Gtk
        label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=selectable)
        if card:
            label.add_css_class("card")
            label.add_css_class("am-summary")
        return label

    def button(self, label: str, on_click, tooltip: str = ""):
        from gi.repository import Gtk
        button = Gtk.Button(label=label, halign=Gtk.Align.START)
        if tooltip:
            button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda *_a: call(self.addon_id, on_click))
        return button

    def confirm(self, heading: str, body: str, preview: str, confirm_label: str, on_confirm):
        """A dialog that shows `preview` (exactly what will happen or be sent); on_confirm only on OK."""
        from gi.repository import Adw, Gtk
        dialog = Adw.AlertDialog(heading=heading, body=body)
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(preview)
        scroller = Gtk.ScrolledWindow(min_content_height=240, max_content_height=420, propagate_natural_height=True)
        scroller.set_child(view)
        dialog.set_extra_child(scroller)
        import i18n
        dialog.add_response("cancel", i18n._("Cancel"))
        dialog.add_response("ok", confirm_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.connect("response", lambda _d, response: response == "ok" and call(self.addon_id, on_confirm))
        dialog.present(self._app.window)

    def run_in_background(self, work, done=None):
        """work() on a thread; done(result) back on the UI thread (both guarded)."""
        from gi.repository import GLib

        def thread():
            result = call(self.addon_id, work)
            if done is not None:
                GLib.idle_add(lambda: (call(self.addon_id, done, result), False)[1])
        threading.Thread(target=thread, daemon=True).start()

    def toast(self, text: str, seconds: int = 4):
        self._app.toast(text, seconds)

    def duration_text(self, seconds: float) -> str:
        return self._app.duration_text(seconds)

    @staticmethod
    def short_number(n: int) -> str:
        for size, unit in ((10 ** 9, "B"), (10 ** 6, "M"), (10 ** 3, "k")):
            if n >= size:
                return f"{n / size:.1f}{unit}".replace(".0", "")
        return str(n)

    @staticmethod
    def tilde(path: str) -> str:
        import inventory
        return inventory.tilde(path)
