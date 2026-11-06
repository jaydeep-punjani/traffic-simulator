"""FastAPI app: scenarios, run control, live stats over WebSocket, exports, dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ..config import (DEFAULT_PLATFORM_MIX, REGIONS, VERTICAL_PLATFORMS, apply_overrides, list_scenarios, load_destinations,
                      load_scenario_raw, Scenario)
from ..events import PRESETS
from ..runner import RUNS_DIR, Run

log = logging.getLogger("trafficsim.api")
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"


class StartRequest(BaseModel):
    scenario: str | None = None
    yaml: str | None = None                    # full scenario YAML (edited in the dashboard)
    overrides: dict[str, Any] = {}             # dotted-key overrides
    dry_run: bool = False


class SpeedRequest(BaseModel):
    speed: float


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
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)


def create_app(destinations_path: str | None = None, host_override: str | None = None, runs_dir: str | None = None) -> FastAPI:
    hub = Hub()
    state: dict[str, Any] = {"run": None, "history": []}

    @asynccontextmanager
    async def lifespan(_app):
        hub.loop = asyncio.get_running_loop()
        yield
        run = state["run"]
        if run and run.state != "finished":
            run.stop()
            await asyncio.to_thread(run.wait, 30)

    app = FastAPI(title="trafficsim", version="0.1.0", lifespan=lifespan)
    runs_root = Path(runs_dir) if runs_dir else RUNS_DIR

    def dest():
        return load_destinations(destinations_path, host_override)

    def current() -> Run:
        run = state["run"]
        if run is None:
            raise HTTPException(404, "no run")
        return run

    # ------------------------------------------------------------------ metadata
    @app.get("/api/meta")
    def meta():
        return {"verticals": VERTICAL_PLATFORMS, "regions": list(REGIONS), "event_types": {k: v.get("vertical") for k, v in PRESETS.items()},
                "event_presets": PRESETS, "default_platform_mix": DEFAULT_PLATFORM_MIX,
                "destination": dest().redacted()}

    @app.get("/api/scenarios")
    def scenarios():
        return [{"id": s["id"], "name": s["name"], "description": s["description"]} for s in list_scenarios()]

    @app.get("/api/scenarios/{sid}")
    def scenario(sid: str):
        try:
            raw = load_scenario_raw(sid)
        except FileNotFoundError:
            raise HTTPException(404, f"scenario {sid} not found")
        return {"id": sid, "raw": raw, "yaml": yaml.safe_dump(raw, sort_keys=False)}

    def build_scenario(req: StartRequest) -> Scenario:
        if req.yaml:
            raw = yaml.safe_load(req.yaml) or {}
        elif req.scenario:
            raw = load_scenario_raw(req.scenario)
        else:
            raise HTTPException(400, "scenario or yaml required")
        overrides = {k: v for k, v in req.overrides.items() if v not in (None, "")}
        try:
            return Scenario.model_validate(apply_overrides(raw, overrides))
        except ValidationError as e:
            raise HTTPException(422, json.loads(e.json()))

    @app.post("/api/validate")
    def validate(req: StartRequest):
        from ..planner import TrafficPlanner
        sc = build_scenario(req)
        try:
            pl = TrafficPlanner(sc)
        except Exception as e:
            raise HTTPException(422, str(e))
        peak, at = pl.peak_estimate()
        return {"ok": True, "name": sc.name, "peak": round(peak), "peak_at": at, "cohorts": len(pl.cohorts)}

    # ------------------------------------------------------------------ runs
    @app.post("/api/runs")
    async def start(req: StartRequest):
        run = state["run"]
        if run is not None and run.state not in ("finished",):
            raise HTTPException(409, f"run {run.id} is {run.state}; stop it first")
        sc = build_scenario(req)
        try:
            new = Run(sc, dest(), send=not req.dry_run, runs_dir=runs_root)
        except Exception as e:
            raise HTTPException(422, str(e))
        new.listeners.append(lambda p, rid=new.id: hub.publish({"type": "point" if "summary" not in p else "finished", "run_id": rid, **p}))
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
        current().pause()
        return current().status()

    @app.post("/api/runs/current/resume")
    def resume():
        current().resume()
        return current().status()

    @app.post("/api/runs/current/speed")
    def speed(req: SpeedRequest):
        if req.speed <= 0:
            raise HTTPException(422, "speed must be > 0")
        current().set_speed(req.speed)
        return current().status()

    @app.post("/api/runs/current/events")
    def trigger(spec: dict):
        if spec.get("type") not in PRESETS:
            raise HTTPException(422, f"unknown event type {spec.get('type')!r}")
        try:
            return current().trigger(spec)
        except Exception as e:
            raise HTTPException(422, str(e))

    @app.post("/api/runs/current/stop")
    def stop():
        current().stop()
        return current().status()

    @app.get("/api/runs/current/series")
    def series(last: int = 600):
        pts = list(current().agg.series)
        return pts[-last:]

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
        base = runs_root.resolve()
        path = (runs_root / rid / name).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, filename=f"{rid}-{name}")

    @app.get("/api/runs/current/export/{kind}")
    def export_live(kind: str):
        """Exports for the current run, written on demand (works while running)."""
        from ..export import write_timeseries_csv
        run = current()
        run.out_dir.mkdir(parents=True, exist_ok=True)
        if kind == "summary.json":
            p = run.out_dir / "summary.json"
            p.write_text(json.dumps(run.summary(), indent=2, default=str))
        elif kind == "timeseries.csv":
            p = run.out_dir / "timeseries.csv"
            write_timeseries_csv(list(run.agg.series), p)
        else:
            raise HTTPException(404, "unknown export")
        return FileResponse(p, filename=f"{run.id}-{kind}")

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        hub.clients.add(q)
        try:
            run = state["run"]
            hello = {"type": "hello", "status": run.status() if run else None,
                     "series": list(run.agg.series)[-300:] if run else []}
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
