"""Run exports: summary.json, timeseries.csv (+ sampled payloads written by workers).

The coordinator appends every 1 Hz point to timeseries.jsonl while the run is
live, so the final CSV covers the whole run even when it outlives the
in-memory history the dashboard uses.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from .timeutil import iso_ms

SERIES_COLUMNS = [
    "wall",
    "elapsed",
    "sim_time",
    "sim_time_iso",
    "speed",
    "paused",
    "active",
    "target",
    "eps",
    "sessions_started",
    "sessions_ended",
    "sdk_sent_rate",
    "sdk_failed_rate",
    "sdk_dropped_rate",
    "queue_depth",
    "lag",
    "total_events",
]


def flatten_point(p: dict) -> dict:
    row = {k: p.get(k) for k in SERIES_COLUMNS if k in p}
    row["sim_time_iso"] = iso_ms(p["sim_time"])
    row["queue_depth"] = p.get("sdk", {}).get("queue_depth", 0)
    for group in ("active_by", "target_by", "eps_by_type", "eps_by_vertical", "eps_by_platform"):
        for k, v in (p.get(group) or {}).items():
            row[f"{group}.{k}"] = v
    return row


def _write_csv(series: Iterable[dict], f) -> None:
    rows = [flatten_point(p) for p in series]
    cols = list(SERIES_COLUMNS)
    cols += sorted({k for r in rows for k in r} - set(cols))
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)


def timeseries_csv_text(series: Iterable[dict]) -> str:
    buf = io.StringIO()
    _write_csv(series, buf)
    return buf.getvalue()


def write_timeseries_csv(series: Iterable[dict], path: Path) -> None:
    with open(path, "w", newline="") as f:
        _write_csv(series, f)


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:  # a torn final line from a crash
                    continue


def write_summary(summary: dict, path: Path) -> None:
    path.write_text(json.dumps(summary, indent=2, default=str))


def write_exports(out_dir: Path, summary: dict, series: Iterable[dict] | None = None) -> dict:
    """Write summary.json and timeseries.csv. Without `series`, the CSV is built from timeseries.jsonl."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_summary(summary, out_dir / "summary.json")
    jsonl = out_dir / "timeseries.jsonl"
    if series is None:
        series = read_jsonl(jsonl) if jsonl.exists() else []
    write_timeseries_csv(series, out_dir / "timeseries.csv")
    files = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    return {"dir": str(out_dir), "files": files}
