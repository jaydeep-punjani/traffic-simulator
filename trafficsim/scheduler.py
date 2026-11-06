"""Min-heap scheduler of sessions keyed by their next simulated wake-up time."""

from __future__ import annotations

import heapq


class Scheduler:
    __slots__ = ("heap", "_wake")

    def __init__(self):
        self.heap: list = []
        self._wake: dict[int, float] = {}  # sid -> authoritative wake time (stale heap entries are skipped)

    def __len__(self) -> int:
        return len(self._wake)

    def push(self, t: float, session) -> None:
        self._wake[session.sid] = t
        heapq.heappush(self.heap, (t, session.sid, session))

    def peek_time(self) -> float | None:
        heap = self.heap
        while heap:
            t, sid, _ = heap[0]
            if self._wake.get(sid) == t:
                return t
            heapq.heappop(heap)
        return None

    def pop_due(self, now: float):
        """Pop the next session due at or before `now`; returns (t, session) or None."""
        heap = self.heap
        wake = self._wake
        while heap and heap[0][0] <= now:
            t, sid, s = heapq.heappop(heap)
            if wake.get(sid) == t:
                return t, s
        return None

    def discard(self, session) -> None:
        self._wake.pop(session.sid, None)
