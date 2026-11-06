"""Event emission through the open-source Segment SDK (segment-analytics-python).

One `SimClient` per source (vertical x platform) per worker. `SimClient`
subclasses the SDK `Client`, replaces `_enqueue` and runs `SimConsumer` threads so that:
  * the platform-specific `context.library` we set (analytics-ios, analytics.js ...)
    is preserved instead of being overwritten with analytics-python;
  * the per-message `clean()` + extra `json.dumps` size check are skipped
    (our payloads are already JSON-safe) which roughly doubles throughput;
  * queue-full drops, oversize (>32 KB) drops, upload failures and anything left
    undelivered after the drain timeout are all counted instead of silently logged.
Batching, consumer threads, retries/backoff, gzip, auth and `sentAt` all remain
the SDK's own code paths (`Consumer` + `request.post` to `<host>/v1/batch`).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from requests.adapters import HTTPAdapter
from segment.analytics import request as _seg_request
from segment.analytics.client import Client
from segment.analytics.consumer import BATCH_SIZE_LIMIT, MAX_MSG_SIZE, Consumer
from segment.analytics.request import DatetimeSerializer

from .config import Destinations, SdkConfig

log = logging.getLogger("trafficsim.emitter")
UTC = timezone.utc

# All SDK clients in a process share one requests.Session; the default pool of
# 10 connections is too small for (sources x threads) consumers, so widen it.
for _scheme in ("http://", "https://"):
    _seg_request._session.mount(_scheme, HTTPAdapter(pool_connections=32, pool_maxsize=64))


class _Counter:
    __slots__ = ("batches", "failed", "lock", "oversize", "sent")

    def __init__(self):
        self.sent = 0
        self.batches = 0
        self.failed = 0
        self.oversize = 0
        self.lock = threading.Lock()


class SimConsumer(Consumer):
    """SDK consumer thread with delivery accounting.

    `request` counts events only after the SDK's own retrying upload succeeds.
    `next` mirrors the SDK 2.4 batching loop but acknowledges (`task_done`)
    and counts messages it drops for exceeding 32 KB. Upstream skips the
    acknowledgement, which makes `queue.join()`-style draining hang.
    """

    def __init__(self, counter: _Counter, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.counter = counter

    def request(self, batch):
        res = super().request(batch)
        with self.counter.lock:
            self.counter.sent += len(batch)
            self.counter.batches += 1
        return res

    def next(self):
        q = self.queue
        items: list = []
        start = time.monotonic()
        total = 0
        while len(items) < self.upload_size:
            elapsed = time.monotonic() - start
            if elapsed >= self.upload_interval:
                break
            try:
                item = q.get(block=True, timeout=self.upload_interval - elapsed)
            except queue.Empty:
                break
            try:
                size = len(json.dumps(item, cls=DatetimeSerializer).encode())
            except Exception:  # unserialisable message: count it, never lose the ack
                log.exception("dropping unserialisable message")
                size = MAX_MSG_SIZE + 1
            if size > MAX_MSG_SIZE:
                q.task_done()
                with self.counter.lock:
                    self.counter.oversize += 1
                continue
            items.append(item)
            total += size
            if total >= BATCH_SIZE_LIMIT:
                break
        return items


class SimClient(Client):
    def __init__(self, write_key: str, host: str, sdk: SdkConfig, send: bool = True):
        self.counter = _Counter()
        self.dropped = 0
        self.enqueued = 0
        self.undelivered = 0
        # Build the SDK client with send=False so it neither starts its own consumer
        # nor registers an atexit join; our counted consumers replace it below.
        super().__init__(
            write_key=write_key,
            host=host,
            max_queue_size=sdk.max_queue_size,
            send=False,
            on_error=self._on_error,
            gzip=sdk.gzip,
            max_retries=sdk.max_retries,
            timeout=sdk.timeout,
            thread=1,
            upload_size=sdk.upload_size,
            upload_interval=sdk.upload_interval,
        )
        self.send = send
        self.consumers = (
            [
                SimConsumer(
                    self.counter,
                    self.queue,
                    write_key,
                    host=host,
                    on_error=self._on_error,
                    upload_size=sdk.upload_size,
                    upload_interval=sdk.upload_interval,
                    gzip=sdk.gzip,
                    retries=sdk.max_retries,
                    timeout=sdk.timeout,
                )
                for _ in range(max(1, sdk.threads))
            ]
            if send
            else []
        )
        for c in self.consumers:
            c.start()

    def _on_error(self, exc, batch):
        with self.counter.lock:
            self.counter.failed += len(batch)
        log.warning("segment upload failed for %d events: %s", len(batch), exc)

    def _enqueue(self, msg):
        ts = msg.get("timestamp")
        if ts is None:
            ts = datetime.now(UTC)
        msg["timestamp"] = ts.isoformat(timespec="milliseconds") if isinstance(ts, datetime) else str(ts)
        if msg.get("messageId") is None:
            msg["messageId"] = str(uuid4())
        for k in ("userId", "anonymousId", "category", "name"):
            if k in msg and msg[k] is None:
                del msg[k]
        if not msg.get("integrations"):
            msg.pop("integrations", None)
        if not self.send:
            self.enqueued += 1
            return True, msg
        try:
            self.queue.put(msg, block=False)
            self.enqueued += 1
            return True, msg
        except queue.Full:
            self.dropped += 1
            return False, msg

    def pending(self) -> int:
        return self.queue.qsize()

    def drain(self, timeout: float) -> bool:
        """Wait (bounded) for every queued event to be uploaded or failed, then stop consumers.

        Anything still unacknowledged at the deadline is reported as `undelivered`.
        """
        deadline = time.monotonic() + timeout
        ok = True
        if self.send:
            while self.queue.unfinished_tasks > 0:
                if time.monotonic() > deadline:
                    ok = False
                    break
                time.sleep(0.05)
        for c in self.consumers:
            c.pause()
        for c in self.consumers:
            c.join(timeout=max(0.1, min(2.0, deadline - time.monotonic())))
        self.undelivered = self.queue.unfinished_tasks if self.send else 0
        return ok


class Emitter:
    """Routes session events to the right SimClient and records stats."""

    def __init__(
        self,
        destinations: Destinations,
        sdk: SdkConfig,
        pairs,
        stats,
        send: bool = True,
        sample_rate: float = 0.0,
        sample_path: str | None = None,
        rng=None,
    ):
        self.stats = stats
        self.clients: dict[tuple[str, str], SimClient] = {}
        for v, p in pairs:
            self.clients[(v, p)] = SimClient(destinations.write_key(v, p), destinations.host, sdk, send=send)
        self.sample_rate = sample_rate
        self.rng = rng
        # kept open for the run (closed in close()); a context manager does not fit
        self._sample_fh = open(sample_path, "a", encoding="utf-8") if sample_rate > 0 and sample_path else None  # noqa: SIM115

    @staticmethod
    def _ts(s) -> datetime:
        return datetime.fromtimestamp(s.now, UTC)

    @staticmethod
    def _ctx(s) -> dict:
        if s.platform == "web":
            page = s.data.get("page")
            if page:
                return {**s.ctx, "page": page}
        return s.ctx

    def _record(self, s, kind: str, name: str, key: str | None, res) -> None:
        ok, msg = res
        s.seq += 1
        self.stats.event(s.vertical, s.platform, kind, name, key, ok)
        if self._sample_fh is not None and self.rng.random() < self.sample_rate:
            self._sample_fh.write(json.dumps(msg, default=str) + "\n")

    def track(self, s, key: str, name: str, props: dict) -> None:
        c = self.clients[(s.vertical, s.platform)]
        res = c.track(user_id=s.user_id, event=name, properties=props, context=self._ctx(s), timestamp=self._ts(s), anonymous_id=s.anon_id)
        self._record(s, "track", name, key, res)

    def page(self, s, name: str, props: dict, category: str | None = None) -> None:
        c = self.clients[(s.vertical, s.platform)]
        res = c.page(
            user_id=s.user_id,
            category=category,
            name=name,
            properties=props,
            context=self._ctx(s),
            timestamp=self._ts(s),
            anonymous_id=s.anon_id,
        )
        self._record(s, "page", name, None, res)

    def screen(self, s, name: str, props: dict, category: str | None = None) -> None:
        c = self.clients[(s.vertical, s.platform)]
        res = c.screen(
            user_id=s.user_id, category=category, name=name, properties=props, context=s.ctx, timestamp=self._ts(s), anonymous_id=s.anon_id
        )
        self._record(s, "screen", name, None, res)

    def identify(self, s, traits: dict) -> None:
        c = self.clients[(s.vertical, s.platform)]
        res = c.identify(user_id=s.user_id, traits=dict(traits), context=self._ctx(s), timestamp=self._ts(s), anonymous_id=s.anon_id)
        self._record(s, "identify", "identify", None, res)

    def alias(self, s, previous_id: str) -> None:
        c = self.clients[(s.vertical, s.platform)]
        res = c.alias(previous_id=previous_id, user_id=s.user_id, context=self._ctx(s), timestamp=self._ts(s))
        self._record(s, "alias", "alias", None, res)

    # ---- health
    def sdk_snapshot(self) -> dict:
        sent = failed = batches = dropped = depth = enq = oversize = undelivered = 0
        for c in self.clients.values():
            sent += c.counter.sent
            failed += c.counter.failed
            batches += c.counter.batches
            oversize += c.counter.oversize
            dropped += c.dropped
            depth += c.pending()
            enq += c.enqueued
            undelivered += c.undelivered
        return {
            "sent": sent,
            "failed": failed,
            "batches": batches,
            "dropped": dropped,
            "queue_depth": depth,
            "enqueued": enq,
            "oversize": oversize,
            "undelivered": undelivered,
        }

    def max_queue_fill(self) -> float:
        return max((c.pending() / c.queue.maxsize for c in self.clients.values() if c.queue.maxsize), default=0.0)

    def close(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        results = [c.drain(max(0.1, deadline - time.monotonic())) for c in self.clients.values()]  # drain every client
        ok = all(results)
        if self._sample_fh:
            self._sample_fh.close()
        return ok


class RecordingEmitter(Emitter):
    """Test/dry-run emitter that keeps messages in memory instead of using the SDK."""

    def __init__(self, stats=None):
        self.stats = stats or _NullStats()
        self.messages: list[dict] = []
        self.clients = {}
        self._sample_fh = None

    def _push(self, s, msg, kind, name, key):
        msg.update(
            {
                "timestamp": datetime.fromtimestamp(s.now, UTC).isoformat(timespec="milliseconds"),
                "anonymousId": s.anon_id,
                "context": self._ctx(s),
            }
        )
        if s.user_id:
            msg["userId"] = s.user_id
        self.messages.append(msg)
        s.seq += 1
        self.stats.event(s.vertical, s.platform, kind, name, key, True)

    def track(self, s, key, name, props):
        self._push(s, {"type": "track", "event": name, "properties": props, "_key": key}, "track", name, key)

    def page(self, s, name, props, category=None):
        self._push(s, {"type": "page", "name": name, "properties": props}, "page", name, None)

    def screen(self, s, name, props, category=None):
        self._push(s, {"type": "screen", "name": name, "properties": props}, "screen", name, None)

    def identify(self, s, traits):
        self._push(s, {"type": "identify", "traits": traits}, "identify", "identify", None)

    def alias(self, s, previous_id):
        self._push(s, {"type": "alias", "previousId": previous_id}, "alias", "alias", None)

    def sdk_snapshot(self):
        n = len(self.messages)
        return {"sent": n, "failed": 0, "batches": 0, "dropped": 0, "queue_depth": 0, "enqueued": n, "oversize": 0, "undelivered": 0}

    def max_queue_fill(self):
        return 0.0

    def close(self, timeout=0):
        return True


class _NullStats:
    def event(self, *a, **k):
        pass
