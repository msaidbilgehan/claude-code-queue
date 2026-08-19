"""
Per-prompt Claude Code profiles, and usage limits scoped to the account.

Each config directory holds its own credentials, so the profile a prompt records
decides which account pays for it. Limits are per account, so one profile
exhausting its window must not stall work billing to another.

Test IDs: PRF-001..PRF-040
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_code_queue.cli import main
from claude_code_queue.models import PromptStatus, QueuedPrompt, QueueState
from claude_code_queue.queue_manager import QueueManager
from claude_code_queue.storage import QueueStorage

PROFILE_A = "/profiles/account-a"
PROFILE_B = "/profiles/account-b"
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _limited(profile, hours_out=3, **kw):
    """A prompt rate-limited on *profile*, whose window has not reopened."""
    return QueuedPrompt(
        status=PromptStatus.RATE_LIMITED,
        claude_config_dir=profile,
        reset_time=datetime.now() + timedelta(hours=hours_out),
        **kw,
    )


def _queued(profile, **kw):
    return QueuedPrompt(status=PromptStatus.QUEUED, claude_config_dir=profile, **kw)


def _mock_proc():
    proc = MagicMock()
    proc.communicate.return_value = ("done", "")
    proc.returncode = 0
    proc.pid = 4242
    proc.wait.return_value = 0
    return proc


class TestProfileKey:
    def test_uses_the_recorded_profile(self):  # PRF-001
        assert QueuedPrompt(claude_config_dir=PROFILE_A).profile_key() == PROFILE_A

    def test_unset_resolves_to_the_active_profile(self, tmp_path, monkeypatch):  # PRF-002
        """An unrecorded prompt bills to whatever the processor runs under, so it
        must group with that account rather than look like a separate one."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert QueuedPrompt().profile_key() == str(tmp_path)

    def test_explicit_and_unset_share_a_key_when_they_match(self, tmp_path, monkeypatch):  # PRF-003
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert QueuedPrompt().profile_key() == QueuedPrompt(
            claude_config_dir=str(tmp_path)
        ).profile_key()


class TestLimitsAreScopedToTheAccount:
    def test_a_limited_account_does_not_stall_another(self, tmp_path, monkeypatch):  # PRF-010
        """The reason for queueing across profiles in the first place."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        runnable = _queued(PROFILE_B, id="runnable")
        state = QueueState(prompts=[_limited(PROFILE_A, id="blocked"), runnable])
        assert state.get_next_prompt() is runnable

    def test_a_limited_account_blocks_its_own_work(self, tmp_path, monkeypatch):  # PRF-011
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        state = QueueState(prompts=[_limited(PROFILE_A), _queued(PROFILE_A)])
        assert state.get_next_prompt() is None

    def test_every_account_limited_means_no_work(self, tmp_path, monkeypatch):  # PRF-012
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        state = QueueState(
            prompts=[_limited(PROFILE_A), _queued(PROFILE_A),
                     _limited(PROFILE_B), _queued(PROFILE_B)]
        )
        assert state.get_next_prompt() is None

    def test_a_reopened_window_stops_blocking(self, tmp_path, monkeypatch):  # PRF-013
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        expired = _limited(PROFILE_A, hours_out=-1, id="expired")
        waiting = _queued(PROFILE_A, id="waiting", priority=5)
        state = QueueState(prompts=[expired, waiting])
        assert state.get_next_prompt() is not None

    def test_unrecorded_prompts_share_the_active_account(self, tmp_path, monkeypatch):  # PRF-014
        """A prompt queued before profiles existed still belongs to some account —
        treating it as its own would let it dodge a limit it actually shares."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        state = QueueState(
            prompts=[_limited(str(tmp_path)), QueuedPrompt(status=PromptStatus.QUEUED)]
        )
        assert state.get_next_prompt() is None

    def test_priority_still_decides_among_runnable_work(self, tmp_path, monkeypatch):  # PRF-015
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        urgent = _queued(PROFILE_B, id="urgent", priority=0)
        state = QueueState(
            prompts=[_limited(PROFILE_A), _queued(PROFILE_B, id="later", priority=9), urgent]
        )
        assert state.get_next_prompt() is urgent


class TestExecutionBillsTheRecordedAccount:
    def test_profile_reaches_the_subprocess(self, interface, mocker, tmp_path):  # PRF-020
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        interface.execute_prompt(
            QueuedPrompt(content="x", working_directory=str(tmp_path),
                         claude_config_dir=PROFILE_A)
        )
        assert popen.call_args[1]["env"]["CLAUDE_CONFIG_DIR"] == PROFILE_A

    def test_unrecorded_profile_inherits_the_processor(self, interface, mocker, tmp_path, monkeypatch):  # PRF-021
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/profiles/ambient")
        popen = mocker.patch("subprocess.Popen", return_value=_mock_proc())
        interface.execute_prompt(
            QueuedPrompt(content="x", working_directory=str(tmp_path))
        )
        assert popen.call_args[1]["env"]["CLAUDE_CONFIG_DIR"] == "/profiles/ambient"


class TestCleanupFollowsTheProfile:
    def test_scratch_files_are_removed_from_the_billed_profile(self, manager, tmp_path, monkeypatch):  # PRF-030
        """The processor's profile is not necessarily the prompt's."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "processor"))
        billed = tmp_path / "billed"
        todo = billed / "todos" / f"{SESSION_ID}-agent-{SESSION_ID}.json"
        todo.parent.mkdir(parents=True)
        todo.write_text("[]")

        manager._cleanup_session_artifacts(
            QueuedPrompt(session_id=SESSION_ID, claude_config_dir=str(billed))
        )
        assert not todo.exists()

    def test_other_profiles_are_left_alone(self, manager, tmp_path, monkeypatch):  # PRF-031
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "processor"))
        untouched = tmp_path / "processor" / "todos" / f"{SESSION_ID}-agent-{SESSION_ID}.json"
        untouched.parent.mkdir(parents=True)
        untouched.write_text("[]")

        manager._cleanup_session_artifacts(
            QueuedPrompt(session_id=SESSION_ID, claude_config_dir=str(tmp_path / "billed"))
        )
        assert untouched.exists()


class TestPersistenceAndCli:
    def test_profile_survives_a_reload(self, storage):  # PRF-035
        storage._save_single_prompt(
            QueuedPrompt(id="abc12345", content="x", claude_config_dir=PROFILE_A)
        )
        assert storage.load_queue_state().prompts[0].claude_config_dir == PROFILE_A

    def test_add_records_the_active_profile(self, tmp_path, monkeypatch):  # PRF-036
        profile = tmp_path / "profile"
        profile.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        argv = ["claude-queue", "--storage-dir", str(tmp_path), "add", "do a thing"]
        with patch("sys.argv", argv):
            assert main() == 0
        prompt = QueueStorage(str(tmp_path)).load_queue_state().prompts[0]
        assert prompt.claude_config_dir == str(profile)

    def test_add_accepts_an_explicit_profile(self, tmp_path, monkeypatch):  # PRF-037
        chosen = tmp_path / "chosen"
        chosen.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient"))
        argv = ["claude-queue", "--storage-dir", str(tmp_path), "add", "x",
                "--profile", str(chosen)]
        with patch("sys.argv", argv):
            assert main() == 0
        prompt = QueueStorage(str(tmp_path)).load_queue_state().prompts[0]
        assert prompt.claude_config_dir == str(chosen)

    def test_resume_session_records_the_profile(self, tmp_path, monkeypatch):  # PRF-038
        profile = tmp_path / "profile"
        profile.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        argv = ["claude-queue", "--storage-dir", str(tmp_path), "resume-session", SESSION_ID]
        with patch("sys.argv", argv):
            assert main() == 0
        prompt = QueueStorage(str(tmp_path)).load_queue_state().prompts[0]
        assert prompt.claude_config_dir == str(profile)

    def test_missing_profile_directory_warns(self, tmp_path, capsys):  # PRF-039
        """A typo would otherwise surface as an auth failure when the queue runs."""
        argv = ["claude-queue", "--storage-dir", str(tmp_path), "add", "x",
                "--profile", str(tmp_path / "typo")]
        with patch("sys.argv", argv):
            assert main() == 0
        assert "does not exist" in capsys.readouterr().err
