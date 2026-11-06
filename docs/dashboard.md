# Dashboard and HTTP API

`trafficsim serve` starts the dashboard and its API on `http://127.0.0.1:8000`.

```bash
trafficsim serve                        # 127.0.0.1:8000, sends to the host in config/destinations*.yaml
trafficsim serve --port 9000 --host https://collector.example.com
```

The dashboard has no authentication. Keep it on `127.0.0.1` or put it behind an authenticating proxy; see
[operations.md#security](operations.md#security).

## Using the dashboard

The page has three parts: run setup on the left, live results on the right, and past runs at the bottom.

### 1. Set up a run

| Control | What it does |
|---|---|
| **Scenario** | Pick a bundled scenario by name. Its description appears underneath. |
| **Simultaneous users** | Target concurrent sessions at peak (`traffic.base_concurrency`). |
| **Speed** | Real time up to 240x, or a custom value. 60x plays one simulated hour per real minute. |
| **Simulated length** | How much simulated time to cover, or *until I stop it*. |
| **Timestamps start** | Now, 1 hour to 7 days ago, or a custom time (`start_at`). |
| **Platform split** | Sliders per app for web / iOS / Android, or PC / PS5. |
| **Advanced options** | Seed, worker count, events/s cap, payload sampling, and the raw scenario YAML. |

A preview under the form explains the run in plain words: how many users, how long it takes in real time and where
events go. It warns when accelerated timestamps would run ahead of the real clock.
**Check settings** validates the scenario and adds the planned peak. **Practice run** simulates without sending
anything. **Start** sends events.

### 2. While it runs

- A progress bar with simulated time and real time left.
- **Pause** / **Resume**, and **Stop** (click twice to confirm). Stopping drains the SDK queues for up to 60 s.
- **Speed** can change at any time.
- **Add traffic event** starts a flash sale, premiere, tournament and so on right now, with an optional size and
  duration.

### 3. Reading the results

| Panel | Shows |
|---|---|
| Health banner | Whether every event is reaching the collector. Explains failures, drops, oversized messages and lag, with what to do. |
| Active users | Concurrent sessions per app and platform, with the target line. |
| Events per second | By Segment call type (track, page, screen, identify, alias). |
| Delivery | Sent, failed and dropped per second, and SDK queue size. |
| Funnels | Conversion through each vertical's funnel (play → complete, view → cart → order, matchmaking → match). |
| Top events | The most frequent event names. |
| Traffic events | Scenario events in progress, with their envelope. |
| Past runs | Every run in `runs/`, with download links for `summary.json`, `timeseries.csv` and sampled payloads. |

## HTTP API

Interactive OpenAPI docs are served at `/docs`, and the schema at `/openapi.json`.

Write requests (`POST`) must come from the same origin as the dashboard; cross-origin writes and WebSocket
connections get `403`. Scenarios are referenced by **name** (`ecom_flash_sale`), never by file path.

### Scenarios

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/meta` | | Verticals and platforms, regions, event presets, default platform mix, destination (write keys masked) |
| `GET /api/scenarios` | | `[{id, name, description}]` |
| `GET /api/scenarios/{id}` | | `{id, raw, yaml}` |
| `POST /api/validate` | Start request | `{ok, name, peak, peak_at, cohorts}`, or `422` with field errors |

### Runs

| Method and path | Body | Returns |
|---|---|---|
| `POST /api/runs` | Start request | Run status. `409` if a run is already active. |
| `GET /api/runs/current` | | Run status |
| `POST /api/runs/current/pause` | | Run status |
| `POST /api/runs/current/resume` | | Run status |
| `POST /api/runs/current/speed` | `{"speed": 60}` (0 < speed ≤ 10000) | Run status |
| `POST /api/runs/current/events` | `{"type": "flash_sale", "magnitude": 3, "hold": "30m"}` | The overlay that was added (at most 100 per run) |
| `POST /api/runs/current/stop` | | Run status |
| `GET /api/runs/current/series?last=600` | | The last N one-second points (1–7200) |
| `GET /api/runs/current/summary` | | Run summary |
| `GET /api/runs/current/export/summary.json` | | Summary as a download |
| `GET /api/runs/current/export/timeseries.csv` | | Time series as a download |
| `GET /api/runs` | | The 50 most recent runs and their files |
| `GET /api/runs/{id}/files/{name}` | | One export file |
| `GET /healthz` | | `ok` (plain text) |

**Start request:**

```json
{
  "scenario": "ecom_flash_sale",
  "overrides": {"traffic.base_concurrency": 5000, "speed": 60, "start_at": "-24h"},
  "dry_run": false
}
```

Use `"yaml": "<full scenario YAML>"` instead of `scenario` to run an edited scenario. `overrides` takes dotted keys,
the same as `--set` on the CLI (at most 200).

```bash
curl -s -X POST http://127.0.0.1:8000/api/runs -H 'content-type: application/json' \
  -d '{"scenario": "baseline_gaming", "overrides": {"traffic.base_concurrency": 2000}}'
curl -s -X POST http://127.0.0.1:8000/api/runs/current/events -H 'content-type: application/json' \
  -d '{"type": "tournament", "magnitude": 2, "hold": "20m"}'
curl -s -X POST http://127.0.0.1:8000/api/runs/current/stop
```

`curl` sends no `Origin` header, so these requests pass the same-origin check.

### WebSocket `/ws`

Each message is one JSON object:

| `type` | When | Payload |
|---|---|---|
| `hello` | On connect | Current run `status` (or `null`) and the last 300 points as `series` |
| `started` | A run starts | `run_id`, `status` |
| `point` | Every second while running | `run_id` and one stats point (sessions, events by type and platform, SDK counters, funnels, lag) |
| `finished` | The run ends and exports are written | `run_id` and the summary |

`started`, `point` and `finished` carry `run_id`, so a client can ignore points from an older run.
