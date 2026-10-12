"""Per-task runtime state: counters, subprocess registry and deadline bookkeeping.

No cross-task mutable globals: every run gets its own instance.
"""

from __future__ import annotations

import threading
from datetime import datetime


class RunRuntime:
    def __init__(self, tool_timeout_seconds: float = 20.0) -> None:
        self.started_at = datetime.now().astimezone()
        self.tool_timeout_seconds = tool_timeout_seconds
        self.model_calls = 0
        self.tool_calls = 0
        self.model_prompt_tokens = 0
        self.model_completion_tokens = 0
        self.cost_rmb = 0.0
        self._processes: list = []
        self._lock = threading.Lock()

    def remaining_seconds(self, deadline: datetime) -> float:
        return (deadline - datetime.now(deadline.tzinfo)).total_seconds()

    def register_process(self, process) -> None:
        with self._lock:
            self._processes.append(process)

    def forget_process(self, process) -> None:
        with self._lock:
            if process in self._processes:
                self._processes.remove(process)

    def kill_all(self) -> None:
        """Best-effort termination of any still-running tool subprocesses."""
        import os
        import signal

        with self._lock:
            pending = list(self._processes)
            self._processes.clear()
        for process in pending:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError, PermissionError, OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except Exception:
                pass
