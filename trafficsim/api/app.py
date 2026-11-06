"""FastAPI app: scenarios, run control, live stats over WebSocket, exports, dashboard.

Security model (see docs/operations.md#security): there is no authentication, so
the server binds to 127.0.0.1 by default. Browsers are prevented from driving it
cross-site by an Origin check on state-changing requests and the WebSocket.
Remote callers can only name bundled scenarios/catalogs by id, never paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..config import (
    DEFAULT_PLATFORM_MIX,
    ID_RE,
    REGIONS,
    RUNS_DIR,
    VERTICAL_PLATFORMS,
    Scenario,
    apply_overrides,
    list_scenarios,
    load_destinations,
    load_scenario_raw,
    resolve_catalog_path,
    safe_load_strict,
    yaml_error_message,
)
from ..events import PRESETS
from ..export import timeseries_csv_text
from ..planner import TrafficPlanner
from ..runner import STOP_GRACE, Run

log = logging.getLogger("trafficsim.api")
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"


MAX_YAML_BYTES = 256 * 1024
MAX_OVERRIDES = 200
MAX_LIVE_OVERLAYS = 100
ACTIVE_STATES = ("running", "paused")


class StartRequest(BaseModel):
    scenario: str | None = Field(None, max_length=64)
    yaml: str | None = Field(None, max_length=MAX_YAML_BYTES)  # full scenario YAML (edited in the dashboard)
    overrides: dict[str, Any] = Field(default_factory=dict)  # dotted-key overrides
    dry_run: bool = False


class SpeedRequest(BaseModel):
    speed: float = Field(gt=0, le=10000)


def _validation_detail(e: ValidationError) -> list:
    # Never echo submitted values back (they may contain secrets or be large).
    return e.errors(include_input=False, include_url=False, include_context=False)


def _same_origin(origin: str | None, host: str | None) -> bool:
    """True when there is no Origin (non-browser client) or it matches the Host header."""
    if not origin:
        return True
    if origin == "null" or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


class Hub:
    """Fan-out of time-series points to WebSocket clients."""

    def __init__(self):
        self.clients: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, msg: dict) -> None:
        if self.loop is None:
            return
        data = json.dumps(msg, default=str)
        self.loop.call_soon_threadsafe(self._fanout, data)

    def _fanout(self, data: str) -> None:
        for q in list(self.clients):
            if q.qsize() > 50:  # slow client: drop oldest
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(data)


def create_app(destinations_path: str | None = None, host_override: str | None = None, runs_dir: str | None = None) -> FastAPI:
    hub = Hub()
    state: dict[str, Any] = {"run": None, "history": []}

    @asynccontextmanager
    async def lifespan(_app):
        hub.loop = asyncio.get_running_loop()
        yield
        run = state["run"]
        if run:
            run.stop()  # no-op once finishing/finished; exports may still be being written, so always wait
            # Long enough for workers to drain their SDK queues (or be terminated) and for exports to be written.
            if not await asyncio.to_thread(run.wait, run.drain_timeout + STOP_GRACE + 15):
                logging.getLogger(__name__).warning("run %s did not finish before shutdown", run.id)

    app = FastAPI(title="trafficsim", version=__version__, lifespan=lifespan)
    runs_root = Path(runs_dir) if runs_dir else RUNS_DIR
    start_lock = asyncio.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(
            request.headers.get("origin"), request.headers.get("host")
        ):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            # Revalidate (cheap 304 via ETag) so an upgraded dashboard never runs a stale app.js against new HTML.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def dest():
        return load_destinations(destinations_path, host_override)

    def current() -> Run:
        run = state["run"]
        if run is None:
            raise HTTPException(404, "no run")
        return run

    def active() -> Run:
        run = current()
        if run.state not in ACTIVE_STATES:
            raise HTTPException(409, f"run {run.id} is {run.state}")
        return run

    # ------------------------------------------------------------------ metadata
    @app.get("/api/meta")
    def meta():
        return {
            "verticals": VERTICAL_PLATFORMS,
            "regions": list(REGIONS),
            "event_types": {k: v.get("vertical") for k, v in PRESETS.items()},
            "event_presets": PRESETS,
            "default_platform_mix": DEFAULT_PLATFORM_MIX,
            "destination": dest().redacted(),
        }

    @app.get("/api/scenarios")
    def scenarios():
        return [{"id": s["id"], "name": s["name"], "description": s["description"]} for s in list_scenarios()]

    def load_by_id(sid: str) -> dict:
        try:
            return load_scenario_raw(sid, allow_path=False)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except FileNotFoundError:
            raise HTTPException(404, "scenario not found") from None
        except yaml.YAMLError as e:
            raise HTTPException(500, f"scenario file is invalid: {yaml_error_message(e)}") from None

    @app.get("/api/scenarios/{sid}")
    def scenario(sid: str):
        raw = load_by_id(sid)
        return {"id": sid, "raw": raw, "yaml": yaml.safe_dump(raw, sort_keys=False)}

    def build_scenario(req: StartRequest) -> Scenario:
        if req.yaml:
            try:
                raw = safe_load_strict(req.yaml)
            except yaml.YAMLError as e:
                raise HTTPException(400, f"invalid YAML: {yaml_error_message(e)}") from None
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                raise HTTPException(400, "scenario YAML must be a mapping")
        elif req.scenario:
            raw = load_by_id(req.scenario)
        else:
            raise HTTPException(400, "scenario or yaml required")
        if len(req.overrides) > MAX_OVERRIDES:
            raise HTTPException(400, f"at most {MAX_OVERRIDES} overrides")
        overrides = {k: v for k, v in req.overrides.items() if v not in (None, "")}
        try:
            sc = Scenario.model_validate(apply_overrides(raw, overrides))
        except ValidationError as e:
            raise HTTPException(422, _validation_detail(e)) from None
        for v, vc in sc.verticals.items():
            if vc.catalog:
                try:
                    resolve_catalog_path(v, vc.catalog, allow_path=False)
                except ValueError as e:
                    raise HTTPException(422, str(e)) from None
        return sc

    def check_scenario(sc: Scenario) -> TrafficPlanner:
        """Build everything a run needs (planner, catalogs, journeys) so errors surface before workers start."""
        import random

        from ..emitter import RecordingEmitter
        from ..journeys import Catalog
        from ..verticals import RUNTIMES

        try:
            pl = TrafficPlanner(sc)
            for v in pl.verticals:
                RUNTIMES[v](Catalog.load(v, sc.verticals[v]), RecordingEmitter(), random.Random(0), sc.seed)
        except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as e:
            raise HTTPException(422, str(e)) from None
        return pl

    @app.post("/api/validate")
    def validate(req: StartRequest):
        sc = build_scenario(req)
        pl = check_scenario(sc)
        peak, at = pl.peak_estimate()
        return {"ok": True, "name": sc.name, "peak": round(peak), "peak_at": at, "cohorts": len(pl.cohorts)}

    # ------------------------------------------------------------------ runs
    @app.post("/api/runs")
    async def start(req: StartRequest):
        sc = build_scenario(req)
        await asyncio.to_thread(check_scenario, sc)
        async with start_lock:  # two concurrent POSTs must not both pass the "no active run" check
            run = state["run"]
            if run is not None and run.state != "finished":
                raise HTTPException(409, f"run {run.id} is {run.state}; stop it first")
            try:
                new = Run(sc, dest(), send=not req.dry_run, runs_dir=runs_root)
            except (ValueError, OSError) as e:
                raise HTTPException(422, str(e)) from None

            def relay(point: dict, rid: str = new.id) -> None:
                hub.publish({"type": "point" if "summary" not in point else "finished", "run_id": rid, **point})

            new.listeners.append(relay)
            await asyncio.to_thread(new.start)
            state["run"] = new
            state["history"].append(new.id)
        hub.publish({"type": "started", "run_id": new.id, "status": new.status()})
        return new.status()

    @app.get("/api/runs/current")
    def status():
        return current().status()

    @app.post("/api/runs/current/pause")
    def pause():
        run = active()
        run.pause()
        return run.status()

    @app.post("/api/runs/current/resume")
    def resume():
        run = active()
        run.resume()
        return run.status()

    @app.post("/api/runs/current/speed")
    def speed(req: SpeedRequest):
        run = active()
        run.set_speed(req.speed)
        return run.status()

    @app.post("/api/runs/current/events")
    def trigger(spec: dict[str, Any]):
        run = active()
        if spec.get("type") not in PRESETS:
            raise HTTPException(422, "unknown event type")
        if len(run.live_overlays) >= MAX_LIVE_OVERLAYS:
            raise HTTPException(409, f"at most {MAX_LIVE_OVERLAYS} live events per run")
        try:
            return run.trigger(spec)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/runs/current/stop")
    def stop():
        run = current()
        run.stop()
        return run.status()

    @app.get("/api/runs/current/series")
    def series(last: int = Query(600, ge=1, le=7200)):
        return current().agg.series_snapshot(last)

    @app.get("/api/runs/current/summary")
    def summary():
        return JSONResponse(json.loads(json.dumps(current().summary(), default=str)))

    @app.get("/api/runs")
    def runs():
        out = []
        if runs_root.exists():
            for d in sorted(runs_root.iterdir(), reverse=True):
                if d.is_dir():
                    out.append({"id": d.name, "files": sorted(p.name for p in d.iterdir() if p.is_file())})
        return out[:50]

    @app.get("/api/runs/{rid}/files/{name}")
    def export_file(rid: str, name: str):
        if not ID_RE.match(rid):
            raise HTTPException(404, "not found")
        base = runs_root.resolve()
        path = (runs_root / rid / name).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, filename=f"{rid}-{name}")

    @app.get("/api/runs/current/export/{kind}")
    def export_live(kind: str):
        """Exports for the current run, built in memory (works while running; never touches disk)."""
        run = current()
        disposition = {"Content-Disposition": f'attachment; filename="{run.id}-{kind}"'}
        if kind == "summary.json":
            return Response(json.dumps(run.summary(), indent=2, default=str), media_type="application/json", headers=disposition)
        if kind == "timeseries.csv":
            return Response(timeseries_csv_text(run.agg.series_snapshot()), media_type="text/csv", headers=disposition)
        raise HTTPException(404, "unknown export")

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if not _same_origin(socket.headers.get("origin"), socket.headers.get("host")):
            await socket.close(code=1008)
            return
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        hub.clients.add(q)
        try:
            run = state["run"]
            hello = {"type": "hello", "status": run.status() if run else None, "series": run.agg.series_snapshot(300) if run else []}
            await socket.send_text(json.dumps(hello, default=str))
            while True:
                data = await q.get()
                await socket.send_text(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.clients.discard(q)

    # ------------------------------------------------------------------ dashboard
    @app.get("/")
    def index():
        return FileResponse(DASHBOARD / "index.html")

    @app.get("/healthz")
    def healthz():
        return PlainTextResponse("ok")

    app.mount("/static", StaticFiles(directory=DASHBOARD), name="static")
    app.state.hub = hub
    app.state.sim = state
    return app
