import time


class Clock:
    def now(self) -> int:
        return int(time.time())

    def monotonic(self) -> float:
        return time.monotonic()

    def monotonic_deadline(self, expires_at: int) -> float:
        return self.monotonic() + max(0, expires_at - self.now())
