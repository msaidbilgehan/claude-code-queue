"""
Single-writer lock over a queue storage directory.
"""

import os
import sys
from pathlib import Path
from typing import Optional, Union

LOCK_FILENAME = ".queue.lock"


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


class QueueLockError(RuntimeError):
    """Raised when another process already holds a queue storage directory."""


class QueueLock:
    """Advisory exclusive lock over one queue storage directory.

    ``QueueManager`` assumes it is the directory's only writer: every tick it
    reloads all prompt files, claims the highest-priority one, and rewrites
    queue-state.json wholesale. Two processors on one directory therefore claim
    the same prompt — running the task twice — and overwrite each other's
    counters. Queued tasks are only advised to be idempotent, never required to
    be, so this has to fail loudly at startup instead of corrupting quietly.

    The kernel owns the lock for the lifetime of the file descriptor, so it is
    released even when the holder is SIGKILLed or the machine loses power; there
    is no stale lock to clear by hand. The lock file itself is left on disk
    deliberately — unlinking it on release would let a second processor lock a
    now-orphaned inode and believe it had exclusive access.

    Commands that only read or write individual prompt files (``add``,
    ``status``, ``list``, ``bank``) never take the lock: they are safe to run
    alongside a live processor.
    """

    def __init__(self, storage_dir: Union[str, Path]) -> None:
        self.path = Path(storage_dir).expanduser() / LOCK_FILENAME
        self._fd: Optional[int] = None

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock, or raise ``QueueLockError`` naming the current holder."""
        if self._fd is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _try_lock(fd)
        except OSError as e:
            holder = self._read_holder()
            os.close(fd)
            raise QueueLockError(
                f"queue storage directory {self.path.parent} is already being "
                f"processed by {holder}. Running two processors against one "
                f"directory executes the same prompt twice. Stop the other "
                f"processor, or start this one with a different --storage-dir."
            ) from e

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        """Release the lock. Safe when not held, and safe to call twice."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    def _read_holder(self) -> str:
        """Describe the holder for the error message; best-effort only."""
        try:
            pid = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            pid = ""
        return f"process {pid}" if pid.isdigit() else "another process"

    def __enter__(self) -> "QueueLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
