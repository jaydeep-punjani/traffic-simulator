# Running in Docker

The image runs every `trafficsim` command. `compose.yaml` starts the dashboard with the bundled mock collector and
adds an on-demand container for headless runs.

## Quick start

```bash
docker compose up -d --build        # dashboard: http://localhost:8000   mock collector: http://localhost:8088/stats
docker compose ps                   # both services should be "healthy"
docker compose logs -f dashboard
docker compose down                 # add -v to also delete the runs volume
```

Headless runs use the `sim` service (compose profile `cli`). Anything after `sim` is passed to `trafficsim`:

```bash
docker compose run --rm sim run stress_100k --max-wall 150
docker compose run --rm sim run mixed_all --start-at -24h --speed 120
docker compose run --rm sim validate
docker compose run --rm sim list
```

## Services

| Service | Command | Port (host) | Notes |
|---|---|---|---|
| `collector` | `mock-collector` | `127.0.0.1:8088` (`COLLECTOR_PORT`) | Counts events. `GET /stats`, `POST /reset`. |
| `dashboard` | `serve` | `127.0.0.1:8000` (`DASHBOARD_PORT`) | Waits for a healthy collector. Health check: `/healthz`. |
| `sim` | `run …` and other CLI commands | none | Profile `cli`; only starts with `docker compose run`. |

All three use the same image (`trafficsim:local`).

## Sending to a real collector

Set `SEGMENT_HOST` and write keys in your shell or in a `.env` file next to `compose.yaml`. `.env` is gitignored and
excluded from the image.

```bash
# .env
SEGMENT_HOST=https://rudderstack.example.com
SEGMENT_WRITE_KEY=...
ECOM_IOS_WRITE_KEY=...          # optional per-source keys, see docs/operations.md#destinations
```

```bash
docker compose up -d --no-deps dashboard                 # --no-deps: don't start the mock collector
docker compose run --rm --no-deps sim run baseline_ecom
```

The variables are the ones `config/destinations.example.yaml` reads: `SEGMENT_HOST`, `SEGMENT_WRITE_KEY` and
`<VERTICAL>_<PLATFORM>_WRITE_KEY`. A variable that is not set stays unset in the container, so the next fallback key
applies. Inside the compose network the mock collector is `http://collector:8088`, which is the default.

To use a full destinations file instead of variables, mount it read-only:

```yaml
# compose.override.yaml
services:
  dashboard:
    volumes:
      - ./config/destinations.yaml:/app/config/destinations.yaml:ro
```

## Scenarios, catalogs and exports

- `./scenarios` and `./catalogs` are mounted read-only over the copies in the image, so edits apply on the next run
  without rebuilding. Without compose, the image uses the copies baked in at build time (`TRAFFICSIM_HOME=/app`).
- Exports go to `/data/runs` (`TRAFFICSIM_RUNS_DIR`), stored in the named volume `runs`. To copy them out:

  ```bash
  docker compose cp dashboard:/data/runs ./runs-export
  ```

  The dashboard's export links download them too.

## Sizing

- The default worker count respects container CPU limits (cgroup `cpu.max` and the CPU affinity mask), not just the
  host CPU count. To cap a service, add `cpus: 4` to it in a `compose.override.yaml`, or set `workers` in the scenario.
- On Docker Desktop, every container shares the VM's CPUs, so the collector and the generator compete. Measured on an
  8-CPU Docker Desktop VM: `stress_100k` at 60x held 100k sessions (peak 102k) and sent 10.26M events in 139 s.
  The collector received all of them, with 0 failed and 0 dropped. Sim lag reached ~720 sim seconds (~12 wall seconds),
  more than on the bare host. Give the VM more CPUs or use a lower `speed` if lag matters.
- Workers are separate processes communicating over pipes, so the default 64 MB `/dev/shm` is enough.

## Hardening

The compose services:

- run as the non-root user `trafficsim` (uid 10001), set in the image;
- use a read-only root filesystem, with a tmpfs at `/tmp` and the `runs` volume as the only writable paths;
- drop all Linux capabilities and set `no-new-privileges`;
- run `init: true`, so worker processes are reaped and signals are forwarded;
- publish ports on `127.0.0.1` only. The dashboard has no authentication (see [operations.md#security](operations.md#security)).
  Inside the container the servers bind `0.0.0.0` so other containers and the published port can reach them. Don't
  change the port mapping to `0.0.0.0` on a shared network; put an authenticating reverse proxy in front instead.

The browser Origin check still works behind the port mapping, because the browser sends matching `Host` and `Origin`
headers (for example `localhost:8000`).

The image is built in two stages: dependencies are resolved into wheels in a build stage, and the runtime stage
installs them with `--no-index`. `.dockerignore` keeps `.env`, `config/destinations.yaml`, `.git`, `.venv`, `runs/` and
tests out of the build context.

## Shutdown

- `docker compose stop dashboard` stops an active run and drains SDK queues before exiting (`stop_grace_period: 100s`, above the 60 s drain timeout plus the 15 s worker grace and export time).
  The exit status is 143 because uvicorn re-raises SIGTERM after its graceful shutdown; exports are still complete.
- `sim` handles SIGTERM like Ctrl-C: it stops generating, drains (up to `drain_timeout` + 15 s), writes exports and
  exits (`stop_grace_period: 90s`). A second signal exits without draining.

## Without compose

```bash
docker build -t trafficsim .
docker run --rm --init -p 127.0.0.1:8000:8000 -e SEGMENT_HOST=https://collector.example.com \
  -e SEGMENT_WRITE_KEY trafficsim                        # dashboard
docker run --rm --init -v trafficsim-runs:/data/runs \
  trafficsim run baseline_streaming --dry-run --max-wall 30
```

Use `--init` so worker processes are reaped. Build with another Python using `--build-arg PYTHON_VERSION=3.13`.
