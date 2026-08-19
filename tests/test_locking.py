"""
Single-writer lock over a queue storage directory.

Two processors on one directory claim the same prompt and run the task twice, so
these guards are load-bearing rather than cosmetic.

Test IDs: LCK-001..LCK-014
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_queue
from claude_code_queue.claude_interface import ClaudeCodeInterface
from claude_code_queue.cli import main
from claude_code_queue.locking import LOCK_FILENAME, QueueLock, QueueLockError
from claude_code_queue.models import QueuedPrompt, QueueState
from claude_code_queue.storage import QueueStorage


@pytest.fixture
def lock(tmp_path):
    held = QueueLock(tmp_path)
    yield held
    held.release()


class TestAcquireRelease:
    def test_acquire_creates_lock_file_holding_the_pid(self, lock, tmp_path):  # LCK-001
        lock.acquire()
        assert (tmp_path / LOCK_FILENAME).read_text().strip() == str(os.getpid())

    def test_second_lock_on_same_directory_is_refused(self, lock, tmp_path):  # LCK-002
        lock.acquire()
        with pytest.raises(QueueLockError):
            QueueLock(tmp_path).acquire()

    def test_refusal_names_the_holder_and_the_way_out(self, lock, tmp_path):  # LCK-003
        lock.acquire()
        with pytest.raises(QueueLockError) as excinfo:
            QueueLock(tmp_path).acquire()
        message = str(excinfo.value)
        assert str(os.getpid()) in message
        assert "--storage-dir" in message

    def test_release_frees_the_directory(self, lock, tmp_path):  # LCK-004
        lock.acquire()
        lock.release()
        QueueLock(tmp_path).acquire()  # must not raise
        assert not lock.is_held

    def test_release_is_safe_when_never_held(self, tmp_path):  # LCK-005
        QueueLock(tmp_path).release()

    def test_release_is_idempotent(self, lock):  # LCK-006
        lock.acquire()
        lock.release()
        lock.release()

    def test_separate_directories_do_not_contend(self, tmp_path):  # LCK-007
        first, second = QueueLock(tmp_path / "a"), QueueLock(tmp_path / "b")
        first.acquire()
        second.acquire()  # must not raise
        first.release()
        second.release()

    def test_repeat_acquire_on_one_instance_is_a_noop(self, lock):  # LCK-008
        lock.acquire()
        lock.acquire()  # must not deadlock or raise
        assert lock.is_held

    def test_context_manager_releases_on_exit(self, tmp_path):  # LCK-009
        with QueueLock(tmp_path):
            with pytest.raises(QueueLockError):
                QueueLock(tmp_path).acquire()
        QueueLock(tmp_path).acquire()  # must not raise

    def test_lock_file_is_not_world_readable(self, lock, tmp_path):  # LCK-010
        lock.acquire()
        assert (tmp_path / LOCK_FILENAME).stat().st_mode & 0o077 == 0

    def test_lock_dies_with_the_holding_process(self, tmp_path):  # LCK-011
        """The kernel owns the lock, so a SIGKILLed processor leaves nothing to
        clean up by hand. A lock implemented with a plain marker file would strand
        the directory forever."""
        pkg_parent = str(Path(claude_code_queue.__file__).parent.parent)
        source = (
            "from claude_code_queue.locking import QueueLock;"
            f"QueueLock({str(tmp_path)!r}).acquire()"
        )
        result = subprocess.run(
            [sys.executable, "-c", source],
            env={**os.environ, "PYTHONPATH": pkg_parent},
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode()
        assert (tmp_path / LOCK_FILENAME).exists()

        QueueLock(tmp_path).acquire()  # holder is gone; must not raise


class TestQueueManagerIntegration:
    def test_start_refuses_a_locked_directory(self, manager, tmp_path, capsys):  # LCK-012
        other = QueueLock(tmp_path)
        other.acquire()
        try:
            assert manager.start() is False
        finally:
            other.release()
        assert "already being processed" in capsys.readouterr().err

    def test_start_releases_the_lock_on_shutdown(self, manager, tmp_path, mocker):  # LCK-013
        def stop_after_one_pass(callback=None):
            manager.stop()
            return False

        mocker.patch.object(
            manager, "_process_queue_iteration", side_effect=stop_after_one_pass
        )
        assert manager.start() is True
        QueueLock(tmp_path).acquire()  # must not raise

    def test_cli_start_exits_nonzero_when_locked(self, tmp_path, mocker):  # LCK-014
        """A supervisor has to see a refused start as a failure."""
        mocker.patch.object(ClaudeCodeInterface, "_verify_claude_available")
        other = QueueLock(tmp_path)
        other.acquire()
        try:
            argv = ["claude-queue", "--storage-dir", str(tmp_path), "start"]
            with patch("sys.argv", argv):
                assert main() == 1
        finally:
            other.release()

    def test_lock_file_is_not_mistaken_for_a_prompt(self, lock, tmp_path):  # LCK-015
        """The lock lives beside the queue/ directory, not inside it, so the
        queue loader never sees it."""
        lock.acquire()
        storage = QueueStorage(str(tmp_path))
        storage.save_queue_state(
            QueueState(prompts=[QueuedPrompt(id="abc12345", content="real work")])
        )
        reloaded = storage.load_queue_state()
        assert [p.id for p in reloaded.prompts] == ["abc12345"]
