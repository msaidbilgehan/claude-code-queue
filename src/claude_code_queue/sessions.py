"""
Reading Claude Code's session logs.

Claude Code records every conversation as a JSONL file under
``<config dir>/projects/<encoded path>/<session-uuid>.jsonl``. This module reads
just enough of them to answer "which session was that?" — the id needed by
``claude-queue resume-session`` and a human-readable title to recognise it by.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from .paths import claude_config_dir

#: Stop scanning a log after this many lines. The title and working directory sit
#: in the opening records — a median of eleven lines in — so the cap only bites on
#: outliers, where the fallbacks still apply. Without it, listing sessions would
#: read hundreds of megabytes of transcript to print a few dozen titles.
MAX_SCAN_LINES = 400

#: Companion byte cap: single records can be megabytes when a tool returns a large
#: payload, so a line budget alone does not bound the read.
MAX_SCAN_BYTES = 1_048_576

_UNTITLED = "(untitled)"


@dataclass(frozen=True)
class SessionInfo:
    """One Claude Code conversation, as much as its log's opening records reveal."""

    session_id: str
    title: str
    project_dir: str
    git_branch: Optional[str]
    last_active: datetime
    path: Path

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "project_dir": self.project_dir,
            "git_branch": self.git_branch,
            "last_active": self.last_active.isoformat(),
            "path": str(self.path),
        }


def _clean_title(text: Optional[str]) -> Optional[str]:
    """Collapse a prompt into a single-line title, or None when there is nothing.

    Slash commands arrive wrapped in ``<command-name>`` markup, which makes a
    useless title; strip the tags and fall through when only markup remains.
    """
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.replace("<", " <").split())
    parts = [word for word in cleaned.split() if not word.startswith("<")]
    joined = " ".join(parts).strip()
    return joined or None


def _first_user_text(record: Dict[str, Any]) -> Optional[str]:
    """Pull the text of a user record, whether its content is a string or blocks."""
    if record.get("isMeta"):
        return None
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text")
    return None


def read_session(path: Path) -> Optional[SessionInfo]:
    """Summarise one session log, or return None when it yields nothing usable."""
    ai_title = last_prompt = first_user = None
    project_dir = git_branch = None
    scanned_bytes = 0

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                scanned_bytes += len(line)
                if index >= MAX_SCAN_LINES or scanned_bytes > MAX_SCAN_BYTES:
                    break

                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                kind = record.get("type")
                if kind == "ai-title":
                    ai_title = record.get("aiTitle")
                elif kind == "last-prompt":
                    last_prompt = record.get("lastPrompt")
                elif kind == "user":
                    project_dir = project_dir or record.get("cwd")
                    git_branch = git_branch or record.get("gitBranch")
                    first_user = first_user or _first_user_text(record)

                # Everything worth printing is known; the rest of the transcript
                # cannot change it.
                if ai_title and project_dir:
                    break
    except OSError:
        return None

    title = (
        _clean_title(ai_title)
        or _clean_title(last_prompt)
        or _clean_title(first_user)
        or _UNTITLED
    )

    try:
        last_active = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None

    return SessionInfo(
        session_id=path.stem,
        title=title,
        project_dir=project_dir or "",
        git_branch=git_branch,
        last_active=last_active,
        path=path,
    )


def find_session(
    session_id: str, claude_dir: Optional[Path] = None
) -> Optional[SessionInfo]:
    """Locate one session by id, or None when this profile has no such log.

    Logs are named after the session, so this is a direct glob rather than a scan.
    """
    root = (claude_dir or claude_config_dir()) / "projects"
    try:
        candidates = sorted(root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    for path in candidates:
        info = read_session(path)
        if info is not None:
            return info
    return None


def iter_session_files(claude_dir: Optional[Path] = None) -> Iterator[Path]:
    """Yield every session log under the active profile, newest first."""
    root = (claude_dir or claude_config_dir()) / "projects"
    try:
        files = list(root.glob("*/*.jsonl"))
    except OSError:
        return
    for path in sorted(files, key=_safe_mtime, reverse=True):
        yield path


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def list_sessions(
    project_dir: Union[str, Path, None] = None,
    search: Optional[str] = None,
    limit: Optional[int] = None,
    claude_dir: Optional[Path] = None,
) -> List[SessionInfo]:
    """Return sessions newest first.

    *project_dir* filters on the working directory recorded inside each log rather
    than on Claude Code's encoded directory name, which rewrites ``.`` and ``_``
    as well as ``/`` and would silently miss any path containing them.
    """
    wanted = str(Path(project_dir).expanduser().resolve()) if project_dir else None
    needle = search.lower() if search else None

    found: List[SessionInfo] = []
    for path in iter_session_files(claude_dir):
        if limit is not None and len(found) >= limit:
            break
        info = read_session(path)
        if info is None:
            continue
        if wanted is not None and info.project_dir != wanted:
            continue
        if needle is not None and needle not in info.title.lower():
            continue
        found.append(info)
    return found
