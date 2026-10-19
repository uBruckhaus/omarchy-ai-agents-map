"""The chess page's opponent: an agent asked for one move at a time.

An agent can play when its adapter has an `ask` command (one prompt, one answer, without
tools: adapters/*.toml), it is installed and it is set up (signed in, or it uses a local LLM).
Each move is one run of that command in a new, empty, private folder, so the agent sees no
project and its answer is all that comes back. The prompt carries the position (FEN), the
game so far, the move just played and the legal answers; the move is taken from the answer
(chess_rules.find_move), and what the agent says about it is shown.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass

import chess_rules as rules
import harnesses
from i18n import _

# Thinking time: how long one answer may take, how hard the model thinks (the adapter's ask.effort
# options for that level) and how strongly the agent is asked to play.
THINKING = {"quick": (30, "at the level of a casual club player"),
            "normal": (90, "strongly, at the level of a good club player"),
            "deep": (180, "at master level"),
            "max": (360, "as strongly as you can")}
DEFAULT_THINKING = "normal"
MAX_ANSWER = 64 * 1024     # bytes read of it
MAX_COMMENT = 600          # characters of the agent's words shown
LANGUAGES = {"de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
             "nl": "Dutch", "pl": "Polish"}


def ask_argv(command: str) -> tuple:
    harness = harnesses.get(command)
    return harness.ask if harness is not None else ()


def plays(command: str, game: str = "chess") -> bool:
    """The agent's adapter says it can play the game (ask.games) and how to ask it once (ask.argv)."""
    harness = harnesses.get(command)
    return harness is not None and game in harness.games and bool(harness.ask)


def can_play(agent, set_up: bool, game: str = "chess") -> bool:
    """The agent is offered as an opponent: its adapter says it plays, it is installed and set up."""
    return bool(agent is not None and agent.installed and set_up and plays(agent.command, game)
                and shutil.which(ask_argv(agent.command)[0]))


def side_name(side: str) -> str:
    return "White" if side == "w" else "Black"


def seconds(level: str) -> int:
    return THINKING.get(level, THINKING[DEFAULT_THINKING])[0]


def prompt(start_pos: rules.Position, moves: list, agent_side: str, language: str = "en",
           level: str = DEFAULT_THINKING) -> str:
    """The text the agent gets for its next move."""
    pos = start_pos
    for move in moves:
        pos = rules.apply(pos, move)
    me = side_name(agent_side)
    you = side_name("b" if agent_side == "w" else "w")
    legal = ", ".join(sorted(rules.san(pos, m) for m in rules.legal_moves(pos)))
    lines = [
        f"We are playing a game of chess. You play {me}, I play {you}.",
        f"Current position (FEN): {rules.fen(pos)}",
    ]
    if moves:
        lines.append(f"Moves so far: {rules.game_san(start_pos, moves)}")
        last_pos = start_pos
        for move in moves[:-1]:
            last_pos = rules.apply(last_pos, move)
        lines.append(f"My last move: {rules.san(last_pos, moves[-1])}")
    else:
        lines.append("You make the first move.")
    lines += [
        f"Your legal moves: {legal}",
        f"Play {THINKING.get(level, THINKING[DEFAULT_THINKING])[1]}; you have about {seconds(level) * 2 // 3} "
        "seconds to think.",
        "Answer with your move in standard algebraic notation (SAN) alone on the first line.",
        "Then say in at most two short sentences what the idea of your move is"
        + (f", in {LANGUAGES[language]}." if language in LANGUAGES else "."),
        "Do not use any tools and do not ask questions.",
    ]
    return "\n".join(lines)


@dataclass
class Reply:
    move: rules.Move | None      # the move found in the answer (legal), or None
    written: str                 # how the agent wrote it
    comment: str                 # the agent's words, without the move line
    error: str = ""              # why there is no answer (timeout, no output, the program failed)


def comment_of(answer: str, written: str) -> str:
    """What the agent says besides its move: the answer without a first line that is only the move."""
    lines = [line.strip() for line in str(answer or "").splitlines() if line.strip()]
    if lines and written and lines[0].strip("*`. ").replace(" ", "").endswith(written.replace(" ", "")) \
            and len(lines[0]) <= len(written) + 12:
        lines = lines[1:]
    text = " ".join(lines)
    return text[:MAX_COMMENT] + ("…" if len(text) > MAX_COMMENT else "")


def run_argv(command: str, text: str, level: str = DEFAULT_THINKING) -> list:
    """The adapter's ask command with the prompt in place, and its options for the thinking level
    (ask.effort) where it says "{effort}", else just before the prompt."""
    harness = harnesses.get(command)
    if harness is None or not harness.ask:
        return []
    effort = list(dict(harness.effort).get(level, ()))
    placed = "{effort}" in harness.ask
    out = []
    for a in harness.ask:
        if a == "{effort}":
            out += effort
        elif a == "{prompt}":
            out += ([] if placed else effort) + [text]
        else:
            out.append(a)
    return out


def ask(command: str, text: str, pos: rules.Position, runner=None, level: str = DEFAULT_THINKING) -> Reply:
    """Ask the agent once and read its move (blocking: call it off the main thread). It may take
    the level's time (THINKING), and a little more for starting up."""
    import ai_core
    argv = run_argv(command, text, level)
    if not argv:
        return Reply(None, "", "", _("This agent cannot be asked for a move."))
    run = runner or ai_core.read_bounded_output
    folder = tempfile.mkdtemp(prefix="asterism-chess-")      # private (0700), empty
    try:
        out, ok = run(argv, max_bytes=MAX_ANSWER, timeout=seconds(level) + 15, cwd=folder,
                      env={**os.environ, "NO_COLOR": "1"})
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    answer = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
    if not answer.strip():
        return Reply(None, "", "", _("No answer: the agent took too long or stopped.") if not ok
                     else _("The agent gave an empty answer."))
    move, written = rules.find_move(pos, answer)
    return Reply(move, written, comment_of(answer, written))


def retry_prompt(first: str, written_answer: str) -> str:
    """A second try after an answer without a legal move."""
    said = " ".join(written_answer.split())[:200]
    return (first + "\n\nYour previous answer did not contain one of your legal moves"
            + (f" (it began: “{said}”)" if said else "")
            + ". Answer again: one legal move from the list, in SAN, alone on the first line.")
