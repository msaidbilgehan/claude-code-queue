"""
Filesystem locations shared across the package.

Kept free of queue and subprocess imports so storage-only CLI commands can use it
without pulling in the Claude Code binary dependency (the E3 dispatch pattern).
"""

import os
from pathlib import Path


def claude_config_dir() -> Path:
    """Return Claude Code's config directory, honouring ``$CLAUDE_CONFIG_DIR``.

    Claude Code keeps its per-profile state — settings, installed skills, and the
    session artifacts the queue cleans up — under ``$CLAUDE_CONFIG_DIR`` when that
    variable is set, and under ``~/.claude`` otherwise. Anyone running more than
    one profile switches between them with that variable, so assuming
    ``~/.claude`` silently reads from, and writes to, the wrong profile.
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude"
