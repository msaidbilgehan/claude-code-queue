"""
Rate-limit artifact cleanup.

Covers config-directory resolution, session-UUID correlation, and the
``--session-id`` plumbing that makes the correlation exact. This path deletes
files outside the queue's own data directory, so every guard here is load-bearing.

Test IDs: ART-001..ART-035
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_code_queue.claude_interface import ClaudeCodeInterface
from claude_code_queue.models import QueuedPrompt
from claude_code_queue.paths import claude_config_dir
from claude_code_queue.queue_manager import QueueManager

SESSION_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"


def _make_artifacts(claude_dir, session_id, project_dir="-home-user-proj", jsonl_body="x"):
    """Create the four artifact files Claude Code leaves behind for one session."""
    paths = {
        "jsonl": claude_dir / "projects" / project_dir / f"{session_id}.jsonl",
        "todo": claude_dir / "todos" / f"{session_id}-agent-{session_id}.json",
        "debug": claude_dir / "debug" / f"{session_id}.txt",
        "telemetry": claude_dir / "telemetry" / f"1p_failed_events.{session_id}.abc123.json",
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(jsonl_body if key == "jsonl" else "x")
    return paths


def _mock_proc(stdout="done", stderr="", returncode=0):
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 4242
    proc.wait.return_value = returncode
    return proc


class TestClaudeConfigDir:
    def test_honours_claude_config_dir(self, tmp_path, monkeypatch):  # ART-001
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
        assert claude_config_dir() == tmp_path / "custom"

    def test_falls_back_to_home_claude(self, tmp_path, monkeypatch):  # ART-002
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert claude_config_dir() == tmp_path / ".claude"

    def test_expands_user_tilde(self, monkeypatch):  # ART-003
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/somewhere")
        assert claude_config_dir() == Path.home() / "somewhere"

    def test_empty_value_falls_back(self, tmp_path, monkeypatch):  # ART-004
        """An exported-but-empty variable must not resolve to the process CWD."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert claude_config_dir() == tmp_path / ".claude"


class TestArtifactRemoval:
    def test_removes_all_four_artifact_kinds(self, tmp_path, monkeypatch):  # ART-010
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        paths = _make_artifacts(tmp_path, SESSION_ID)
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 4
        for path in paths.values():
            assert not path.exists()

    def test_finds_jsonl_whatever_the_encoded_project_dir(self, tmp_path, monkeypatch):  # ART-011
        """Claude Code rewrites '.', '_' and '/' to '-' when encoding the project
        directory name. Cleanup locates the log by session UUID precisely so it
        never has to reproduce that encoding."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        paths = _make_artifacts(tmp_path, SESSION_ID, project_dir="-Users-x-my-proj-v2-test")
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 4
        assert not paths["jsonl"].exists()

    def test_leaves_other_sessions_untouched(self, tmp_path, monkeypatch):  # ART-012
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        mine = _make_artifacts(tmp_path, SESSION_ID)
        theirs = _make_artifacts(tmp_path, OTHER_ID)
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 4
        for path in mine.values():
            assert not path.exists()
        for path in theirs.values():
            assert path.exists()

    def test_large_conversation_log_still_removed(self, tmp_path, monkeypatch):  # ART-013
        """No size heuristic survives: a long rate-limited conversation is still
        this session's file, and the old <10 KB guard would have spared it."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        paths = _make_artifacts(tmp_path, SESSION_ID, jsonl_body="y" * 200_000)
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 4
        assert not paths["jsonl"].exists()

    def test_absent_artifacts_report_zero(self, tmp_path, monkeypatch):  # ART-014
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 0

    def test_missing_config_dir_is_not_an_error(self, tmp_path, monkeypatch):  # ART-015
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "does-not-exist"))
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 0

    def test_config_dir_wins_over_home_claude(self, tmp_path, monkeypatch):  # ART-016
        """Regression: cleanup hardcoded ~/.claude, so it silently deleted nothing
        for anyone running with CLAUDE_CONFIG_DIR set."""
        home, cfg = tmp_path / "home", tmp_path / "cfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        monkeypatch.setattr(Path, "home", lambda: home)
        stray = _make_artifacts(home / ".claude", SESSION_ID)
        mine = _make_artifacts(cfg, SESSION_ID)
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 4
        for path in mine.values():
            assert not path.exists()
        for path in stray.values():
            assert path.exists()

    def test_unreadable_artifact_does_not_abort_the_rest(self, tmp_path, monkeypatch, mocker):  # ART-017
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _make_artifacts(tmp_path, SESSION_ID)
        real_unlink = Path.unlink
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("locked")
            return real_unlink(self, *args, **kwargs)

        mocker.patch.object(Path, "unlink", flaky)
        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 3

    def test_symlinked_projects_directory_is_traversed(self, tmp_path, monkeypatch):  # ART-018
        """Profiles routinely symlink projects/ at a directory shared between them
        so history follows the user across profiles. The glob has to follow that
        link or cleanup silently finds nothing."""
        profile, shared = tmp_path / "profile", tmp_path / "shared"
        (shared / "-Users-x-proj").mkdir(parents=True)
        jsonl = shared / "-Users-x-proj" / f"{SESSION_ID}.jsonl"
        jsonl.write_text("x")
        profile.mkdir()
        (profile / "projects").symlink_to(shared, target_is_directory=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))

        assert QueueManager._do_cleanup_rate_limit_artifacts(SESSION_ID) == 1
        assert not jsonl.exists()


class TestCleanupWrapper:
    def test_none_session_id_deletes_nothing(self, manager, tmp_path, monkeypatch):  # ART-020
        """Without a known UUID the old code guessed by size and mtime. Guessing
        can destroy an unrelated session's history, so we decline instead."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        paths = _make_artifacts(tmp_path, SESSION_ID)
        prompt = QueuedPrompt(id="abc12345", content="x")
        manager._cleanup_rate_limit_artifacts(prompt, None)
        for path in paths.values():
            assert path.exists()

    def test_logs_removed_count(self, manager, tmp_path, monkeypatch):  # ART-021
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _make_artifacts(tmp_path, SESSION_ID)
        prompt = QueuedPrompt(id="abc12345", content="x")
        manager._cleanup_rate_limit_artifacts(prompt, SESSION_ID)
        assert "Cleaned up 4 rate-limit artifact(s)" in prompt.execution_log

    def test_failure_is_logged_not_raised(self, manager, mocker):  # ART-022
        """Cleanup must never propagate: the caller still has to persist the
        prompt's RATE_LIMITED status or it re-queues forever."""
        mocker.patch.object(
            QueueManager,
            "_do_cleanup_rate_limit_artifacts",
            side_effect=OSError("disk gone"),
        )
        prompt = QueuedPrompt(id="abc12345", content="x")
        manager._cleanup_rate_limit_artifacts(prompt, SESSION_ID)
        assert "artifact cleanup failed" in prompt.execution_log

    def test_nothing_removed_logs_nothing(self, manager, tmp_path, monkeypatch):  # ART-023
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        prompt = QueuedPrompt(id="abc12345", content="x")
        manager._cleanup_rate_limit_artifacts(prompt, SESSION_ID)
        assert "Cleaned up" not in prompt.execution_log


class TestSessionIdPlumbing:
    def test_flag_sent_when_cli_supports_it(self, interface, mocker, tmp_path):  # ART-030
        interface._supports_session_id = True
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        result = interface.execute_prompt(
            QueuedPrompt(id="abc12345", content="hi", working_directory=str(tmp_path))
        )
        cmd = popen.call_args[0][0]
        assert "--session-id" in cmd
        assert cmd[cmd.index("--session-id") + 1] == result.session_id

    def test_flag_absent_when_cli_lacks_it(self, interface, mocker, tmp_path):  # ART-031
        """An unknown flag would fail every queued prompt, so it is opt-in."""
        assert interface._supports_session_id is False
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        result = interface.execute_prompt(
            QueuedPrompt(id="abc12345", content="hi", working_directory=str(tmp_path))
        )
        assert "--session-id" not in popen.call_args[0][0]
        assert result.session_id is None

    def test_session_id_is_a_uuid(self, interface, mocker, tmp_path):  # ART-032
        import uuid

        interface._supports_session_id = True
        mocker.patch("subprocess.Popen", return_value=_mock_proc())
        result = interface.execute_prompt(
            QueuedPrompt(id="abc12345", content="hi", working_directory=str(tmp_path))
        )
        assert uuid.UUID(result.session_id).version == 4

    def test_each_execution_gets_a_fresh_id(self, interface, mocker, tmp_path):  # ART-033
        interface._supports_session_id = True
        mocker.patch("subprocess.Popen", return_value=_mock_proc())
        prompt = QueuedPrompt(id="abc12345", content="hi", working_directory=str(tmp_path))
        first = interface.execute_prompt(prompt).session_id
        second = interface.execute_prompt(prompt).session_id
        assert first != second

    def test_timeout_result_still_carries_session_id(self, interface, mocker, tmp_path):  # ART-034
        """A timed-out run leaves artifacts behind too; the caller needs the UUID."""
        import subprocess as sp

        interface._supports_session_id = True
        proc = _mock_proc()
        proc.communicate.side_effect = sp.TimeoutExpired(cmd="claude", timeout=1)
        mocker.patch("subprocess.Popen", return_value=proc)
        result = interface.execute_prompt(
            QueuedPrompt(id="abc12345", content="hi", working_directory=str(tmp_path))
        )
        assert result.success is False
        assert result.session_id is not None

    @pytest.mark.parametrize(
        "returncode,stdout,expected",
        [
            (0, "  --session-id <uuid>  Use a specific session ID", True),
            (0, "  --print  Print mode", False),
            (1, "  --session-id <uuid>", False),
        ],
    )
    def test_support_detection(self, interface, mocker, returncode, stdout, expected):  # ART-035
        mocker.patch(
            "subprocess.run",
            return_value=MagicMock(returncode=returncode, stdout=stdout),
        )
        assert interface._detect_session_id_support({}) is expected

    def test_support_detection_survives_a_broken_cli(self, interface, mocker):  # ART-036
        mocker.patch("subprocess.run", side_effect=OSError("no such binary"))
        assert interface._detect_session_id_support({}) is False
