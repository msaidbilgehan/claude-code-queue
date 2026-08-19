"""
Queue manager with execution loop.
"""

import os
import sys
import time
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable, Dict, Any

from .models import QueuedPrompt, QueueState, PromptStatus, ExecutionResult
from .storage import QueueStorage
from .claude_interface import ClaudeCodeInterface
from .paths import claude_config_dir


class QueueManager:
    """Manages the queue execution lifecycle."""

    def __init__(
        self,
        storage_dir: str = "~/.claude-queue",
        claude_command: str = "claude",
        check_interval: int = 30,
        timeout: int = 3600,
        skip_permissions: bool = True,
        generic_failure_retry_delay: int = 60,
    ):
        self.storage = QueueStorage(storage_dir)
        self.claude_interface = ClaudeCodeInterface(claude_command, timeout,
                                                    skip_permissions=skip_permissions)
        self.check_interval = check_interval
        self.running = False
        self.state: Optional[QueueState] = None
        if generic_failure_retry_delay < 1:
            print(
                f"Warning: generic_failure_retry_delay={generic_failure_retry_delay} "
                f"clamped to 1 to prevent retry spin loops",
                file=sys.stderr,
            )
        self._generic_failure_retry_delay = max(1, generic_failure_retry_delay)
        # Idle-wait coordination between _process_queue_iteration() and start():
        # seconds until the next known actionable moment (rate-limit reset or
        # retry cooldown expiry), or None when there is nothing to wait for.
        self._idle_wait_seconds: Optional[float] = None
        # Dedup key for the waiting message so it prints once per wait state
        # instead of on every poll iteration.
        self._last_wait_key: Optional[str] = None

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        # Signal-safe stderr write — avoids re-entering print()'s stream lock
        # if the main thread is mid-print when the signal fires.
        try:
            os.write(2, f"\nReceived signal {signum}, shutting down gracefully...\n".encode())
        except OSError:
            pass
        self.claude_interface.kill_current()  # unblocks communicate() if executing
        self.stop()

    def start(self, callback: Optional[Callable[[QueueState], None]] = None) -> None:
        """Start the queue processing loop."""
        print("Starting Claude Code Queue Manager...")

        is_working, message = self.claude_interface.test_connection()
        if not is_working:
            print(f"Error: {message}")
            return

        print(f"✓ {message}")

        self.state = self.storage.load_queue_state()
        print(f"✓ Loaded queue with {len(self.state.prompts)} prompts")

        # Fix 3: warn about cooldown-blocked prompts so operators aren't confused
        # when the queue appears stuck for up to 60 seconds after restart.
        now = datetime.now()
        cooldown_count = sum(
            1 for p in self.state.prompts
            if p.status == PromptStatus.QUEUED
            and p.retry_not_before is not None
            and now < p.retry_not_before
        )
        if cooldown_count:
            soonest = min(
                p.retry_not_before for p in self.state.prompts
                if p.retry_not_before is not None and now < p.retry_not_before
            )
            wait = max(1, int((soonest - now).total_seconds()))
            print(f"⏳ {cooldown_count} prompt(s) in retry cooldown (next eligible in {wait}s)")

        self.running = True

        try:
            while self.running:
                did_work = self._process_queue_iteration(callback)

                if self.running and not did_work:
                    # Sleep until the next known actionable moment (rate-limit
                    # reset / cooldown expiry) so the retry lands exactly on it,
                    # but never longer than check_interval — prompts added to the
                    # queue directory mid-wait still get picked up promptly.
                    sleep_seconds = self.check_interval
                    if self._idle_wait_seconds is not None:
                        sleep_seconds = min(
                            self.check_interval, max(1.0, self._idle_wait_seconds)
                        )
                    time.sleep(sleep_seconds)

        except KeyboardInterrupt:
            # print() is safe: we are in normal execution context, not signal-handler
            # context.  The signal handler used os.write(2, ...) and has already returned.
            print("\nShutdown requested by user")
        except Exception as e:
            print(f"Error in queue processing: {e}")
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Stop the queue processing loop."""
        self.running = False

    def _shutdown(self) -> None:
        """Clean shutdown procedure."""
        # print() is safe here: _shutdown() runs from the finally block in start(),
        # which is normal execution context (not signal-handler context).  The signal
        # handler itself uses os.write(2, ...) to avoid stream-lock re-entrance.
        print("Shutting down...")

        if self.state:
            for prompt in self.state.prompts:
                if prompt.status == PromptStatus.EXECUTING:
                    # Invariant: _execute_prompt() must clear retry_not_before
                    # in-memory before save_queue_state() writes .executing.md, so by the
                    # time _shutdown() runs, retry_not_before is already None.
                    if prompt.retry_not_before is not None:
                        print(
                            f"Warning: prompt {prompt.id} has stale "
                            f"retry_not_before={prompt.retry_not_before} during shutdown "
                            f"(expected None); clearing. Check call order in _execute_prompt().",
                            file=sys.stderr,
                        )
                        prompt.clear_retry_backoff()
                    prompt.status = PromptStatus.QUEUED
                    prompt.add_log("Execution interrupted during shutdown")

            self.storage.save_queue_state(self.state)
            print("✓ Queue state saved")

        print("Queue manager stopped")

    def _process_queue_iteration(
        self, callback: Optional[Callable[[QueueState], None]] = None
    ) -> bool:
        """Process one iteration of the queue. Returns True if a prompt was executed."""
        previous_total_processed = self.state.total_processed if self.state else 0
        previous_failed_count = self.state.failed_count if self.state else 0
        previous_rate_limited_count = self.state.rate_limited_count if self.state else 0
        previous_last_processed = self.state.last_processed if self.state else None
        
        self.state = self.storage.load_queue_state()
        
        self.state.total_processed = max(self.state.total_processed, previous_total_processed)
        self.state.failed_count = max(self.state.failed_count, previous_failed_count)
        self.state.rate_limited_count = max(self.state.rate_limited_count, previous_rate_limited_count)
        if previous_last_processed and (not self.state.last_processed or self.state.last_processed < previous_last_processed):
            self.state.last_processed = previous_last_processed

        self._check_rate_limited_prompts()

        next_prompt = self.state.get_next_prompt()

        if next_prompt is None:
            rate_limited_prompts = [
                p for p in self.state.prompts if p.status == PromptStatus.RATE_LIMITED
            ]
            if rate_limited_prompts:
                reset_times = [
                    p.reset_time for p in rate_limited_prompts if p.reset_time is not None
                ]
                if reset_times:
                    soonest_reset = min(reset_times)
                    remaining = max(0, (soonest_reset - datetime.now()).total_seconds())
                    self._idle_wait_seconds = remaining
                    # Print once per wait state (count + reset target), not per poll.
                    wait_key = f"rl:{len(rate_limited_prompts)}:{soonest_reset:%Y-%m-%d %H:%M}"
                    if wait_key != self._last_wait_key:
                        self._last_wait_key = wait_key
                        hours, rest = divmod(int(remaining), 3600)
                        minutes = rest // 60
                        print(
                            f"Waiting for rate limit reset ({len(rate_limited_prompts)} prompt(s) rate limited, "
                            f"next reset at {soonest_reset:%H:%M} — in {hours}h {minutes:02d}m; "
                            f"sleeping until then, new prompts still picked up)"
                        )
                else:
                    self._idle_wait_seconds = None
                    wait_key = f"rl-heuristic:{len(rate_limited_prompts)}"
                    if wait_key != self._last_wait_key:
                        self._last_wait_key = wait_key
                        print(
                            f"Waiting for rate limit reset ({len(rate_limited_prompts)} prompt(s) rate limited, "
                            f"no reset time parsed — retrying every 5 minutes)"
                        )
            else:
                now = datetime.now()
                cooldown_prompts = [
                    p for p in self.state.prompts
                    if p.status == PromptStatus.QUEUED
                    and p.retry_not_before is not None
                    and now < p.retry_not_before
                ]
                if cooldown_prompts:
                    soonest = min(p.retry_not_before for p in cooldown_prompts)
                    wait = max(0, (soonest - now).total_seconds())
                    self._idle_wait_seconds = wait
                    wait_key = f"cooldown:{len(cooldown_prompts)}:{soonest:%H:%M:%S}"
                    if wait_key != self._last_wait_key:
                        self._last_wait_key = wait_key
                        print(
                            f"Waiting for retry cooldown ({len(cooldown_prompts)} prompt(s) in backoff, "
                            f"next eligible in {max(1, int(wait))}s)"
                        )
                else:
                    self._idle_wait_seconds = None
                    if self._last_wait_key != "empty":
                        self._last_wait_key = "empty"
                        print("No prompts in queue")

            # Fix S8: _check_rate_limited_prompts() may have transitioned prompts to
            # FAILED. Save state so those transitions are persisted even when no prompt
            # is executed this iteration.
            self.storage.save_queue_state(self.state)

            if callback:
                callback(self.state)
            return False

        # New work resets the wait-state dedup so the next wait announces itself.
        self._last_wait_key = None
        self._idle_wait_seconds = None

        print(f"Executing prompt {next_prompt.id}: {next_prompt.content[:50]}...")
        self._execute_prompt(next_prompt)

        self.storage.save_queue_state(self.state)

        if callback:
            callback(self.state)
        return True

    def _check_rate_limited_prompts(self) -> None:
        """Check if any rate-limited prompts should be retried."""
        current_time = datetime.now()

        for prompt in self.state.prompts:
            if prompt.status != PromptStatus.RATE_LIMITED:
                continue

            # Fix 4c: use reset_time as the authoritative gate when available.
            # The 5-minute heuristic is a fallback for when no reset_time was parsed.
            if prompt.reset_time is not None:
                if current_time < prompt.reset_time:
                    continue  # reset window not yet reached — stay RATE_LIMITED
                # reset_time has passed → fall through to retry/fail logic
            elif not (
                prompt.rate_limited_at
                and current_time >= prompt.rate_limited_at + timedelta(minutes=5)
            ):
                continue  # heuristic window not yet elapsed — stay RATE_LIMITED

            if prompt.can_retry():
                prompt.status = PromptStatus.QUEUED
                prompt.clear_retry_backoff()    # Fix 3: clear so the re-queued prompt is immediately
                                                # selectable by get_next_prompt().
                prompt.add_log("Retrying after rate limit cooldown")
                print(f"✓ Prompt {prompt.id} ready for retry after cooldown")
            else:
                prompt.status = PromptStatus.FAILED
                prompt.clear_retry_backoff()    # Fix 3: clear stale field for YAML cleanliness
                prompt.add_log(f"Max retries ({prompt.max_retries}) exceeded")
                print(f"✗ Prompt {prompt.id} failed - max retries exceeded")

    def _execute_prompt(self, prompt: QueuedPrompt) -> None:
        """Execute a single prompt."""
        prompt.status = PromptStatus.EXECUTING
        prompt.clear_retry_backoff()    # consumed; clear so it doesn't persist into .executing.md
        prompt.last_executed = datetime.now()
        retries_str = "∞" if prompt.max_retries == -1 else str(prompt.max_retries)
        prompt.add_log(
            f"Started execution (attempt {prompt.retry_count + 1}/{retries_str})"
        )

        self.storage.save_queue_state(self.state)

        # execute_prompt() may raise KeyboardInterrupt (Ctrl+C path).
        # _process_execution_result() is intentionally bypassed in that case;
        # the prompt stays EXECUTING for _shutdown() to revert to QUEUED.
        result = self.claude_interface.execute_prompt(prompt)

        self._process_execution_result(prompt, result)

    def _process_execution_result(
        self, prompt: QueuedPrompt, result: ExecutionResult
    ) -> None:
        """Process the result of prompt execution."""
        execution_summary = f"Execution completed in {result.execution_time:.1f}s"

        if result.success:
            # retry_not_before is already None — cleared by _execute_prompt() via clear_retry_backoff().
            prompt.status = PromptStatus.COMPLETED
            prompt.add_log(f"{execution_summary} - SUCCESS")
            if result.output:
                prompt.add_log(f"Output:\n{result.output}")

            self.state.total_processed += 1
            print(
                f"✓ Prompt {prompt.id} completed successfully "
                f"in {result.execution_time:.0f}s"
            )
            if result.output:
                summary = result.output.strip()
                if len(summary) > 1200:
                    summary = summary[:1200].rstrip() + "\n… (truncated)"
                print(f"--- Output ---\n{summary}\n--- (full output saved to completed/) ---")

        elif result.is_non_retryable:
            # Fix B — Non-retryable error: fail immediately, skip retry counter and can_retry().
            # Placing is_non_retryable before is_rate_limited in the chain ensures it always
            # takes precedence; a check nested inside else: would be silently bypassed if both
            # flags were ever set simultaneously.
            prompt.status = PromptStatus.FAILED
            prompt.clear_retry_backoff()    # Fix 3: clear stale field for YAML cleanliness
            prompt.add_log(f"{execution_summary} - FAILED (non-retryable error, retry budget preserved)")
            if result.error:
                prompt.add_log(f"Error: {result.error}")
            self.state.failed_count += 1
            print(
                f"✗ Prompt {prompt.id} failed permanently (non-retryable error, no retry)"
            )

        elif result.is_rate_limited:
            # Fix S4: prompt.status is EXECUTING at this point — checking it against
            # RATE_LIMITED always returns False. Use rate_limited_at (persisted from
            # the previous rate-limit event) as the authoritative signal instead.
            was_already_rate_limited = prompt.rate_limited_at is not None
            prompt.status = PromptStatus.RATE_LIMITED
            prompt.rate_limited_at = datetime.now()
            prompt.retry_count += 1
            # Fix S1: propagate the parsed reset_time onto the prompt so it survives
            # the next file reload and _check_rate_limited_prompts() can use it.
            # .replace(tzinfo=None) guards same-session comparisons against tz-aware
            # datetimes constructed directly (e.g. in tests); _cap_reset_time() already
            # strips tzinfo in the normal production path via _detect_rate_limit().
            if result.rate_limit_info and result.rate_limit_info.reset_time is not None:
                prompt.reset_time = result.rate_limit_info.reset_time.replace(tzinfo=None)
            else:
                prompt.reset_time = None  # clear any stale value; fall back to heuristic

            prompt.add_log(f"{execution_summary} - RATE LIMITED")
            if result.rate_limit_info and result.rate_limit_info.limit_message:
                source_tag = (
                    " [via stdout]"
                    if result.rate_limit_info.detection_source == "stdout"
                    else ""
                )
                prompt.add_log(f"Message{source_tag}: {result.rate_limit_info.limit_message}")

            if not was_already_rate_limited and self.state is not None:
                self.state.rate_limited_count += 1
            if prompt.reset_time is not None:
                print(
                    f"⚠ Prompt {prompt.id} rate limited, will retry after reset at "
                    f"{prompt.reset_time:%H:%M}"
                )
            else:
                print(f"⚠ Prompt {prompt.id} rate limited, will retry in 5 minutes")

            self._cleanup_rate_limit_artifacts(prompt, result.session_id)

        else:
            prompt.retry_count += 1

            if prompt.can_retry():
                prompt.status = PromptStatus.QUEUED
                # Fix 3 — Set retry_not_before so get_next_prompt() skips this prompt
                # for self._generic_failure_retry_delay seconds, preventing a spin loop.
                prompt.retry_not_before = datetime.now() + timedelta(
                    seconds=self._generic_failure_retry_delay
                )
                prompt.add_log(
                    f"{execution_summary} - FAILED "
                    f"(will retry in {self._generic_failure_retry_delay}s)"
                )
                if result.error:
                    prompt.add_log(f"Error: {result.error}")
                print(
                    f"✗ Prompt {prompt.id} failed, will retry in "
                    f"{self._generic_failure_retry_delay}s "
                    f"({prompt.retry_count}/{'∞' if prompt.max_retries == -1 else prompt.max_retries})"
                )
            else:
                prompt.status = PromptStatus.FAILED
                prompt.clear_retry_backoff()    # Fix 3: clear stale field for YAML cleanliness
                prompt.add_log(f"{execution_summary} - FAILED (max retries exceeded)")
                if result.error:
                    prompt.add_log(f"Error: {result.error}")

                self.state.failed_count += 1
                retries_str = "∞" if prompt.max_retries == -1 else str(prompt.max_retries)
                print(
                    f"✗ Prompt {prompt.id} failed permanently after {retries_str} attempts"
                )

        self.state.last_processed = datetime.now()

    def _cleanup_rate_limit_artifacts(
        self, prompt: QueuedPrompt, session_id: Optional[str]
    ) -> None:
        """Remove the artifact files this rate-limited execution left behind.

        A rate-limited ``claude --print`` still writes a conversation log, a todo
        stub, a debug transcript and telemetry events. Left alone they accumulate
        (thousands per rate-limit window) until the Claude Code UI crawls loading
        the junk history.

        Deletion is keyed on *session_id* — the UUID this queue generated and
        passed to the CLI via ``--session-id`` — so every path targeted is an exact
        match for a file this execution created. When *session_id* is ``None`` (the
        installed CLI predates the flag) nothing is deleted: identifying the files
        heuristically, by size and mtime, risks destroying an unrelated session's
        history.

        Failures are logged and swallowed. Propagating would skip
        save_queue_state(), leaving the prompt as ``.executing.md`` on disk and
        causing a re-queue loop on the next start.
        """
        if session_id is None:
            return

        try:
            deleted = self._do_cleanup_rate_limit_artifacts(session_id)
        except Exception as e:
            prompt.add_log(f"Warning: artifact cleanup failed: {e}")
            print(f"Warning: artifact cleanup failed: {e}")
            return

        if deleted:
            prompt.add_log(f"Cleaned up {deleted} rate-limit artifact(s)")
            print(f"[cleanup] Removed {deleted} rate-limit artifact(s)")

    @staticmethod
    def _do_cleanup_rate_limit_artifacts(session_id: str) -> int:
        """Delete *session_id*'s artifacts; return how many files were removed.

        IMPORTANT: this depends on Claude Code's internal layout under the config
        directory (``projects/``, ``todos/``, ``debug/``, ``telemetry/``). That
        layout is undocumented and may change between versions; if it does,
        cleanup silently stops finding files, which is safe — nothing outside
        these four session-scoped names is ever touched.

        The conversation log is found with a ``projects/*/<uuid>.jsonl`` glob
        rather than by rebuilding Claude Code's encoded project-directory name.
        That encoding rewrites ``.`` and ``_`` to ``-`` as well as ``/``, so
        recomputing it silently misses any project path containing those
        characters. A session UUID is unique on its own, which makes the glob both
        simpler and exact.
        """
        claude_dir = claude_config_dir()
        targets = [
            # Conversation log — parent directory name is unknown, matched by UUID.
            *claude_dir.glob(f"projects/*/{session_id}.jsonl"),
            # Todo stub — deterministic name.
            claude_dir / "todos" / f"{session_id}-agent-{session_id}.json",
            # Debug transcript — deterministic name.
            claude_dir / "debug" / f"{session_id}.txt",
            # Telemetry events — trailing UUID is unknown, matched by glob.
            *claude_dir.glob(f"telemetry/1p_failed_events.{session_id}.*.json"),
        ]

        deleted = 0
        for target in targets:
            try:
                target.unlink()
                deleted += 1
            except OSError:
                pass  # never created by this run, already gone, or inaccessible
        return deleted

    def _format_duration(self, seconds: float) -> str:
        """Format duration in seconds to human readable format."""
        if seconds < 0:
            return "now"

        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            return f"{minutes}m"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            if minutes == 0:
                return f"{hours}h"
            return f"{hours}h {minutes}m"

    def add_prompt(self, prompt: QueuedPrompt) -> bool:
        """Add a prompt to the queue."""
        try:
            if not self.state:
                self.state = self.storage.load_queue_state()

            self.state.add_prompt(prompt)

            success = self.storage.save_queue_state(self.state)
            if success:
                print(f"✓ Added prompt {prompt.id} to queue")
            else:
                print(f"✗ Failed to save prompt {prompt.id}")

            return success

        except Exception as e:
            print(f"Error adding prompt: {e}")
            return False

    def remove_prompt(self, prompt_id: str) -> bool:
        """Remove a prompt from the queue."""
        try:
            if not self.state:
                self.state = self.storage.load_queue_state()

            prompt = self.state.get_prompt(prompt_id)
            if prompt:
                if prompt.status == PromptStatus.EXECUTING:
                    print(f"Cannot remove executing prompt {prompt_id}")
                    return False

                prompt.status = PromptStatus.CANCELLED
                prompt.add_log("Cancelled by user")

                success = self.storage.save_queue_state(self.state)
                if success:
                    print(f"✓ Cancelled prompt {prompt_id}")
                else:
                    print(f"✗ Failed to cancel prompt {prompt_id}")

                return success
            else:
                print(f"Prompt {prompt_id} not found")
                return False

        except Exception as e:
            print(f"Error removing prompt: {e}")
            return False

    def get_status(self) -> QueueState:
        """Get current queue status."""
        if not self.state:
            self.state = self.storage.load_queue_state()
        return self.state

    def create_prompt_template(self, filename: str, priority: int = 0) -> str:
        """Create a prompt template file."""
        file_path = self.storage.create_prompt_template(filename, priority)
        return str(file_path)

    def save_prompt_to_bank(self, template_name: str, priority: int = 0) -> str:
        """Save a prompt template to the bank."""
        file_path = self.storage.save_prompt_to_bank(template_name, priority)
        return str(file_path)

    def list_bank_templates(self) -> list:
        """List all templates in the bank."""
        return self.storage.list_bank_templates()

    def use_bank_template(self, template_name: str) -> bool:
        """Use a template from the bank by copying it to the queue."""
        try:
            prompt = self.storage.use_bank_template(template_name)
            if not prompt:
                print(f"Template '{template_name}' not found in bank")
                return False

            if not self.state:
                self.state = self.storage.load_queue_state()

            self.state.add_prompt(prompt)
            success = self.storage.save_queue_state(self.state)
            
            if success:
                print(f"✓ Added prompt {prompt.id} from template '{template_name}' to queue")
            else:
                print(f"✗ Failed to save prompt from template '{template_name}'")
            
            return success

        except Exception as e:
            print(f"Error using bank template '{template_name}': {e}")
            return False

    def delete_bank_template(self, template_name: str) -> bool:
        """Delete a template from the bank."""
        success = self.storage.delete_bank_template(template_name)
        if success:
            print(f"✓ Deleted template '{template_name}' from bank")
        else:
            print(f"✗ Template '{template_name}' not found in bank")
        return success

    def get_rate_limit_info(self) -> Dict[str, Any]:
        """Get basic rate limit information for testing."""
        if not self.state:
            self.state = self.storage.load_queue_state()

        current_time = datetime.now()
        rate_limited_prompts = [
            p for p in self.state.prompts if p.status == PromptStatus.RATE_LIMITED
        ]

        info = {
            "current_time": current_time,
            "has_rate_limited_prompts": len(rate_limited_prompts) > 0,
            "rate_limited_count": len(rate_limited_prompts),
            "prompts": [],
        }

        for prompt in rate_limited_prompts:
            info["prompts"].append(
                {
                    "id": prompt.id,
                    "rate_limited_at": prompt.rate_limited_at,
                    "retry_count": prompt.retry_count,
                    "max_retries": prompt.max_retries,
                }
            )

        return info
