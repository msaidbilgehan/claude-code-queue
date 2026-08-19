"""
Layered configuration for queue behaviour.

The queue ships without a config file. Every setting resolves through a chain
that ends in a built-in constant, so an absent — or malformed — file is a valid
state at every level rather than a startup failure.
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml  # type: ignore

#: Queue-wide settings, alongside the queue directories.
CONFIG_FILENAME = "config.yaml"

#: Per-project overrides, in the directory a prompt executes in.
PROJECT_CONFIG_FILENAME = ".claude-queue.yaml"

#: Sent when continuing a session that an earlier attempt left unfinished.
#: Phrased around inspecting the conversation rather than repeating the task,
#: because the queue re-sends this on every retry and the original instruction
#: is already in the history.
DEFAULT_RESUME_MESSAGE = (
    "The previous attempt was interrupted before it finished. Review what you "
    "already completed earlier in this conversation, then continue from that "
    "point. Do not redo work that is already done."
)

PathLike = Union[str, Path, None]


def load_config_file(path: Path) -> Dict[str, Any]:
    """Read one config file, returning ``{}`` when absent or unusable.

    A broken config must never stop the queue: a daemon that refuses to start
    because of a stray tab in YAML is worse than one running on defaults.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as e:
        print(f"Warning: ignoring malformed config {path}: {e}", file=sys.stderr)
        return {}

    return loaded if isinstance(loaded, dict) else {}


def resolve_resume_message(
    prompt_value: Optional[str] = None,
    working_directory: PathLike = None,
    storage_dir: PathLike = None,
) -> str:
    """Resolve the message sent when continuing an interrupted session.

    Most specific wins: the prompt's own ``resume_message`` frontmatter, then the
    project's ``.claude-queue.yaml``, then the queue's ``config.yaml``, then
    :data:`DEFAULT_RESUME_MESSAGE`.

    A blank or non-string value counts as unset at every level, so emptying a
    field falls through to the next one instead of resuming with an empty prompt.
    """
    candidates = [prompt_value]

    if working_directory:
        project = Path(working_directory).expanduser() / PROJECT_CONFIG_FILENAME
        candidates.append(load_config_file(project).get("resume_message"))

    if storage_dir:
        queue_wide = Path(storage_dir).expanduser() / CONFIG_FILENAME
        candidates.append(load_config_file(queue_wide).get("resume_message"))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate

    return DEFAULT_RESUME_MESSAGE
