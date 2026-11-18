"""Tiny synchronous client-side rate limiter (injectable clock/sleep for tests)."""
import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(self, max_per_sec: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / max(max_per_sec, 0.1)
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until a request may be sent; spaces requests at least `interval` apart."""
        with self._lock:
            now = self._clock()
            delay = self._next - now
            if delay > 0:
                self._sleep(delay)
                now = self._next
            self._next = now + self.interval
