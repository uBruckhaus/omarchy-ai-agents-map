"""The chess page: a game against an agent that is set up (the header's chess button, with it selected).
It opens in a window of its own (floating or placed like any other) or as a page of the main window,
as Settings › Chess opens in says; the game is the same in each.

The board in the middle; above it a card for each kind of Black piece, below it one for each kind
of White piece, with how many are on the board. A choice above starts a new game with White or
with Black, or sets up a position: then the cards are a palette (pick one and click squares, or drag
one onto the board; a right click empties a square). Pieces are moved by dragging them with the
mouse, or by clicking the piece and then the square. After each of your moves the agent is asked for its move in the
background (chess_agent), the move is played and what it says about it is shown.
"""
from __future__ import annotations

import math
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

import ai_core  # noqa: E402
import chess_agent  # noqa: E402
import chess_rules as rules  # noqa: E402
import desktop  # noqa: E402
import i18n  # noqa: E402
from i18n import _, N_  # noqa: E402

GLYPHS = {"K": "♚", "Q": "♛", "R": "♜", "B": "♝", "N": "♞", "P": "♟"}        # solid shapes
OUTLINES = {"K": "♔", "Q": "♕", "R": "♖", "B": "♗", "N": "♘", "P": "♙"}      # their contours and details
KINDS = ("K", "Q", "R", "B", "N", "P")
KIND_NAMES = {"K": N_("King"), "Q": N_("Queen"), "R": N_("Rook"), "B": N_("Bishop"), "N": N_("Knight"),
              "P": N_("Pawn")}
CHOICES = (("white", N_("New game with White")), ("black", N_("New game with Black")),
           ("define", N_("Define position")))
# Thinking time (chess_agent.THINKING): longer is stronger.
THINKING_LABELS = (("quick", N_("Quick (30 s)")), ("normal", N_("Normal (1½ min)")), ("deep", N_("Deep (3 min)")),
                   ("max", N_("Maximum (6 min)")))
BOARD_MIN, BOARD_MAX = 320, 560
CHESS_WINDOW_SIZE = (760, 960)    # the chess window's first size: the biggest board with its cards
CHESS_MODES = ("floating", "window", "page")   # Settings › Chess opens in (settings_schema "chess_window")
TAKEN_W = 40                     # the columns of captured pieces beside the board
TAKEN_ORDER = "QRBNP"            # how they are listed: the strongest first
# Fonts whose chess glyphs are solid shapes (others, Font Awesome for one, draw them differently).
PIECE_FONTS = "DejaVu Sans,Noto Sans Symbols 2"
# chess_rules.problems() answers in English; these make them translatable (_() at display).
PROBLEMS = (N_("White needs exactly one king"), N_("Black needs exactly one king"),
            N_("Pawns cannot stand on the first or last rank"), N_("A side cannot have more than eight pawns"),
            N_("The side not to move must not be in check"))


def _rgb(hex_color: str, fallback=(0.5, 0.5, 0.5)):
    try:
        h = hex_color.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return fallback


def _mix(a, b, t: float):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


class ChessPage:
    # --- building -------------------------------------------------------------------------------

    def build_chess_page(self) -> Gtk.Widget:
        """The main window's chess page: a holder for the game, which moves into the chess window
        when it opens there (chess_place)."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, vexpand=True)
        self.chess_widget = page
        self.chess_window = None
        self.chess_window_box = None
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        self.chess_back_icon = Gtk.Image.new_from_icon_name("go-previous-symbolic")
        back_box.append(self.chess_back_icon)
        self.chess_back_label = Gtk.Label(label=_("Back"))
        back_box.append(self.chess_back_label)
        self.chess_back_keys = Gtk.Label(label="· ⌫")
        back_box.append(self.chess_back_keys)
        back.set_child(back_box)
        back.set_tooltip_text(_("Back (Backspace, Esc)"))
        back.connect("clicked", lambda *_a: self.close_chess())
        header.append(back)
        self.chess_back = back
        self.chess_title = Gtk.Label(xalign=0, hexpand=True)
        self.chess_title.add_css_class("am-section-title")
        header.append(self.chess_title)
        # Back keeps the game going (the agent's card on the map leads back to it); × ends it.
        end = Gtk.Button.new_from_icon_name("window-close-symbolic")
        end.add_css_class("flat")
        end.add_css_class("am-header-btn")
        end.set_tooltip_text(_("End the game and close the page"))
        end.connect("clicked", lambda *_a: self.end_chess())
        header.append(end)
        self.chess_end = end
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        body.add_css_class("am-settings")
        controls = Gtk.Box(spacing=8)
        controls.add_css_class("am-chess-controls")
        self.chess_choice = Gtk.DropDown.new_from_strings([_(label) for _key, label in CHOICES])
        self.chess_choice.set_tooltip_text(_("A new game, or a position set up by hand"))
        # Picking an entry starts it (White or Black: a new game, the board turned to your side);
        # Start begins the chosen one again.
        self.chess_choice.connect("notify::selected", lambda *_a: self.chess_choice_ready and self.chess_start_choice())
        self.chess_choice_ready = False
        controls.append(self.chess_choice)
        self.chess_thinking_choice = Gtk.DropDown.new_from_strings([_(label) for _key, label in THINKING_LABELS])
        self.chess_thinking_choice.set_selected([k for k, _l in THINKING_LABELS].index(chess_agent.DEFAULT_THINKING))
        self.chess_thinking_choice.set_tooltip_text(
            _("Thinking time: how long the agent may take for a move. Longer also lets its model think harder "
              "and asks it to play more strongly."))
        controls.append(self.chess_thinking_choice)
        start = Gtk.Button(label=_("Start"))
        start.add_css_class("am-small-btn")
        start.connect("clicked", lambda *_a: self.chess_start_choice())
        controls.append(start)
        self.chess_spinner = Gtk.Spinner()
        controls.append(self.chess_spinner)
        body.append(controls)

        # Setting up a position: whose move, and the rest of the tools.
        self.chess_setup_row = Gtk.Box(spacing=8)
        self.chess_setup_row.add_css_class("am-chess-controls")
        self.chess_turn_choice = Gtk.DropDown.new_from_strings([_("White to move"), _("Black to move")])
        self.chess_setup_row.append(self.chess_turn_choice)
        empty = Gtk.Button(label=_("Empty board"))
        empty.add_css_class("am-small-btn")
        empty.connect("clicked", lambda *_a: self.chess_setup_fill(""))
        self.chess_setup_row.append(empty)
        initial = Gtk.Button(label=_("Start position"))
        initial.add_css_class("am-small-btn")
        initial.connect("clicked", lambda *_a: self.chess_setup_fill(rules.START_FEN))
        self.chess_setup_row.append(initial)
        for side, label in (("w", _("Play it with White")), ("b", _("Play it with Black"))):
            button = Gtk.Button(label=label)
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda _b, s=side: self.chess_play_setup(s))
            self.chess_setup_row.append(button)
        body.append(self.chess_setup_row)

        # The cards above and below are as wide as the board, all three centred.
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.set_halign(Gtk.Align.CENTER)
        column.set_size_request(BOARD_MAX, -1)
        self.chess_column = column
        self.chess_top_cards = Gtk.Box(spacing=6, homogeneous=True)
        column.append(self.chess_top_cards)
        board = Gtk.DrawingArea()
        board.set_content_width(BOARD_MAX)
        board.set_content_height(BOARD_MAX)
        board.set_size_request(BOARD_MIN, BOARD_MIN)
        board.set_draw_func(self.chess_draw)
        # Dragging a piece, and a press without moving (a click), with the left button; the right
        # button empties a square while a position is set up. Pieces dragged from the cards drop here.
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self.chess_drag_begin)
        drag.connect("drag-update", self.chess_drag_update)
        drag.connect("drag-end", self.chess_drag_end)
        board.add_controller(drag)
        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_click.connect("pressed", lambda _g, _n, x, y: self.chess_clicked(self.chess_square_at(x, y), True, x, y))
        board.add_controller(right_click)
        drop = Gtk.DropTarget.new(str, Gdk.DragAction.COPY)
        drop.connect("drop", self.chess_card_dropped)
        board.add_controller(drop)
        self.chess_board = board
        self.chess_drag = None
        column.append(board)
        # Captured pieces beside the board: Black's on the left, White's on the right.
        sides = Gtk.Box(spacing=8, vexpand=True)
        sides.set_halign(Gtk.Align.CENTER)        # the board always in the middle of the free room
        sides.set_valign(Gtk.Align.CENTER)
        self.chess_taken = {}
        for side in ("b", "w"):
            taken = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, valign=Gtk.Align.CENTER)
            taken.add_css_class("am-chess-taken")
            taken.set_size_request(TAKEN_W, -1)
            self.chess_taken[side] = taken
        sides.append(self.chess_taken["b"])
        sides.append(column)
        sides.append(self.chess_taken["w"])
        self.chess_bottom_cards = Gtk.Box(spacing=6, homogeneous=True)
        column.append(self.chess_bottom_cards)
        body.append(sides)

        # Below: a card that folds, with whose move it is (or the hint while setting up), what the
        # agent said about its move and the game so far; folded, its heading still shows the status.
        self.chess_info = self.group_card(body, "chess-game", _("Game"), default_open=True)
        self.chess_status = Gtk.Label(xalign=0, wrap=True)
        self.chess_status.add_css_class("am-chess-status")
        self.chess_info.append(self.chess_status)
        answer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        answer.add_css_class("am-chess-answer")
        self.chess_answer_title = Gtk.Label(xalign=0)
        self.chess_answer_title.add_css_class("am-chess-answer-title")
        answer.append(self.chess_answer_title)
        self.chess_answer = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.chess_answer.add_css_class("am-chess-said")
        self.chess_answer.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        answer.append(self.chess_answer)
        self.chess_retry = Gtk.Button(label=_("Ask again"))
        self.chess_retry.add_css_class("am-small-btn")
        self.chess_retry.set_halign(Gtk.Align.START)
        self.chess_retry.connect("clicked", lambda *_a: self.chess_ask_agent())
        answer.append(self.chess_retry)
        self.chess_info.append(answer)
        self.chess_answer_box = answer
        self.chess_moves = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.chess_moves.add_css_class("am-dim")
        self.chess_moves.add_css_class("am-chess-said")
        self.chess_info.append(self.chess_moves)

        self.chess_body = body
        self.chess_scroller = self.page_scroller(body)
        page.append(self.chess_scroller)
        # The board as big as the window allows (BOARD_MIN…BOARD_MAX), so the page fits without scrolling.
        self.chess_side = BOARD_MAX
        board.add_tick_callback(self.chess_fit)
        self.chess_agent = None
        self.chess_token = 0
        self.chess_reset(rules.start(), "w")
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        holder.append(page)
        self.chess_holder = holder
        return holder

    # --- where it shows: a page of the main window, or a window of its own ------------------------

    @staticmethod
    def chess_opens_in() -> str:
        mode = ai_core.setting("chess_window")
        return mode if mode in CHESS_MODES else CHESS_MODES[0]

    def chess_window_card(self) -> Gtk.Box:
        """The chess window, made the first time: themed as the main window (its name carries the
        CSS), closing it only hides it (the game goes on), Esc and Backspace close it."""
        if self.chess_window is None:
            window = Adw.ApplicationWindow(application=self)
            window.set_decorated(desktop.decorated())
            window.set_name("aiagentsmap-window")
            window.set_icon_name(desktop.app_id())
            window.set_title(_("Chess"))
            window.set_default_size(*CHESS_WINDOW_SIZE)
            window.connect("close-request", lambda *_a: (self.close_chess(), True)[1])
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self.chess_window_key)
            window.add_controller(keys)
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
            card.add_css_class("am-card")
            if desktop.decorated():
                toolbar = Adw.ToolbarView()
                toolbar.add_top_bar(Adw.HeaderBar())
                toolbar.set_content(card)
                window.set_content(toolbar)
            else:
                window.set_content(card)
            self.chess_window = window
            self.chess_window_box = card
        return self.chess_window_box

    def chess_window_key(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            self.close_chess()
            return True
        return False

    def chess_in_window(self) -> bool:
        return self.chess_window is not None and self.chess_widget.get_parent() is self.chess_window_box

    def chess_place(self):
        """The game into the chess window or the main window's chess page, as Settings say. In the
        window its Back is Close (the desktop's own close button where it draws a title bar)."""
        in_window = self.chess_opens_in() != "page"
        target = self.chess_window_card() if in_window else self.chess_holder
        parent = self.chess_widget.get_parent()
        if parent is not target:
            if parent is not None:
                parent.remove(self.chess_widget)
            target.append(self.chess_widget)
        self.chess_back_icon.set_visible(not in_window)
        self.chess_back_label.set_label(_("Close") if in_window else _("Back"))
        self.chess_back_keys.set_label("· Esc" if in_window else "· ⌫")
        self.chess_back.set_tooltip_text(_("Close (Esc); the game goes on") if in_window
                                         else _("Back (Backspace, Esc)"))
        self.chess_back.set_visible(not (in_window and desktop.decorated()))
        self.chess_end.set_tooltip_text(_("End the game and close the window") if in_window
                                        else _("End the game and close the page"))

    def set_chess_opens_in(self, mode: str):
        """Settings › Chess opens in: an open game goes on; the knight opens it in the new place."""
        self.save_setting(chess_window=mode)
        if self.chess_window is not None:
            self.chess_window.set_visible(False)
        self.chess_place()

    def chess_show_window(self, title: str, floating: bool):
        """To the front; under Hyprland floated at its size and centred (it would be tiled), once
        Hyprland lists it (a few tenths of a second after present)."""
        self.chess_window.present()
        tries = [0]

        def place():
            if self.chess_window is None or not self.chess_window.get_visible():
                return GLib.SOURCE_REMOVE
            if desktop.place_window(title, floating, CHESS_WINDOW_SIZE):
                return GLib.SOURCE_REMOVE
            tries[0] += 1
            return tries[0] < 15
        GLib.timeout_add(100, place)

    # --- open / close ------------------------------------------------------------------------------

    def chess_set_up(self, agent) -> bool:
        """Is the agent set up to play: signed in, or connected to a local LLM it may use."""
        import agent_details
        import agent_status
        access = agent_status.access_table().get(agent.command)
        if access is None:
            return False
        if agent_status.credentials_present(access):
            return True
        if access.local_ok:
            try:
                return any(p.local for p in agent_details.details_for(agent.command).providers)
            except Exception:
                return False
        return False

    def can_play_chess(self, agent) -> bool:
        return chess_agent.can_play(agent, agent is not None and agent.installed and self.chess_set_up(agent))

    def open_chess(self, agent):
        """A game against this agent; back to the page, the game goes on where it was."""
        same = self.chess_agent is not None and self.chess_agent.command == agent.command
        self.chess_agent = agent
        title = _("Chess") + " · " + agent.name
        self.chess_title.set_label(title.upper())
        if not same or (not self.chess_moves_played and self.chess_mode != "define"):
            self.chess_choice_ready = False
            self.chess_choice.set_selected(0)
            self.chess_choice_ready = True
            self.chess_new_game("w")
        else:
            self.chess_refresh()
        self.chess_place()
        mode = self.chess_opens_in()
        if mode == "page":
            if self.page_name() not in ("settings", "help", "chess"):
                self.remember_page()                 # back to a terminal or details page, as from Settings
            self.show_page("chess")
        else:
            if self.page_name() == "chess":
                self.back_to_last_page()
            self.chess_window.set_title(title)       # place_window finds it by this title
            self.chess_show_window(title, mode == "floating")
        self.chess_mark_map()

    def close_chess(self):
        """Back, or closing the chess window: the game goes on (an answer on its way is still
        played); the knights lead back."""
        if self.chess_in_window():
            self.chess_window.set_visible(False)
        elif self.page_name() == "chess":
            self.back_to_last_page()
        self.chess_mark_map()

    def end_chess(self):
        """× or the agent's stop button: the game ends (an answer on its way is dropped) and the
        page closes if it is open."""
        self.chess_reset(rules.start(), "w")
        self.chess_agent = None
        if self.chess_window is not None:
            self.chess_window.set_visible(False)
        if self.page_name() == "chess":
            self.back_to_last_page()
        self.chess_mark_map()

    def chess_game_with(self, agent) -> bool:
        """A game against this agent is open (the page may be closed)."""
        return agent is not None and self.chess_agent is not None and self.chess_agent.command == agent.command

    def chess_selected_agent(self):
        """The agent selected on the map, where it can play chess."""
        command = next((i.partition(":")[2] for i in getattr(self, "selection", []) if i.startswith("agent:")), None)
        agent = next((a for a in getattr(self, "agent_list", []) if a.command == command), None)
        return agent if agent is not None and self.can_play_chess(agent) else None

    def on_chess_button(self):
        agent = self.chess_selected_agent() or self.chess_agent
        if agent is not None:
            self.open_chess(agent)

    def chess_mark_map(self):
        """The header's chess button: a game against the selected agent where it plays one, back to
        the game while one is open, else greyed. While a game is open, on the agent's card a stop
        button (it ends the game) and a chess button, back to the game."""
        button = getattr(self, "chess_button", None)
        if button is not None:
            agent = self.chess_selected_agent()
            if agent is not None and not self.chess_game_with(agent):
                tip = _("Play chess") + "\n" + _("A game of chess against {0}; it is asked for each move").format(agent.name)
            elif agent is not None or self.chess_agent is not None:
                tip = _("Back to the chess game")
            else:
                tip = _("Play chess: select an agent on the map that plays it (Claude Code, Codex, Hermes or Pi, set up)")
            button.set_sensitive(agent is not None or self.chess_agent is not None)
            button.set_tooltip_text(tip)
        view = getattr(self, "side_map", None)
        if view is not None:
            view.chess_node = f"agent:{self.chess_agent.command}" if self.chess_agent is not None else None
            view.queue_draw()

    # --- game state --------------------------------------------------------------------------------

    def chess_reset(self, start_pos, player: str, mode: str = "game"):
        self.chess_token += 1                       # an answer still on its way is dropped
        self.chess_start_pos = start_pos
        self.chess_pos = start_pos
        self.chess_moves_played = []
        self.chess_player = player                  # "w" or "b": the side you play
        self.chess_mode = mode                      # "game" or "define"
        self.chess_selected = None
        self.chess_palette = None                   # the piece placed in define mode
        self.chess_last = None
        self.chess_thinking = False
        self.chess_said = ""
        self.chess_error = ""

    def chess_start_choice(self):
        key = CHOICES[self.chess_choice.get_selected()][0]
        if key == "define":
            self.chess_reset(self.chess_pos, self.chess_player, "define")
            self.chess_turn_choice.set_selected(0 if self.chess_pos.turn == "w" else 1)
            self.chess_refresh()
        else:
            self.chess_new_game("w" if key == "white" else "b")

    def chess_new_game(self, player: str, start_pos=None):
        self.chess_reset(start_pos or rules.start(), player)
        self.chess_refresh()
        if self.chess_pos.turn != player:
            self.chess_ask_agent()

    def chess_setup_fill(self, fen: str):
        self.chess_pos = rules.from_fen(fen) if fen else rules.Position(tuple([""] * 64), "w", "")
        self.chess_refresh()

    def chess_play_setup(self, player: str):
        turn = "w" if self.chess_turn_choice.get_selected() == 0 else "b"
        pos = rules.Position(self.chess_pos.board, turn, "KQkq", None, 0, 1)
        pos = rules.from_fen(rules.fen(pos))         # castling only where king and rook stand at home
        problems = rules.problems(pos)
        if problems:
            self.chess_set_status(_(problems[0]))
            return
        if rules.status(pos) != "ongoing":
            self.chess_set_status(_("In this position the game is already over."))
            return
        self.chess_new_game(player, pos)

    def chess_over_text(self, pos) -> str:
        state = rules.status(pos)
        if state == "checkmate":
            winner = "b" if pos.turn == "w" else "w"
            return _("Checkmate: you win.") if winner == self.chess_player else _("Checkmate: {0} wins.").format(
                self.chess_agent.name if self.chess_agent else "")
        return {"stalemate": _("Stalemate: a draw."), "fifty": _("Draw by the fifty-move rule."),
                "material": _("Draw: neither side can still mate.")}.get(state, "")

    def chess_play(self, move):
        self.chess_moves_played.append(move)
        self.chess_last = move
        self.chess_pos = rules.apply(self.chess_pos, move)
        self.chess_selected = None

    # --- the agent's move --------------------------------------------------------------------------

    def chess_ask_agent(self, retry_of: str = ""):
        agent = self.chess_agent
        if agent is None or self.chess_mode != "game" or self.chess_pos.turn == self.chess_player:
            return
        if rules.status(self.chess_pos) != "ongoing":
            return
        self.chess_token += 1
        token, pos = self.chess_token, self.chess_pos
        level = THINKING_LABELS[self.chess_thinking_choice.get_selected()][0]
        text = chess_agent.prompt(self.chess_start_pos, list(self.chess_moves_played),
                                  self.chess_pos.turn, i18n.current(), level)
        if retry_of:
            text = chess_agent.retry_prompt(text, retry_of)
        self.chess_thinking = True
        self.chess_error = ""
        self.chess_think_start = time.monotonic()
        self.chess_think_limit = chess_agent.seconds(level)
        GLib.timeout_add_seconds(1, self.chess_tick, token)
        self.chess_refresh()

        def work():
            reply = chess_agent.ask(agent.command, text, pos, level=level)
            GLib.idle_add(done, reply)

        def done(reply):
            if token != self.chess_token:
                return False                       # a new game or a move undone meanwhile
            self.chess_thinking = False
            self.chess_said = reply.comment
            if reply.error:
                self.chess_error = reply.error
            elif reply.move is None:
                if not retry_of:
                    self.chess_ask_agent(retry_of=reply.comment or " ")
                    return False
                self.chess_error = _("{0} did not name one of its legal moves.").format(agent.name)
            else:
                self.chess_play(reply.move)
            self.chess_refresh()
            return False

        threading.Thread(target=work, daemon=True).start()

    # --- showing -----------------------------------------------------------------------------------

    def chess_refresh(self):
        pos = self.chess_pos
        define = self.chess_mode == "define"
        self.chess_setup_row.set_visible(define)
        self.chess_fill_cards(self.chess_top_cards, "b")
        self.chess_fill_cards(self.chess_bottom_cards, "w")
        if self.chess_player == "b" and not define:   # your pieces at the bottom
            self.chess_fill_cards(self.chess_top_cards, "w")
            self.chess_fill_cards(self.chess_bottom_cards, "b")
        self.chess_fill_taken(define)
        name = self.chess_agent.name if self.chess_agent else ""
        if define:
            status = _("Pick a piece below or above, then click squares; a right click empties a square.")
        elif self.chess_thinking:
            spent = int(time.monotonic() - getattr(self, "chess_think_start", time.monotonic()))
            limit = getattr(self, "chess_think_limit", 0)
            status = _("{0} is thinking… {1} of {2}").format(name, f"{spent // 60}:{spent % 60:02d}",
                                                              f"{limit // 60}:{limit % 60:02d}")
        else:
            status = self.chess_over_text(pos) or (
                _("Your move.") if pos.turn == self.chess_player else _("{0} is to move.").format(name))
            if rules.in_check(pos, pos.turn) and rules.status(pos) == "ongoing":
                status += " " + _("Check!")
        self.chess_set_status(status)
        if self.chess_thinking:
            self.chess_spinner.start()
        else:
            self.chess_spinner.stop()
        said = self.chess_said or ""
        self.chess_answer_title.set_label(_("{0} says").format(name) if said or self.chess_error else "")
        self.chess_answer.set_label("\n".join(t for t in (said, self.chess_error) if t))
        self.chess_answer_box.set_visible(bool(said or self.chess_error) and not define)
        self.chess_retry.set_visible(bool(self.chess_error) and not self.chess_thinking)
        self.chess_moves.set_label(rules.game_san(self.chess_start_pos, self.chess_moves_played)
                                   if self.chess_moves_played and not define else "")
        self.chess_board.queue_draw()

    def chess_tick(self, token: int) -> bool:
        """Once a second while the agent thinks: the time spent, in the status line."""
        if token != self.chess_token or not self.chess_thinking:
            return False
        self.chess_refresh_status_only()
        return True

    def chess_refresh_status_only(self):
        name = self.chess_agent.name if self.chess_agent else ""
        spent = int(time.monotonic() - self.chess_think_start)
        limit = self.chess_think_limit
        self.chess_set_status(_("{0} is thinking… {1} of {2}").format(
            name, f"{spent // 60}:{spent % 60:02d}", f"{limit // 60}:{limit % 60:02d}"))

    def chess_set_status(self, text: str):
        """Whose move it is (or a hint), in the game card and in its heading while it is folded."""
        self.chess_status.set_label(text)
        self.chess_info.group_summary.set_label(text)
        self.chess_info.group_summary.set_tooltip_text(text or None)

    def chess_fill_cards(self, box: Gtk.Box, side: str):
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        define = self.chess_mode == "define"
        for kind in KINDS:
            piece = kind if side == "w" else kind.lower()
            count = self.chess_pos.board.count(piece)
            card = Gtk.Button() if define else Gtk.Box()
            card.add_css_class("am-chess-card")
            if define and self.chess_palette == piece:
                card.add_css_class("am-chess-picked")
            if not define and count == 0:
                card.add_css_class("am-chess-gone")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            glyph = Gtk.Label(label=GLYPHS[kind])
            glyph.add_css_class("am-chess-glyph")
            glyph.add_css_class("am-chess-white" if side == "w" else "am-chess-black")
            inner.append(glyph)
            label = Gtk.Label(label=f"{_(KIND_NAMES[kind])} ×{count}")
            label.add_css_class("am-chess-count")
            label.set_ellipsize(Pango.EllipsizeMode.END)
            inner.append(label)
            if define:
                card.set_child(inner)
                card.set_tooltip_text(_("Place: {0}").format(_(KIND_NAMES[kind])))
                card.connect("clicked", lambda _b, p=piece: self.chess_pick(p))
                source = Gtk.DragSource(actions=Gdk.DragAction.COPY)
                source.connect("prepare", lambda _s, _x, _y, p=piece: Gdk.ContentProvider.new_for_value(p))
                source.connect("drag-begin", lambda src, _d, p=piece: src.set_icon(self.chess_piece_icon(p), 20, 20))
                card.add_controller(source)
            else:
                card.append(inner)
                card.set_tooltip_text(_("{0} on the board").format(_(KIND_NAMES[kind])))
            box.append(card)

    def chess_fill_taken(self, define: bool):
        """The captured pieces in their columns, the strongest first (none while setting up)."""
        taken = [] if define else rules.captured(self.chess_start_pos, self.chess_moves_played)
        for side, column in self.chess_taken.items():
            while (child := column.get_first_child()) is not None:
                column.remove(child)
            mine = [p for p in taken if (p.isupper() if side == "w" else p.islower())]
            for piece in sorted(mine, key=lambda p: TAKEN_ORDER.index(p.upper())):
                glyph = Gtk.Label(label=GLYPHS[piece.upper()])
                glyph.add_css_class("am-chess-glyph")
                glyph.add_css_class("am-chess-taken-glyph")
                glyph.add_css_class("am-chess-white" if side == "w" else "am-chess-black")
                glyph.set_tooltip_text(_("{0} captured").format(_(KIND_NAMES[piece.upper()])))
                column.append(glyph)

    def chess_pick(self, piece: str):
        self.chess_palette = None if self.chess_palette == piece else piece
        self.chess_refresh()

    # --- the board ---------------------------------------------------------------------------------

    def chess_geometry(self, width: int, height: int):
        side = max(8, min(width, height, BOARD_MAX))
        cell = side // 8
        x0 = (width - cell * 8) // 2
        y0 = (height - cell * 8) // 2
        return x0, y0, cell

    def chess_fit(self, *_args) -> bool:
        """Each frame while the page shows: the board's size from the room the page has (the rest of
        the page stays as it is). Changes only when the window's size does."""
        view_w, view_h = self.chess_scroller.get_width(), self.chess_scroller.get_height()
        if view_w <= 0 or view_h <= 0:
            return True
        other = self.chess_body.get_height() - self.chess_board.get_height()
        side = min(view_h - other - 4, view_w - 2 * (TAKEN_W + 8) - 40)
        side = max(BOARD_MIN, min(BOARD_MAX, side // 8 * 8))
        if abs(side - self.chess_side) >= 8:
            self.chess_side = side
            self.chess_board.set_content_width(side)
            self.chess_board.set_content_height(side)
            self.chess_column.set_size_request(side, -1)
        return True

    def chess_square_at(self, x: float, y: float) -> int | None:
        x0, y0, cell = self.chess_geometry(self.chess_board.get_width(), self.chess_board.get_height())
        col, row = int((x - x0) // cell), int((y - y0) // cell)
        if not (0 <= col < 8 and 0 <= row < 8):
            return None
        flipped = self.chess_player == "b" and self.chess_mode == "game"
        file_, rank = (7 - col, row) if flipped else (col, 7 - row)
        return rank * 8 + file_

    def chess_draw(self, _area, cr, width: int, height: int):
        colors = self.theme.colors
        bg, fg = _rgb(colors.get("background", "#1e1e2e")), _rgb(colors.get("foreground", "#cdd6f4"))
        accent, red = _rgb(colors.get("accent", "#89b4fa")), _rgb(colors.get("red", "#f04452"))
        light, dark = _mix(bg, fg, 0.62), _mix(bg, accent, 0.45)
        x0, y0, cell = self.chess_geometry(width, height)
        flipped = self.chess_player == "b" and self.chess_mode == "game"
        pos = self.chess_pos
        targets = set()
        if self.chess_selected is not None:
            targets = {m.end for m in rules.legal_moves(pos) if m.start == self.chess_selected}
        check_sq = None
        if rules.in_check(pos, pos.turn) and ("K" if pos.turn == "w" else "k") in pos.board:
            check_sq = pos.board.index("K" if pos.turn == "w" else "k")
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(f"{PIECE_FONTS} {max(8, int(cell * 0.62))}px"))
        coords = PangoCairo.create_layout(cr)
        coords.set_font_description(Pango.FontDescription.from_string(f"Sans Bold {max(7, cell // 6)}px"))
        for row in range(8):
            for col in range(8):
                file_, rank = (7 - col, row) if flipped else (col, 7 - row)
                sq = rank * 8 + file_
                x, y = x0 + col * cell, y0 + row * cell
                base = light if (file_ + rank) % 2 else dark
                cr.set_source_rgb(*base)
                cr.rectangle(x, y, cell, cell)
                cr.fill()
                if self.chess_last is not None and sq in (self.chess_last.start, self.chess_last.end):
                    cr.set_source_rgba(*accent, 0.35)
                    cr.rectangle(x, y, cell, cell)
                    cr.fill()
                if sq == self.chess_selected:
                    cr.set_source_rgba(*accent, 0.6)
                    cr.rectangle(x, y, cell, cell)
                    cr.fill()
                if sq == check_sq:
                    cr.set_source_rgba(*red, 0.55)
                    cr.arc(x + cell / 2, y + cell / 2, cell * 0.48, 0, 2 * math.pi)
                    cr.fill()
                # File letters on the bottom row, rank numbers on the left column.
                ink = dark if base == light else light
                if row == 7:
                    coords.set_text(rules.FILES[file_], -1)
                    cr.set_source_rgb(*ink)
                    cr.move_to(x + cell - cell // 5, y + cell - cell // 4)
                    PangoCairo.show_layout(cr, coords)
                if col == 0:
                    coords.set_text(str(rank + 1), -1)
                    cr.set_source_rgb(*ink)
                    cr.move_to(x + 3, y + 2)
                    PangoCairo.show_layout(cr, coords)
                piece = pos.at(sq)
                dragged = self.chess_drag is not None and self.chess_drag.get("moving") and self.chess_drag["from"] == sq
                if piece and not dragged:
                    self.chess_paint_piece(cr, layout, piece, x + cell / 2, y + cell / 2, cell)
                if sq in targets:
                    cr.set_source_rgba(*accent, 0.85)
                    radius = cell * (0.42 if piece else 0.14)
                    cr.arc(x + cell / 2, y + cell / 2, radius, 0, 2 * math.pi)
                    if piece:
                        cr.set_line_width(max(2.0, cell / 18))
                        cr.stroke()
                    else:
                        cr.fill()
        cr.set_source_rgba(*fg, 0.35)
        cr.set_line_width(1)
        cr.rectangle(x0 + 0.5, y0 + 0.5, cell * 8 - 1, cell * 8 - 1)
        cr.stroke()
        if self.chess_drag is not None and self.chess_drag.get("moving"):
            self.chess_paint_piece(cr, layout, self.chess_drag["piece"], self.chess_drag["x"], self.chess_drag["y"],
                                   cell * 1.1)

    @staticmethod
    def chess_paint_piece(cr, layout, piece: str, cx: float, cy: float, cell: float):
        """A piece centred on (cx, cy). White: the solid shape filled light, its contours and details
        (the outline glyph) dark on top. Black: a light halo round the solid shape, the shape dark."""
        kind = piece.upper()
        cr.save()
        if piece.isupper():                         # the outline glyphs are drawn a little smaller
            cr.translate(cx, cy)
            cr.scale(1.12, 1.12)
            cr.translate(-cx, -cy)
        layout.set_text(GLYPHS[kind], -1)
        _ink, logical = layout.get_pixel_extents()
        x, y = cx - logical.width / 2, cy - logical.height / 2
        cr.new_path()
        cr.move_to(x, y)
        PangoCairo.layout_path(cr, layout)
        if piece.isupper():
            cr.set_source_rgb(0.97, 0.96, 0.93)
            cr.fill()
            layout.set_text(OUTLINES[kind], -1)
            cr.move_to(x, y)
            PangoCairo.layout_path(cr, layout)
            cr.set_source_rgb(0.10, 0.10, 0.12)
            cr.fill()
        else:
            cr.set_source_rgba(0.92, 0.91, 0.87, 0.85)
            cr.set_line_width(max(1.5, cell / 22))
            cr.set_line_join(1)                      # round
            cr.stroke_preserve()
            cr.set_source_rgb(0.10, 0.10, 0.12)
            cr.fill()
        cr.restore()

    def chess_piece_icon(self, piece: str):
        """The picture under the pointer while a piece is dragged from a card."""
        import cairo
        size = 44
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(f"{PIECE_FONTS} {int(size * 0.8)}px"))
        self.chess_paint_piece(cr, layout, piece, size / 2, size / 2, size)
        surface.flush()
        data = GLib.Bytes.new(bytes(surface.get_data()))
        return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data, surface.get_stride())

    def chess_set_square(self, sq: int, piece: str):
        board = list(self.chess_pos.board)
        board[sq] = piece
        self.chess_pos = rules.Position(tuple(board), self.chess_pos.turn, "")

    def chess_your_turn(self) -> bool:
        pos = self.chess_pos
        return (self.chess_mode == "game" and not self.chess_thinking and pos.turn == self.chess_player
                and rules.status(pos) == "ongoing")

    def chess_drag_begin(self, _gesture, x, y):
        sq = self.chess_square_at(x, y)
        piece = self.chess_pos.at(sq) if sq is not None else ""
        movable = piece and (self.chess_mode == "define"
                             or (self.chess_your_turn() and rules.color_of(piece) == self.chess_pos.turn))
        self.chess_drag = {"from": sq, "piece": piece, "x": x, "y": y, "x0": x, "y0": y, "moving": False,
                           "movable": bool(movable)}

    def chess_drag_update(self, _gesture, dx, dy):
        drag = self.chess_drag
        if drag is None:
            return
        drag["x"], drag["y"] = drag["x0"] + dx, drag["y0"] + dy
        if drag["movable"] and (drag["moving"] or abs(dx) + abs(dy) > 6):
            if not drag["moving"]:
                drag["moving"] = True
                if self.chess_mode == "game":
                    self.chess_selected = drag["from"]       # its legal squares show while it is dragged
            self.chess_board.queue_draw()

    def chess_drag_end(self, _gesture, dx, dy):
        drag, self.chess_drag = self.chess_drag, None
        if drag is None:
            return
        x, y = drag["x0"] + dx, drag["y0"] + dy
        if not drag["moving"]:                               # a click
            self.chess_clicked(drag["from"], False, x, y)
            return
        target = self.chess_square_at(x, y)
        if self.chess_mode == "define":
            self.chess_set_square(drag["from"], "")
            if target is not None:                           # off the board: the piece is taken away
                self.chess_set_square(target, drag["piece"])
            self.chess_refresh()
            return
        moves = [m for m in rules.legal_moves(self.chess_pos) if m.start == drag["from"] and m.end == target]
        if len(moves) > 1:
            self.chess_ask_promotion(moves, x, y)
        elif moves:
            self.chess_move_made(moves[0])
        else:
            self.chess_selected = None
            self.chess_board.queue_draw()

    def chess_card_dropped(self, _target, value, x, y) -> bool:
        sq = self.chess_square_at(x, y)
        if self.chess_mode != "define" or sq is None or str(value) not in GLYPHS and str(value).upper() not in GLYPHS:
            return False
        self.chess_set_square(sq, str(value))
        self.chess_refresh()
        return True

    def chess_clicked(self, sq, secondary: bool, x: float, y: float):
        if sq is None:
            return
        if self.chess_mode == "define":
            piece = self.chess_pos.at(sq)
            self.chess_set_square(sq, "" if secondary or self.chess_palette is None or piece == self.chess_palette
                                  else self.chess_palette)
            self.chess_refresh()
            return
        if secondary or not self.chess_your_turn():
            return
        pos = self.chess_pos
        piece = pos.at(sq)
        if self.chess_selected is not None:
            moves = [m for m in rules.legal_moves(pos) if m.start == self.chess_selected and m.end == sq]
            if moves:
                if len(moves) > 1:                  # a promotion: which piece
                    self.chess_ask_promotion(moves, x, y)
                    return
                self.chess_move_made(moves[0])
                return
        self.chess_selected = sq if piece and rules.color_of(piece) == pos.turn else None
        self.chess_board.queue_draw()

    def chess_ask_promotion(self, moves, x, y):
        popover = Gtk.Popover()
        popover.set_parent(self.chess_board)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        row = Gtk.Box(spacing=4)
        for move in sorted(moves, key=lambda m: "qrbn".index(m.promotion)):
            kind = move.promotion.upper()
            button = Gtk.Button(label=GLYPHS[kind])
            button.add_css_class("am-chess-promote")
            button.set_tooltip_text(_(KIND_NAMES[kind]))
            button.connect("clicked", lambda _b, m=move: (popover.popdown(), self.chess_move_made(m)))
            row.append(button)
        popover.set_child(row)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def chess_move_made(self, move):
        self.chess_said, self.chess_error = "", ""
        self.chess_play(move)
        self.chess_refresh()
        self.chess_ask_agent()
