"""Claude Code: its sessions from ~/.claude/projects (claude_sessions reads the numbers)."""

import claude_sessions
from harnesses import Harness, Sessions
from i18n import _


def _note() -> str:
    return _("Numbers from Claude Code's session files (no text is read). The API value is what the "
             "tokens would cost at API prices, not what a plan costs.")


HARNESS = Harness("claude", "Claude Code", sessions=Sessions(claude_sessions.collect_by_id, _note))
