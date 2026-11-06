"""Duration / timestamp parsing helpers shared by scenarios, CLI and API."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DUR_RE = re.compile(r"([0-9]*\.?[0-9]+)\s*(ms|s|m|h|d|w)")


def parse_duration(value) -> float:
    """Parse '90s', '15m', '2h', '1d12h', '+2h', '-24h' or a number (seconds)."""
    if value is None:
        raise ValueError("duration is required")
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty duration")
    sign = 1.0
    if s[0] in "+-":
        sign = -1.0 if s[0] == "-" else 1.0
        s = s[1:]
    try:
        return sign * float(s)
    except ValueError:
        pass
    pos = 0
    total = 0.0
    for m in _DUR_RE.finditer(s):
        if m.start() != pos:
            raise ValueError(f"invalid duration: {value!r}")
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s) or pos == 0:
        raise ValueError(f"invalid duration: {value!r}")
    return sign * total


def parse_start_at(value, now: float | None = None) -> float:
    """Resolve a scenario start time to epoch seconds.

    Accepts 'now', a relative offset from now ('-24h', '+1h'), an ISO-8601
    timestamp, or an epoch number.
    """
    now = time.time() if now is None else now
    if value is None:
        return now
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s.lower() in ("", "now"):
        return now
    if s[0] in "+-":
        return now + parse_duration(s)
    iso = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso_ms(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"
