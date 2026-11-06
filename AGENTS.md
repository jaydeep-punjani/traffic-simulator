# AGENTS.md

Guidance for AI coding agents (and humans) working on **trafficsim**. Read this before changing code. It covers how the
repository is organised, the commands that prove a change works, the rules that must not be broken, and step-by-step
recipes for common changes. Human-oriented guidelines live in [CONTRIBUTING.md](CONTRIBUTING.md).

## What this project is

trafficsim simulates concurrent users of three kinds of product (video streaming, e-commerce, gaming) and sends their
events through the open-source Segment SDK (`segment-analytics-python`) to any Segment-compatible collector
(`POST {host}/v1/batch`). A coordinator process plans *how many* sessions each cohort has; worker processes decide
*what each session does* and emit `track` / `page` / `screen` / `identify` / `alias` calls with platform-accurate
`context`. A FastAPI app serves a REST API, a WebSocket stats stream and a no-build-step dashboard.

Read [docs/architecture.md](docs/architecture.md) for the process model and [docs/compatibility.md](docs/compatibility.md)
for what goes on the wire.

## Repository map

| Path | What lives there |
|---|---|
| `trafficsim/config.py` | Scenario/catalog/destination loading and pydantic validation. `VERTICAL_PLATFORMS`, `DEFAULT_PLATFORM_MIX`, `DEFAULT_SESSION_MINUTES`. |
| `trafficsim/timeutil.py`, `clock.py` | Duration/start-time parsing; `SimClock` (wall → sim time, pause, speed). |
| `trafficsim/patterns.py` | Traffic shapes. Registry: `PATTERNS`. |
| `trafficsim/events.py` | Scenario-event overlays. Registry: `PRESETS`. |
| `trafficsim/planner.py` | Target concurrency per cohort `(vertical, platform, region)` at sim time *t*. |
| `trafficsim/identity.py` | Seeded user pool, traits, anonymous → identified → alias. |
| `trafficsim/contexts.py` | Segment `context` per platform. `LIBRARIES`, browser/device/OS tables, `build_context`. |
| `trafficsim/journeys.py` | Journey state machine, `VerticalRuntime` base class, built-in `auth` handler. |
| `trafficsim/verticals/*.py` | One `VerticalRuntime` subclass per vertical; registry `RUNTIMES` in `verticals/__init__.py`. |
| `trafficsim/scheduler.py`, `worker.py` | Per-worker event heap and arrival control. |
| `trafficsim/emitter.py` | `SimClient`/`SimConsumer` over the Segment SDK, delivery accounting, payload sampling. |
| `trafficsim/stats.py`, `export.py` | Aggregated stats; `summary.json`, `timeseries.csv/jsonl`. |
| `trafficsim/runner.py` | Coordinator: start/stop/pause/speed/overlays, drain, exports. `STOP_GRACE`. |
| `trafficsim/api/app.py` | REST + WebSocket + static dashboard, security middleware. |
| `trafficsim/dashboard/` | `index.html`, `app.js`, `styles.css`, vendored Chart.js. No bundler. |
| `trafficsim/mock_collector.py` | Local Segment-compatible `/v1/batch` endpoint used by tests and demos. |
| `trafficsim/cli.py` | `trafficsim run | serve | validate | list | mock-collector`. |
| `catalogs/*.yaml` | Event names, properties params, journeys and funnels per vertical. |
| `scenarios/*.yaml` | Bundled scenarios. |
| `config/destinations.example.yaml` | Host and write-key template (env-var expansion). |
| `tests/` | pytest suite (unit, engine, API, end-to-end against the in-process mock collector). |
| `docs/` | Architecture, scenario reference, compatibility, operations, Docker, releasing. |

Scenarios, catalogs and the example config are bundled into the wheel under `trafficsim/_data` via
`[tool.setuptools.package-dir]` in `pyproject.toml`. If you add a new data directory, add it there too.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pre-commit install          # optional: run the same checks on every commit
```

Plain `python -m venv .venv && .venv/bin/pip install -e '.[dev]'` works too. Supported Python: 3.10–3.13.

## Verify every change

Run these before you say a change is done. CI runs the same checks (`.github/workflows/ci.yml`).

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy trafficsim
timeout 600 .venv/bin/python -m pytest -q
.venv/bin/trafficsim validate                     # every bundled scenario still loads
.venv/bin/pre-commit run --all-files              # whitespace, YAML/TOML, actionlint, ruff (stage new files first)
```

Extra checks by area:

| You changed | Also run |
|---|---|
| Emitter, worker, runner, scheduler | `trafficsim mock-collector` in one shell, then `trafficsim run mixed_all --concurrency 2000 --speed 60 --max-wall 30` and confirm `sdk.sent` equals the collector's received count and `failed/dropped/oversize/undelivered` are 0. |
| Dashboard (`trafficsim/dashboard/`) | `trafficsim serve`, load `http://127.0.0.1:8000`, start/pause/stop a run; check the browser console for CSP errors. |
| `Dockerfile`, `compose.yaml` | `docker build --check . && docker compose config --quiet && docker compose up -d --build --wait`, then `docker compose down -v`. |
| Workflows (`.github/workflows/`) | `.venv/bin/pre-commit run actionlint --all-files`. |
| Packaging (`pyproject.toml`) | `uv build`, then install the wheel in a fresh venv outside the repo and run `trafficsim validate mixed_all`. |

Never leave a long-running process (`serve`, `mock-collector`, compose stack) running after you finish.

## Rules that must not be broken

1. **All events go through the Segment SDK.** Emit only via `Emitter` → `SimClient`. Do not POST to collectors with
   `requests` directly and do not fork or vendor the SDK; subclass it as `emitter.py` does.
2. **Nothing is dropped silently.** Every generated event ends in exactly one of `sent`, `failed`, `dropped`,
   `oversize`, `undelivered`. New failure paths need a counter, surfacing in `sdk_snapshot()`, the dashboard health
   banner and a test.
3. **Event time is simulated time.** `timestamp` comes from the session's sim clock (`Emitter._ts`). Never use
   `datetime.now()` / `time.time()` for event fields.
4. **Determinism.** Use the injected, seeded `rng` (`self.rng`, `random.Random`) in workers and verticals. Never use the
   module-level `random` functions or global `numpy.random` state in simulation code.
5. **Platform fidelity.** `context.library` must stay the platform's library (`analytics.js`, `analytics-swift`,
   `analytics-kotlin`, `Analytics-CSharp`; exact strings, see `LIBRARIES` in `contexts.py`); the SDK would otherwise overwrite it with `analytics-python`. Web uses
   `page`, apps and consoles use `screen`.
6. **Catalog keys, not names.** Code refers to events by catalog key (`product_added`); the display name
   (`Product Added`) is user-editable YAML. Never hard-code display names in Python.
7. **Spec fidelity.** Streaming events follow the Segment Video Spec and e-commerce events follow the E-commerce v2 Spec.
   If you add or rename properties, keep them spec-compliant and update the coverage tables in
   [docs/compatibility.md](docs/compatibility.md).
8. **Secrets.** Never commit write keys, `.env`, `config/destinations.yaml` or `runs/`. Write keys come from environment
   variables and are masked in API output.
9. **API safety.** The API accepts scenario *names*, never file paths. Keep the same-origin checks, request size limits
   and numeric bounds in `api/app.py`.
10. **Dashboard constraints.** Strict CSP: no inline `<script>`/`style=` attributes, no CDN, no build step. Set dynamic
    styles via `element.style` in `app.js`. Every REST/WebSocket message carries `run_id`; ignore messages for other
    runs.
11. **Python 3.10 compatible.** Keep `from __future__ import annotations`; avoid 3.11+ only syntax and stdlib APIs
    (or guard them, as `available_cpus()` does with `getattr`).
12. **Shutdown budget.** Run stop waits up to `drain_timeout + STOP_GRACE`; the API lifespan and compose
    `stop_grace_period` (100 s) must stay larger than that. Change them together.

## Recipes

### Rename or add an event in an existing vertical

1. Add the key and display name under `events:` in `catalogs/<vertical>.yaml`.
2. Emit it from a journey state (`track: [my_key]`) or from the vertical's code with `self.track(s, "my_key", props)`.
3. If it needs properties, register a builder in the vertical's `setup()`: `self.props["my_key"] = self.p_my_key`.
4. If it belongs to a funnel, add it under `funnels:`.
5. Add or extend a test in `tests/test_engine.py`, and update `docs/compatibility.md` if the event is part of a spec.

### Add a journey state with custom logic

Handlers are generators that `yield` dwell seconds and `return` the next state name (or `None` to pick from `next`).
Register them in `setup()` (`self.handlers["checkout"] = self.h_checkout`) and reference them from the catalog
(`handler: checkout`). `Journey.validate` rejects unknown handlers and unknown event keys at load time.

### Add a scenario

Create `scenarios/<id>.yaml` (see [docs/scenario-reference.md](docs/scenario-reference.md)), then run
`trafficsim validate <id>` and a short dry run: `trafficsim run <id> --dry-run --max-wall 20`. Bundled scenarios are
validated by the test suite.

### Add a traffic pattern or scenario-event preset

- Pattern: subclass `Pattern` in `patterns.py` with a unique `kind` and add it to `PATTERNS`; document its fields in
  `docs/scenario-reference.md#patterns`; add a test in `tests/test_time_and_patterns.py`.
- Overlay preset: add an entry to `PRESETS` in `events.py` (envelope, multiplier, platforms, bias, featured); document
  it under `docs/scenario-reference.md#scenario-events-events`; add a planner test.

### Add a platform or a vertical

- Platform: extend `VERTICAL_PLATFORMS` and `DEFAULT_PLATFORM_MIX` in `config.py`, `LIBRARIES` and `build_context` in
  `contexts.py`, the dashboard platform sliders, the destinations example (`<vertical>_<platform>` write key) and the
  platform table in `docs/compatibility.md`.
- Vertical: add `catalogs/<name>.yaml`, a `VerticalRuntime` subclass in `trafficsim/verticals/<name>.py`, register it in
  `RUNTIMES`, add defaults in `config.py`, a baseline scenario, tests and docs.

## Tests

- Tests must not depend on today's date or the local timezone. Pin `start_at` (for example `2026-11-02T00:00:00Z`) when
  behaviour depends on the weekday or hour.
- End-to-end tests start the mock collector in-process on a free port; reuse the fixtures in `tests/conftest.py`.
- Keep the suite under a few minutes; prefer small concurrencies and high `speed`.

## Documentation to update

| Change | Update |
|---|---|
| User-visible behaviour, CLI flag | `README.md`, `docs/operations.md`, `CHANGELOG.md` (`Unreleased`). |
| Dashboard UI, REST endpoint, WebSocket message | `docs/dashboard.md`, `CHANGELOG.md` (`Unreleased`). |
| Scenario/catalog schema | `docs/scenario-reference.md`. |
| Events, properties, context, wire format | `docs/compatibility.md`. |
| Processes, lifecycle, accounting | `docs/architecture.md`. |
| Container behaviour | `docs/docker.md`. |

## Commits and pull requests

- Small, focused commits with imperative subjects ("Add tournament overlay preset").
- Fill in the pull request template; link the issue; list the verification commands you ran and their results.
- Do not bump the version or tag releases; maintainers do that (see [docs/releasing.md](docs/releasing.md)).
- `main` is protected by a ruleset: work on a branch and open a pull request. Required checks are named after the CI and
  CodeQL jobs, so renaming a job also means updating the ruleset
  ([docs/releasing.md#repository-settings](docs/releasing.md#repository-settings)).
- New actions must come from GitHub or a verified creator and be pinned to a full commit SHA.
