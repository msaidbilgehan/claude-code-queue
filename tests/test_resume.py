"""
Continuing an interrupted session instead of starting over.

A queued job that hits the usage limit used to restart from scratch on retry,
repeating whatever the interrupted attempt had already finished. It now resumes
the same conversation. The same machinery backs `claude-queue resume-session`,
which queues a continuation of a session the user was working in.

Test IDs: RES-001..RES-030
"""

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_queue.cli import main
from claude_code_queue.claude_interface import ClaudeCodeInterface
from claude_code_queue.config import (
    CONFIG_FILENAME,
    DEFAULT_RESUME_MESSAGE,
    PROJECT_CONFIG_FILENAME,
    resolve_resume_message,
)
from claude_code_queue.models import ExecutionResult, PromptStatus, QueuedPrompt
from claude_code_queue.storage import QueueStorage

SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _mock_proc(stdout="done", stderr="", returncode=0):
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 4242
    proc.wait.return_value = returncode
    return proc


def _write_config(path, message):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"resume_message: {message}\n", encoding="utf-8")


class TestResumeMessageResolution:
    def test_default_when_nothing_is_configured(self):  # RES-001
        assert resolve_resume_message() == DEFAULT_RESUME_MESSAGE

    def test_prompt_field_wins(self, tmp_path):  # RES-002
        _write_config(tmp_path / PROJECT_CONFIG_FILENAME, "from project")
        _write_config(tmp_path / CONFIG_FILENAME, "from queue")
        assert resolve_resume_message("from prompt", tmp_path, tmp_path) == "from prompt"

    def test_project_beats_queue_wide(self, tmp_path):  # RES-003
        project, storage = tmp_path / "proj", tmp_path / "queue"
        _write_config(project / PROJECT_CONFIG_FILENAME, "from project")
        _write_config(storage / CONFIG_FILENAME, "from queue")
        assert resolve_resume_message(None, project, storage) == "from project"

    def test_queue_wide_used_when_project_is_silent(self, tmp_path):  # RES-004
        project, storage = tmp_path / "proj", tmp_path / "queue"
        project.mkdir()
        _write_config(storage / CONFIG_FILENAME, "from queue")
        assert resolve_resume_message(None, project, storage) == "from queue"

    def test_blank_value_falls_through(self, tmp_path):  # RES-005
        """Emptying a field should fall back, not resume with an empty prompt."""
        _write_config(tmp_path / CONFIG_FILENAME, '""')
        assert resolve_resume_message("   ", tmp_path, tmp_path) == DEFAULT_RESUME_MESSAGE

    def test_malformed_config_falls_back(self, tmp_path, capsys):  # RES-006
        """A stray tab in YAML must not stop the queue."""
        (tmp_path / CONFIG_FILENAME).write_text("resume_message: [unclosed\n")
        assert resolve_resume_message(None, None, tmp_path) == DEFAULT_RESUME_MESSAGE
        assert "malformed config" in capsys.readouterr().err

    def test_non_mapping_config_is_ignored(self, tmp_path):  # RES-007
        (tmp_path / CONFIG_FILENAME).write_text("just a string\n")
        assert resolve_resume_message(None, None, tmp_path) == DEFAULT_RESUME_MESSAGE

    def test_non_string_value_is_ignored(self, tmp_path):  # RES-008
        _write_config(tmp_path / CONFIG_FILENAME, "42")
        assert resolve_resume_message(None, None, tmp_path) == DEFAULT_RESUME_MESSAGE


class TestInterfaceResumes:
    def test_first_attempt_starts_a_new_session(self, interface, mocker, tmp_path):  # RES-010
        interface._supports_session_id = True
        interface._supports_resume = True
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        prompt = QueuedPrompt(id="abc12345", content="do the thing",
                              working_directory=str(tmp_path))
        result = interface.execute_prompt(prompt)
        cmd = popen.call_args[0][0]
        assert "--session-id" in cmd and "--resume" not in cmd
        assert cmd[-1] == "do the thing"
        assert result.session_id is not None

    def test_retry_resumes_the_recorded_session(self, interface, mocker, tmp_path):  # RES-011
        interface._supports_session_id = True
        interface._supports_resume = True
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        prompt = QueuedPrompt(id="abc12345", content="do the thing",
                              working_directory=str(tmp_path), session_id=SESSION_ID)
        result = interface.execute_prompt(prompt, "carry on")
        cmd = popen.call_args[0][0]
        assert cmd[cmd.index("--resume") + 1] == SESSION_ID
        assert "--session-id" not in cmd
        assert result.session_id == SESSION_ID

    def test_resume_sends_the_continuation_not_the_task(self, interface, mocker, tmp_path):  # RES-012
        """Re-sending the original instruction invites redoing finished work."""
        interface._supports_session_id = True
        interface._supports_resume = True
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        prompt = QueuedPrompt(id="abc12345", content="delete every temp file",
                              working_directory=str(tmp_path), session_id=SESSION_ID)
        interface.execute_prompt(prompt, "carry on")
        assert popen.call_args[0][0][-1] == "carry on"

    def test_resume_falls_back_to_the_default_message(self, interface, mocker, tmp_path):  # RES-013
        interface._supports_session_id = True
        interface._supports_resume = True
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        prompt = QueuedPrompt(id="abc12345", content="task",
                              working_directory=str(tmp_path), session_id=SESSION_ID)
        interface.execute_prompt(prompt)
        assert popen.call_args[0][0][-1] == DEFAULT_RESUME_MESSAGE

    def test_resume_does_not_re_attach_context_files(self, interface, mocker, tmp_path):  # RES-014
        """The files are already in the conversation."""
        interface._supports_session_id = True
        interface._supports_resume = True
        (tmp_path / "notes.md").write_text("x")
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        prompt = QueuedPrompt(id="abc12345", content="task", context_files=["notes.md"],
                              working_directory=str(tmp_path), session_id=SESSION_ID)
        interface.execute_prompt(prompt, "carry on")
        assert "@notes.md" not in popen.call_args[0][0][-1]

    def test_old_cli_without_resume_starts_fresh(self, interface, mocker, tmp_path):  # RES-015
        """A CLI that never advertised --resume must not be handed it."""
        interface._supports_session_id = True
        interface._supports_resume = False
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        prompt = QueuedPrompt(id="abc12345", content="task",
                              working_directory=str(tmp_path), session_id=SESSION_ID)
        interface.execute_prompt(prompt, "carry on")
        cmd = popen.call_args[0][0]
        assert "--resume" not in cmd
        assert cmd[-1] == "task"


class TestManagerRecordsSession:
    def test_session_is_recorded_for_the_next_attempt(self, manager):  # RES-020
        manager.state = manager.storage.load_queue_state()
        prompt = QueuedPrompt(id="abc12345", content="task")
        manager._process_execution_result(
            prompt,
            ExecutionResult(success=True, output="ok", session_id=SESSION_ID),
        )
        assert prompt.session_id == SESSION_ID

    def test_recorded_session_survives_a_reload(self, storage):  # RES-021
        prompt = QueuedPrompt(id="abc12345", content="task", session_id=SESSION_ID,
                              resume_message="carry on")
        storage._save_single_prompt(prompt)
        reloaded = storage.load_queue_state().prompts[0]
        assert reloaded.session_id == SESSION_ID
        assert reloaded.resume_message == "carry on"


class TestResumeSessionCommand:
    @staticmethod
    def _run(tmp_path, *extra):
        argv = ["claude-queue", "--storage-dir", str(tmp_path), "resume-session", *extra]
        with patch("sys.argv", argv):
            return main()

    def test_queues_a_continuation(self, tmp_path):  # RES-030
        assert self._run(tmp_path, SESSION_ID) == 0
        prompt = QueueStorage(str(tmp_path)).load_queue_state().prompts[0]
        assert prompt.session_id == SESSION_ID
        assert prompt.status == PromptStatus.QUEUED

    def test_uses_the_running_session_by_default(self, tmp_path, monkeypatch):  # RES-031
        """Run from inside the session that hit the limit, with no arguments."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_ID)
        assert self._run(tmp_path) == 0
        assert QueueStorage(str(tmp_path)).load_queue_state().prompts[0].session_id == SESSION_ID

    def test_refuses_without_a_session(self, tmp_path, monkeypatch, capsys):  # RES-032
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        assert self._run(tmp_path) == 1
        assert "CLAUDE_CODE_SESSION_ID" in capsys.readouterr().err

    def test_rejects_a_malformed_session_id(self, tmp_path, capsys):  # RES-033
        """Catch the typo now, not hours later after the reset."""
        assert self._run(tmp_path, "not-a-uuid") == 1
        assert "not a valid session id" in capsys.readouterr().err

    def test_message_flag_is_stored_on_the_prompt(self, tmp_path):  # RES-034
        assert self._run(tmp_path, SESSION_ID, "-m", "finish the migration") == 0
        prompt = QueueStorage(str(tmp_path)).load_queue_state().prompts[0]
        assert prompt.resume_message == "finish the migration"

    def test_working_directory_is_resolved(self, tmp_path):  # RES-035
        project = tmp_path / "proj"
        project.mkdir()
        assert self._run(tmp_path, SESSION_ID, "-d", str(project)) == 0
        prompt = QueueStorage(str(tmp_path)).load_queue_state().prompts[0]
        assert Path(prompt.working_directory).is_absolute()
        assert Path(prompt.working_directory).resolve() == project.resolve()
