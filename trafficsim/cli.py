"""Command line: trafficsim run | serve | validate | list | mock-collector."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

from . import __version__
from .config import list_scenarios, load_destinations, load_scenario, parse_set_args
from .timeutil import fmt_duration, iso_ms


def _scenario_from_args(args):
    overrides = parse_set_args(args.set)
    for flag, key in (
        ("speed", "speed"),
        ("duration", "duration"),
        ("start_at", "start_at"),
        ("seed", "seed"),
        ("concurrency", "traffic.base_concurrency"),
        ("workers", "workers"),
        ("eps_cap", "events_per_sec_cap"),
        ("sample", "sample_payloads"),
    ):
        val = getattr(args, flag, None)
        if val is not None:
            overrides[key] = val
    return load_scenario(args.scenario, overrides)


def cmd_validate(args) -> int:
    import random

    from .emitter import RecordingEmitter
    from .journeys import Catalog
    from .planner import TrafficPlanner
    from .verticals import RUNTIMES

    names = [args.scenario] if args.scenario else [s["id"] for s in list_scenarios()]
    ok = True
    for name in names:
        try:
            sc = load_scenario(name, parse_set_args(args.set))
            planner = TrafficPlanner(sc)
            for v in planner.verticals:
                RUNTIMES[v].create(Catalog.load(v, sc.verticals[v]), RecordingEmitter(), random.Random(0))
            peak, at = planner.peak_estimate()
            dur = fmt_duration(planner.end - planner.start) if planner.end else "unbounded"
            print(
                f"OK   {name:<24} verticals={','.join(planner.verticals):<24} cohorts={len(planner.cohorts):<3} "
                f"duration={dur:<10} speed={sc.speed:g}x  peak≈{peak:,.0f} sessions at +{fmt_duration(at - planner.start)}"
            )
        except Exception as e:  # report every scenario, not just the first failure
            ok = False
            print(f"FAIL {name}: {e}")
    return 0 if ok else 1


def cmd_list(args) -> int:
    for s in list_scenarios():
        print(f"{s['id']:<24} {s['description']}")
    return 0


def _fmt_point(p: dict) -> str:
    sdk = p.get("sdk", {})
    ov = ",".join(f"{o['name']}({o['intensity']:.2f})" for o in p.get("overlays", []))
    return (
        f"[{p['elapsed']:7.1f}s] sim {p['sim_time_iso'][:19]}  active {p['active']:>7,}/{p['target']:>9,.0f}  "
        f"eps {p['eps']:>9,.0f}  sent/s {p['sdk_sent_rate']:>9,.0f}  queue {sdk.get('queue_depth', 0):>6,}  "
        f"failed {sdk.get('failed', 0):,} dropped {sdk.get('dropped', 0):,}  lag {p['lag']:.0f}s  {ov}"
    )


def cmd_run(args) -> int:
    from .runner import Run

    sc = _scenario_from_args(args)
    dest = load_destinations(args.destinations, args.host)
    run = Run(sc, dest, send=not args.dry_run, runs_dir=args.out, workers=args.workers)
    print(
        f"run {run.id}: scenario={sc.name} workers={run.n_workers} speed={sc.speed:g}x start={iso_ms(run.start_sim)} "
        f"duration={sc.duration} planned peak≈{run.peak:,.0f} sessions  -> {dest.host if not args.dry_run else 'DRY RUN'}"
    )
    every = max(1, int(args.print_every))
    count = [0]

    def show(p):
        if "summary" in p:
            return
        count[0] += 1
        if count[0] % every == 0:
            print(_fmt_point(p), flush=True)

    run.listeners.append(show)
    for w in run.warnings:
        print(f"warning: {w}", file=sys.stderr)
    signals = [0]

    def on_signal(signum, frame):
        signals[0] += 1
        if signals[0] > 1:
            print("\nsecond signal: exiting without draining", file=sys.stderr, flush=True)
            raise KeyboardInterrupt
        print("\nstopping (draining SDK queues; press Ctrl-C again to abort)...", flush=True)
        run.stop()

    # Installed before workers start so an early Ctrl-C still drains instead of orphaning them.
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    run.start()
    deadline = time.monotonic() + args.max_wall if args.max_wall else None
    while not run.wait(0.5):
        if deadline and time.monotonic() > deadline and run.state not in ("stopping", "finishing", "finished"):
            print(f"--max-wall {args.max_wall}s reached, stopping...", flush=True)
            run.stop()
    s = run.summary()
    print(
        json.dumps(
            {
                k: s[k]
                for k in (
                    "run_id",
                    "total_events",
                    "avg_eps",
                    "peak_eps",
                    "peak_active_sessions",
                    "sessions_started",
                    "sessions_trimmed",
                    "journey_errors",
                    "max_lag_sim_seconds",
                    "sdk",
                    "sim_elapsed_s",
                    "wall_elapsed_s",
                    "errors",
                    "warnings",
                )
            },
            indent=2,
        )
    )
    print(f"exports: {run.exports['dir'] if run.exports else '-'}")
    return 1 if s["errors"] else 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api.app import create_app

    app = create_app(destinations_path=args.destinations, host_override=args.host, runs_dir=args.out)
    print(f"dashboard: http://{args.bind}:{args.port}/")
    uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")
    return 0


def cmd_mock(args) -> int:
    from .mock_collector import serve

    serve(args.bind, args.port, args.sample_every)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trafficsim", description="Application traffic simulator (Segment SDK)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_run(p):
        p.add_argument("--set", action="append", metavar="KEY=VALUE", help="override any scenario field (dotted keys)")
        p.add_argument("--destinations", help="destinations YAML (default config/destinations.yaml, else the example)")
        p.add_argument("--host", help="override collector host URL, e.g. http://localhost:8088")
        p.add_argument("--out", help="runs directory (default ./runs)")

    r = sub.add_parser("run", help="run a scenario headless")
    r.add_argument("scenario", help="scenario id (file in scenarios/) or path to YAML")
    r.add_argument("--speed", type=float)
    r.add_argument("--duration")
    r.add_argument("--start-at", dest="start_at", help="'now', '-24h', or ISO-8601")
    r.add_argument("--seed", type=int)
    r.add_argument("--concurrency", type=float, help="traffic.base_concurrency")
    r.add_argument("--workers", type=int)
    r.add_argument("--eps-cap", dest="eps_cap", type=float, help="global events/sec cap")
    r.add_argument("--sample", type=float, help="fraction of payloads to write to runs/<id>/payloads-w*.jsonl")
    r.add_argument("--dry-run", action="store_true", help="simulate without sending to the collector")
    r.add_argument("--max-wall", type=float, help="stop after this many wall-clock seconds")
    r.add_argument("--print-every", type=float, default=5, help="print a status line every N seconds")
    common_run(r)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("serve", help="start the dashboard + API")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--destinations")
    s.add_argument("--host", help="override collector host URL")
    s.add_argument("--out", help="runs directory (default ./runs)")
    s.set_defaults(fn=cmd_serve)

    v = sub.add_parser("validate", help="validate one or all scenarios")
    v.add_argument("scenario", nargs="?")
    v.add_argument("--set", action="append", metavar="KEY=VALUE")
    v.set_defaults(fn=cmd_validate)

    ls = sub.add_parser("list", help="list scenarios")
    ls.set_defaults(fn=cmd_list)

    m = sub.add_parser("mock-collector", help="run a local Segment-compatible collector")
    m.add_argument("--bind", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8088)
    m.add_argument("--sample-every", type=int, default=200, help="keep one sample payload every N batches")
    m.set_defaults(fn=cmd_mock)
    return ap


_NEGATIVE_VALUE_FLAGS = ("--start-at",)


def _join_negative_values(argv: list[str]) -> list[str]:
    """Rewrite `--start-at -24h` as `--start-at=-24h`.

    argparse before Python 3.12 reads a value starting with '-' as another option.
    """
    out: list[str] = []
    it = iter(argv)
    for tok in it:
        if tok in _NEGATIVE_VALUE_FLAGS:
            nxt = next(it, None)
            if nxt is not None and nxt.startswith("-") and nxt[1:2].isdigit():
                out.append(f"{tok}={nxt}")
                continue
            out.append(tok)
            if nxt is not None:
                out.append(nxt)
            continue
        out.append(tok)
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("segment").setLevel(logging.CRITICAL)
    logging.getLogger("backoff").setLevel(logging.CRITICAL)
    args = build_parser().parse_args(_join_negative_values(list(sys.argv[1:] if argv is None else argv)))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
