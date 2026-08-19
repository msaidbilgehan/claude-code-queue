"""
Session artifact cleanup.

Covers config-directory resolution, session-UUID correlation, and the CLI feature
detection behind it. This path deletes files outside the queue's own data
directory, so every guard here is load-bearing.

The conversation log is deliberately *not* deleted — while a prompt is still
retryable it is the state ``--resume`` continues from, and afterwards it is the
record of the run. See tests/test_resume.py for the resume side.

Test IDs: ART-001..ART-036
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_code_queue.models import PromptStatus, QueuedPrompt
from claude_code_queue.paths import claude_config_dir
from claude_code_queue.queue_manager import QueueManager

SESSION_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"

#: todo stub, debug transcript, telemetry events — everything but the log.
SCRATCH_COUNT = 3


def _make_artifacts(claude_dir, session_id, project_dir="-home-user-proj", log_body="x"):
    """Create the four files Claude Code leaves behind for one session."""
    paths = {
        "jsonl": claude_dir / "projects" / project_dir / f"{session_id}.jsonl",
        "todo": claude_dir / "todos" / f"{session_id}-agent-{session_id}.json",
        "debug": claude_dir / "debug" / f"{session_id}.txt",
        "telemetry": claude_dir / "telemetry" / f"1p_failed_events.{session_id}.abc123.json",
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(log_body if key == "jsonl" else "x")
    return paths


def _prompt(session_id=SESSION_ID, status=PromptStatus.COMPLETED):
    return QueuedPrompt(id="abc12345", content="x", session_id=session_id, status=status)


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
    def test_removes_the_scratch_files(self, tmp_path, monkeypatch):  # ART-010
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        paths = _make_artifacts(tmp_path, SESSION_ID)
        assert QueueManager._do_cleanup_session_artifacts(SESSION_ID) == SCRATCH_COUNT
        assert not paths["todo"].exists()
        assert not paths["debug"].exists()
        assert not paths["telemetry"].exists()

    def test_keeps_the_conversation_log(self, tmp_path, monkeypatch):  # ART-011
        """The log is the state --resume continues from, and the record of the run
        afterwards. Retries reuse one session, so logs no longer pile up per
        attempt and there is nothing to reclaim by deleting it."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        paths = _make_artifacts(tmp_path, SESSION_ID)
        QueueManager._do_cleanup_session_artifacts(SESSION_ID)
        assert paths["jsonl"].exists()

    def test_leaves_other_sessions_untouched(self, tmp_path, monkeypatch):  # ART-012
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _make_artifacts(tmp_path, SESSION_ID)
        theirs = _make_artifacts(tmp_path, OTHER_ID)
        assert QueueManager._do_cleanup_session_artifacts(SESSION_ID) == SCRATCH_COUNT
        for path in theirs.values():
            assert path.exists()

    def test_absent_artifacts_report_zero(self, tmp_path, monkeypatch):  # ART-013
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert QueueManager._do_cleanup_session_artifacts(SESSION_ID) == 0

    def test_missing_config_dir_is_not_an_error(self, tmp_path, monkeypatch):  # ART-014
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "does-not-exist"))
        assert QueueManager._do_cleanup_session_artifacts(SESSION_ID) == 0

    def test_config_dir_wins_over_home_claude(self, tmp_path, monkeypatch):  # ART-015
        """Regression: cleanup hardcoded ~/.claude, so it silently deleted nothing
        for anyone running with CLAUDE_CONFIG_DIR set."""
        home, cfg = tmp_path / "home", tmp_path / "cfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        monkeypatch.setattr(Path, "home", lambda: home)
        stray = _make_artifacts(home / ".claude", SESSION_ID)
        mine = _make_artifacts(cfg, SESSION_ID)
        assert QueueManager._do_cleanup_session_artifacts(SESSION_ID) == SCRATCH_COUNT
        assert not mine["todo"].exists()
        for path in stray.values():
            assert path.exists()

    def test_unreadable_artifact_does_not_abort_the_rest(self, tmp_path, monkeypatch, mocker):  # ART-016
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
        assert QueueManager._do_cleanup_session_artifacts(SESSION_ID) == SCRATCH_COUNT - 1

    def test_telemetry_events_are_matched_by_glob(self, tmp_path, monkeypatch):  # ART-017
        """Telemetry files carry a second, unknown UUID and multiply per attempt."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _make_artifacts(tmp_path, SESSION_ID)
        extra = tmp_path / "telemetry" / f"1p_failed_events.{SESSION_ID}.zzz999.json"
        extra.write_text("x")
        assert QueueManager._do_cleanup_session_artifacts(SESSION_ID) == SCRATCH_COUNT + 1
        assert not extra.exists()


class TestCleanupWrapper:
    def test_prompt_without_a_session_deletes_nothing(self, manager, tmp_path, monkeypatch):  # ART-020
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        paths = _make_artifacts(tmp_path, SESSION_ID)
        manager._cleanup_session_artifacts(_prompt(session_id=None))
        for path in paths.values():
            assert path.exists()

    def test_logs_removed_count(self, manager, tmp_path, monkeypatch):  # ART-021
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        _make_artifacts(tmp_path, SESSION_ID)
        prompt = _prompt()
        manager._cleanup_session_artifacts(prompt)
        assert f"Cleaned up {SCRATCH_COUNT} session artifact(s)" in prompt.execution_log

    def test_failure_is_logged_not_raised(self, manager, mocker):  # ART-022
        """Cleanup must never propagate: the caller still has to persist the
        prompt's terminal status or it re-queues forever."""
        mocker.patch.object(
            QueueManager,
            "_do_cleanup_session_artifacts",
            side_effect=OSError("disk gone"),
        )
        prompt = _prompt()
        manager._cleanup_session_artifacts(prompt)
        assert "artifact cleanup failed" in prompt.execution_log

    def test_nothing_removed_logs_nothing(self, manager, tmp_path, monkeypatch):  # ART-023
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        prompt = _prompt()
        manager._cleanup_session_artifacts(prompt)
        assert "Cleaned up" not in prompt.execution_log


class TestFlagDetection:
    def test_reports_advertised_flags(self, interface, mocker):  # ART-030
        mocker.patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=0, stdout="  --session-id <uuid>\n  --resume [value]\n"
            ),
        )
        flags = interface._detect_supported_flags({})
        assert {"--session-id", "--resume"} <= flags

    def test_omits_flags_the_cli_does_not_list(self, interface, mocker):  # ART-031
        mocker.patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stdout="  --print\n")
        )
        assert "--session-id" not in interface._detect_supported_flags({})

    def test_failed_help_reports_nothing(self, interface, mocker):  # ART-032
        mocker.patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout="  --session-id <uuid>\n"),
        )
        assert interface._detect_supported_flags({}) == frozenset()

    def test_broken_cli_reports_nothing(self, interface, mocker):  # ART-033
        mocker.patch("subprocess.run", side_effect=OSError("no such binary"))
        assert interface._detect_supported_flags({}) == frozenset()
