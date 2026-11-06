"""Run exports: summary.json, timeseries.csv (+ sampled payloads written by workers)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .timeutil import iso_ms

SERIES_COLUMNS = ["wall", "elapsed", "sim_time", "sim_time_iso", "speed", "paused", "active", "target", "eps",
                  "sessions_started", "sessions_ended", "sdk_sent_rate", "sdk_failed_rate", "sdk_dropped_rate",
                  "queue_depth", "lag", "total_events"]


def flatten_point(p: dict) -> dict:
    row = {k: p.get(k) for k in SERIES_COLUMNS if k in p}
    row["sim_time_iso"] = iso_ms(p["sim_time"])
    row["queue_depth"] = p.get("sdk", {}).get("queue_depth", 0)
    for group in ("active_by", "target_by", "eps_by_type", "eps_by_vertical", "eps_by_platform"):
        for k, v in (p.get(group) or {}).items():
            row[f"{group}.{k}"] = v
    return row


def write_timeseries_csv(series, path: Path) -> None:
    rows = [flatten_point(p) for p in series]
    cols = list(SERIES_COLUMNS)
    extra = sorted({k for r in rows for k in r} - set(cols))
    cols += extra
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_summary(summary: dict, path: Path) -> None:
    path.write_text(json.dumps(summary, indent=2, default=str))


def write_exports(out_dir: Path, summary: dict, series) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_summary(summary, out_dir / "summary.json")
    write_timeseries_csv(series, out_dir / "timeseries.csv")
    files = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    return {"dir": str(out_dir), "files": files}
