"""Min-heap scheduler of sessions keyed by their next simulated wake-up time."""

from __future__ import annotations

import heapq


class Scheduler:
    """Heap of (wake_time, sid) plus an authoritative sid -> (wake_time, session) map.

    Re-pushing a session supersedes its earlier entry; superseded heap entries are
    skipped lazily on pop. Heap tuples never contain the session object, so equal
    (time, sid) pairs can never fall back to comparing sessions.
    """

    __slots__ = ("_entries", "heap")

    def __init__(self):
        self.heap: list[tuple[float, int]] = []
        self._entries: dict[int, tuple[float, object]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def push(self, t: float, session) -> None:
        self._entries[session.sid] = (t, session)
        heapq.heappush(self.heap, (t, session.sid))

    def _valid(self, t: float, sid: int):
        e = self._entries.get(sid)
        return e is not None and e[0] == t

    def peek_time(self) -> float | None:
        heap = self.heap
        while heap:
            t, sid = heap[0]
            if self._valid(t, sid):
                return t
            heapq.heappop(heap)
        return None

    def pop_due(self, now: float):
        """Pop the next session due at or before `now`; returns (t, session) or None."""
        heap = self.heap
        entries = self._entries
        while heap and heap[0][0] <= now:
            t, sid = heapq.heappop(heap)
            e = entries.get(sid)
            if e is not None and e[0] == t:
                del entries[sid]
                return t, e[1]
        return None

    def discard(self, session) -> None:
        self._entries.pop(session.sid, None)
