"""A tiny Segment-compatible collector for local testing.

Accepts what the Segment SDKs send — `POST /v1/batch` (and single-call
`/v1/track|page|screen|identify|alias|group`), optional gzip, write key via
HTTP Basic auth or body `writeKey` — and counts events without storing them.
`GET /stats` returns totals and rates; `GET /sample` returns recent payloads.

Counting is a fast path: events per request = occurrences of `"messageId"` in the
body, which is exact for SDK-generated JSON but not a full parse. Request bodies
are capped (MAX_BODY / MAX_INFLATED) so a bad client cannot exhaust memory.
It is a test tool: bind it to localhost.
"""

from __future__ import annotations

import base64
import collections
import json
import threading
import time
import zlib

MAX_BODY = 4 * 1024 * 1024  # compressed request body
MAX_INFLATED = 16 * 1024 * 1024  # after gzip decoding (SDK batches are < 500 KB)

TYPES = (b"track", b"page", b"screen", b"identify", b"alias", b"group")
_TYPE_PATTERNS = {t.decode(): b'"type": "' + t + b'"' for t in TYPES}
_TYPE_PATTERNS_COMPACT = {t.decode(): b'"type":"' + t + b'"' for t in TYPES}


class Collector:
    def __init__(self, sample_every: int = 200, keep_samples: int = 50):
        self.lock = threading.Lock()
        self.sample_every = sample_every
        self.keep_samples = keep_samples
        self._clear()

    def _clear(self) -> None:
        self.started = time.time()
        self.events = 0
        self.batches = 0
        self.bytes = 0
        self.bad = 0
        self.by_key: collections.Counter[str] = collections.Counter()
        self.by_type: collections.Counter[str] = collections.Counter()
        self.samples: collections.deque = collections.deque(maxlen=self.keep_samples)
        self.window: collections.deque = collections.deque(maxlen=120)  # [wall second, events]
        self.skew: collections.deque = collections.deque(maxlen=500)  # sentAt - timestamp (seconds)

    def reset(self) -> None:
        with self.lock:
            self._clear()

    def reject(self) -> None:
        with self.lock:
            self.bad += 1

    def record(self, body: bytes, write_key: str | None) -> int:
        n = body.count(b'"messageId"')
        compact = b'"type":"' in body
        pats = _TYPE_PATTERNS_COMPACT if compact else _TYPE_PATTERNS
        counts = {t: body.count(p) for t, p in pats.items()}
        sec = int(time.time())
        if write_key is None:
            try:
                write_key = json.loads(body).get("writeKey")
            except (ValueError, AttributeError):
                write_key = None
        with self.lock:
            self.events += n
            self.batches += 1
            self.bytes += len(body)
            for t, c in counts.items():
                if c:
                    self.by_type[t] += c
            self.by_key[write_key or "?"] += n
            if self.window and self.window[-1][0] == sec:
                self.window[-1][1] += n
            else:
                self.window.append([sec, n])
            take_sample = self.batches % self.sample_every == 1 or self.sample_every <= 1
        if take_sample:
            try:
                doc = json.loads(body)
                msgs = doc.get("batch") or [doc]
                if msgs:
                    m = msgs[0]
                    skew = _skew(doc.get("sentAt"), m.get("timestamp"))
                    with self.lock:
                        self.samples.append({"received_at": time.time(), "sentAt": doc.get("sentAt"), "message": m})
                        if skew is not None:
                            self.skew.append(skew)
            except (ValueError, AttributeError):
                pass
        return n

    def sample_list(self) -> list:
        with self.lock:
            return list(self.samples)

    def stats(self) -> dict:
        with self.lock:
            now = int(time.time())
            last1 = sum(n for s, n in self.window if s == now - 1)
            last10 = sum(n for s, n in self.window if now - 10 <= s < now) / 10
            elapsed = max(1e-3, time.time() - self.started)
            skew = sorted(self.skew)
            return {
                "events": self.events,
                "batches": self.batches,
                "bytes": self.bytes,
                "bad_requests": self.bad,
                "eps_last_1s": last1,
                "eps_last_10s": round(last10, 1),
                "eps_avg": round(self.events / elapsed, 1),
                "peak_eps_1s": max((n for s, n in self.window if s < now), default=0),
                "uptime_s": round(elapsed, 1),
                "by_write_key": dict(self.by_key),
                "by_type": dict(self.by_type),
                "timestamp_skew_s_median": round(skew[len(skew) // 2], 1) if skew else None,
            }


def _skew(sent_at, ts) -> float | None:
    from datetime import datetime

    try:
        a = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00")).timestamp()
        b = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        return a - b
    except ValueError:
        return None


def _inflate(body: bytes) -> bytes | None:
    """Bounded gzip decode; None when invalid or larger than MAX_INFLATED."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(body, MAX_INFLATED)
    except zlib.error:
        return None
    if d.unconsumed_tail or not d.eof:
        return None
    return out


def _basic_user(headers: dict) -> str | None:
    auth = headers.get(b"authorization")
    if not auth or not auth.lower().startswith(b"basic "):
        return None
    try:
        return base64.b64decode(auth[6:]).decode().split(":", 1)[0]
    except Exception:
        return None


def make_app(collector: Collector | None = None):
    col = collector or Collector()

    async def send_json(send, status: int, payload) -> None:
        body = json.dumps(payload, default=str).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path = scope["path"].rstrip("/")
        method = scope["method"]
        if method == "POST" and path.startswith("/v1/"):
            chunks = []
            size = 0
            more = True
            while more:
                msg = await receive()
                chunk = msg.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY:
                    col.reject()
                    await send_json(send, 413, {"error": "body too large"})
                    return
                chunks.append(chunk)
                more = msg.get("more_body", False)
            body = b"".join(chunks)
            headers = dict(scope["headers"])
            if headers.get(b"content-encoding", b"").lower() == b"gzip":
                inflated = _inflate(body)
                if inflated is None:
                    col.reject()
                    await send_json(send, 400, {"error": "bad or oversized gzip body"})
                    return
                body = inflated
            col.record(body, _basic_user(headers))
            await send_json(send, 200, {"success": True})
        elif method == "GET" and path in ("", "/stats"):
            await send_json(send, 200, col.stats())
        elif method == "GET" and path == "/sample":
            await send_json(send, 200, col.sample_list())
        elif method in ("POST", "DELETE") and path == "/reset":
            col.reset()
            await send_json(send, 200, {"reset": True})
        else:
            await send_json(send, 404, {"error": "not found"})

    app.collector = col  # type: ignore[attr-defined]  # exposed for tests
    return app


def serve(host: str = "127.0.0.1", port: int = 8088, sample_every: int = 200) -> None:
    import uvicorn

    app = make_app(Collector(sample_every=sample_every))
    print(f"mock Segment collector listening on http://{host}:{port}  (POST /v1/batch, GET /stats, GET /sample)")
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False, backlog=4096)
