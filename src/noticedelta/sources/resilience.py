"""출처별 속도 제한과 서킷 브레이커 (설계 7.1, 8장)."""

from __future__ import annotations

import time
from collections.abc import Callable


class CircuitOpen(Exception):
    """서킷이 열려 있어 호출하지 않았다."""


class RateLimiter:
    def __init__(
        self,
        rate_per_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = 1.0 / rate_per_sec
        self.clock = clock
        self.sleep = sleep
        self._next: float | None = None

    def acquire(self) -> None:
        now = self.clock()
        if self._next is not None and now < self._next:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        reset_after_s: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_after_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.opened_at = self.clock()
