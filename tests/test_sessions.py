"""
Listing Claude Code sessions.

`resume-session` needs a session id, and the only place those exist is the
filenames of Claude Code's transcripts. These tests cover pulling a usable title
out of a transcript without reading all of it.

Test IDs: SES-001..SES-030
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_code_queue.cli import _format_age, main
from claude_code_queue.sessions import (
    MAX_SCAN_LINES,
    list_sessions,
    read_session,
)

SESSION_ID = "11111111-2222-3333-4444-555555555555"
PROJECT = "/Users/x/work/my.proj_v2"


def _write_log(claude_dir, session_id=SESSION_ID, project=PROJECT, ai_title="Refactor the parser",
               last_prompt=None, first_user=None, encoded="-any-name-at-all", extra_lines=0):
    """Write a transcript shaped like Claude Code's, newest fields first."""
    path = claude_dir / "projects" / encoded / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for _ in range(extra_lines):
        records.append({"type": "attachment", "sessionId": session_id})
    if first_user is not None:
        records.append({
            "type": "user", "cwd": project, "gitBranch": "main",
            "message": {"content": first_user},
        })
    else:
        records.append({"type": "user", "cwd": project, "gitBranch": "main",
                        "message": {"content": "some prompt"}})
    if last_prompt is not None:
        records.append({"type": "last-prompt", "lastPrompt": last_prompt})
    if ai_title is not None:
        records.append({"type": "ai-title", "aiTitle": ai_title})
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


class TestReadSession:
    def test_prefers_the_generated_title(self, tmp_path):  # SES-001
        path = _write_log(tmp_path, ai_title="Refactor the parser")
        assert read_session(path).title == "Refactor the parser"

    def test_falls_back_to_the_last_prompt(self, tmp_path):  # SES-002
        path = _write_log(tmp_path, ai_title=None, last_prompt="Fix the flaky test")
        assert read_session(path).title == "Fix the flaky test"

    def test_falls_back_to_the_first_message(self, tmp_path):  # SES-003
        path = _write_log(tmp_path, ai_title=None, first_user="Add a health endpoint")
        assert read_session(path).title == "Add a health endpoint"

    def test_strips_slash_command_markup(self, tmp_path):  # SES-004
        """A session opened with /model would otherwise be titled with raw markup."""
        path = _write_log(
            tmp_path, ai_title=None,
            first_user="<command-name>/model</command-name> opus",
        )
        assert read_session(path).title == "opus"

    def test_markup_only_message_yields_untitled(self, tmp_path):  # SES-005
        path = _write_log(tmp_path, ai_title=None, first_user="<command-name></command-name>")
        assert read_session(path).title == "(untitled)"

    def test_records_project_and_branch(self, tmp_path):  # SES-006
        info = read_session(_write_log(tmp_path))
        assert info.project_dir == PROJECT
        assert info.git_branch == "main"

    def test_session_id_comes_from_the_filename(self, tmp_path):  # SES-007
        assert read_session(_write_log(tmp_path)).session_id == SESSION_ID

    def test_content_blocks_are_understood(self, tmp_path):  # SES-008
        path = _write_log(
            tmp_path, ai_title=None,
            first_user=[{"type": "text", "text": "Ship the release"}],
        )
        assert read_session(path).title == "Ship the release"

    def test_malformed_lines_are_skipped(self, tmp_path):  # SES-009
        path = _write_log(tmp_path)
        path.write_text("not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        assert read_session(path).title == "Refactor the parser"

    def test_missing_file_yields_nothing(self, tmp_path):  # SES-010
        assert read_session(tmp_path / "gone.jsonl") is None

    def test_stops_scanning_long_transcripts(self, tmp_path):  # SES-011
        """Titles sit in the opening records. Reading whole transcripts would mean
        hundreds of megabytes to print a few dozen lines, so a title pushed past
        the cap falls back instead."""
        path = _write_log(tmp_path, ai_title="Buried title", extra_lines=MAX_SCAN_LINES + 10)
        assert read_session(path).title != "Buried title"


class TestListSessions:
    def test_newest_first(self, tmp_path, monkeypatch):  # SES-020
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        old = _write_log(tmp_path, session_id="a" * 8 + "-1111-2222-3333-444444444444",
                         ai_title="older")
        new = _write_log(tmp_path, session_id="b" * 8 + "-1111-2222-3333-444444444444",
                         ai_title="newer")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(new, (1_700_000_000, 1_700_000_000))
        assert [s.title for s in list_sessions()] == ["newer", "older"]

    def test_filters_by_recorded_working_directory(self, tmp_path, monkeypatch):  # SES-021
        """Filtering reads cwd from the transcript rather than decoding Claude
        Code's directory name, which rewrites '.' and '_' as well as '/'."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _write_log(tmp_path, session_id="a" * 8 + "-1111-2222-3333-444444444444",
                   project=str(tmp_path / "my.proj_v2"), ai_title="mine",
                   encoded="totally-unrelated-name")
        _write_log(tmp_path, session_id="b" * 8 + "-1111-2222-3333-444444444444",
                   project="/somewhere/else", ai_title="theirs")
        (tmp_path / "my.proj_v2").mkdir()
        found = list_sessions(project_dir=tmp_path / "my.proj_v2")
        assert [s.title for s in found] == ["mine"]

    def test_search_matches_titles_case_insensitively(self, tmp_path, monkeypatch):  # SES-022
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _write_log(tmp_path, session_id="a" * 8 + "-1111-2222-3333-444444444444",
                   ai_title="Refactor the PARSER")
        _write_log(tmp_path, session_id="b" * 8 + "-1111-2222-3333-444444444444",
                   ai_title="Write docs")
        assert [s.title for s in list_sessions(search="parser")] == ["Refactor the PARSER"]

    def test_limit_caps_the_result(self, tmp_path, monkeypatch):  # SES-023
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        for i in range(4):
            _write_log(tmp_path, session_id=f"{i}" * 8 + "-1111-2222-3333-444444444444")
        assert len(list_sessions(limit=2)) == 2

    def test_absent_projects_directory_is_not_an_error(self, tmp_path, monkeypatch):  # SES-024
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nothing-here"))
        assert list_sessions() == []

    def test_reads_the_active_profile(self, tmp_path, monkeypatch):  # SES-025
        """Each profile has its own transcripts; listing must follow the active one."""
        first, second = tmp_path / "p1", tmp_path / "p2"
        _write_log(first, ai_title="in profile one")
        _write_log(second, session_id="b" * 8 + "-1111-2222-3333-444444444444",
                   ai_title="in profile two")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(second))
        assert [s.title for s in list_sessions()] == ["in profile two"]


class TestAgeFormatting:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(seconds=0), "just now"),
            (timedelta(seconds=59), "just now"),
            (timedelta(seconds=60), "1m ago"),
            (timedelta(minutes=59), "59m ago"),
            (timedelta(hours=1), "1h ago"),
            (timedelta(hours=23, minutes=59), "23h ago"),
            (timedelta(hours=24), "1d ago"),
            (timedelta(hours=25), "1d ago"),
            (timedelta(days=2, hours=12), "2d ago"),
            (timedelta(days=364), "364d ago"),
        ],
    )
    def test_rolls_over_on_readable_units(self, delta, expected):  # SES-040
        """Hours must roll into days at a day, not linger to 60h."""
        assert _format_age(datetime.now() - delta) == expected

    def test_falls_back_to_a_date_after_a_year(self):  # SES-041
        moment = datetime.now() - timedelta(days=400)
        assert _format_age(moment) == moment.strftime("%Y-%m-%d")

    def test_future_timestamps_do_not_go_negative(self):  # SES-042
        """Clock skew or a copied file can date a log slightly ahead."""
        assert _format_age(datetime.now() + timedelta(hours=1)) == "just now"


class TestSessionsCommand:
    @staticmethod
    def _run(*extra):
        with patch("sys.argv", ["claude-queue", "sessions", *extra]):
            return main()

    def test_lists_id_and_title(self, tmp_path, monkeypatch, capsys):  # SES-030
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _write_log(tmp_path, ai_title="Refactor the parser")
        assert self._run("--all") == 0
        out = capsys.readouterr().out
        assert SESSION_ID in out
        assert "Refactor the parser" in out

    def test_suggests_the_resume_command(self, tmp_path, monkeypatch, capsys):  # SES-031
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _write_log(tmp_path)
        self._run("--all")
        assert f"resume-session {SESSION_ID}" in capsys.readouterr().out

    def test_json_output_is_machine_readable(self, tmp_path, monkeypatch, capsys):  # SES-032
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _write_log(tmp_path, ai_title="Refactor the parser")
        assert self._run("--all", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["session_id"] == SESSION_ID
        assert payload[0]["title"] == "Refactor the parser"

    def test_defaults_to_the_current_directory(self, tmp_path, monkeypatch, capsys):  # SES-033
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _write_log(tmp_path, project="/elsewhere", ai_title="not here")
        monkeypatch.chdir(tmp_path)
        assert self._run() == 0
        out = capsys.readouterr().out
        assert "not here" not in out
        assert "No Claude Code sessions found" in out

    def test_empty_result_points_at_all(self, tmp_path, monkeypatch, capsys):  # SES-034
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        self._run()
        assert "--all" in capsys.readouterr().out

    def test_a_directory_argument_narrows_the_list(self, tmp_path, monkeypatch, capsys):  # SES-035
        """`sessions DIR` is what people reach for, so it must be the plain form."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        project = tmp_path / "proj"
        project.mkdir()
        _write_log(tmp_path, project=str(project), ai_title="in project")
        _write_log(tmp_path, session_id="b" * 8 + "-1111-2222-3333-444444444444",
                   project="/elsewhere", ai_title="somewhere else")
        assert self._run(str(project)) == 0
        out = capsys.readouterr().out
        assert "in project" in out
        assert "somewhere else" not in out

    def test_trailing_slash_is_accepted(self, tmp_path, monkeypatch, capsys):  # SES-036
        """Shell tab-completion appends one."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        project = tmp_path / "proj"
        project.mkdir()
        _write_log(tmp_path, project=str(project), ai_title="in project")
        assert self._run(str(project) + "/") == 0
        assert "in project" in capsys.readouterr().out

    def test_all_together_with_a_directory_is_refused(self, tmp_path, monkeypatch, capsys):  # SES-037
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert self._run("--all", str(tmp_path)) == 1
        assert "--all lists every project" in capsys.readouterr().err

    def test_unknown_directory_warns_but_still_looks(self, tmp_path, monkeypatch, capsys):  # SES-038
        """A project can be deleted while its transcripts remain."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        gone = tmp_path / "deleted-project"
        _write_log(tmp_path, project=str(gone), ai_title="from a deleted project")
        assert self._run(str(gone)) == 0
        captured = capsys.readouterr()
        assert "is not a directory" in captured.err
        assert "from a deleted project" in captured.out
