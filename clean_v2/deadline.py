from __future__ import annotations

from contextlib import contextmanager
import math
import signal
import threading
from typing import Iterator


class StageDeadlineError(RuntimeError):
    """Infrastructure failure: the stage deadline expired or cannot be enforced."""


class StageDeadlineExceeded(StageDeadlineError):
    def __init__(self, stage: str, seconds: float) -> None:
        self.stage = stage
        self.deadline_seconds = seconds
        super().__init__(f"CLEAN_V2_STAGE_DEADLINE: {stage} exceeded {seconds:g}s")


class _DeadlineSignal(BaseException):
    # Provider routers catch Exception to advance their fallback route. Expiry must
    # leave that entire route, including repairs, rather than start another request.
    pass


@contextmanager
def stage_deadline(stage: str, seconds: float) -> Iterator[None]:
    """Enforce one wall-clock cap for synchronous work on the Linux production CLI."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("stage deadline must be finite and positive")
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise StageDeadlineError("CLEAN_V2_STAGE_DEADLINE: requires a POSIX main thread")
    if any(signal.getitimer(signal.ITIMER_REAL)):
        raise StageDeadlineError("CLEAN_V2_STAGE_DEADLINE: an existing timer is active")

    previous_handler = signal.getsignal(signal.SIGALRM)

    def expired(_signum, _frame):
        raise _DeadlineSignal()

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        except _DeadlineSignal:
            raise StageDeadlineExceeded(stage, seconds) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
