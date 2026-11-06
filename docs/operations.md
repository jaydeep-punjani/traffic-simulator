# Operations

## Commands

| Command | Purpose |
|---|---|
| `trafficsim run <id\|path> [options]` | Headless run. Exit code 1 if the run recorded errors. |
| `trafficsim serve [--bind 127.0.0.1] [--port 8000]` | Dashboard and REST/WebSocket API. |
| `trafficsim validate [<id\|path>] [--set k=v]` | Validate one scenario, or every bundled one. |
| `trafficsim list` | Bundled scenarios. |
| `trafficsim mock-collector [--bind 127.0.0.1] [--port 8088]` | Local Segment-compatible endpoint. |

`run` options: `--speed`, `--duration`, `--start-at`, `--seed`, `--concurrency`, `--workers`, `--eps-cap`, `--sample`,
`--dry-run` (simulate without sending), `--max-wall <seconds>`, `--print-every <seconds>`, `--set k=v` (repeatable),
`--destinations`, `--host`, `--out`. `Ctrl-C` stops the run gracefully (SDK queues drain); a second `Ctrl-C` aborts.

## Environment variables

| Variable | Effect |
|---|---|
| `TRAFFICSIM_HOME` | Directory containing `scenarios/`, `catalogs/` and `config/`. Defaults to the source checkout, or the copy bundled in the installed wheel. |
| `TRAFFICSIM_RUNS_DIR` | Where runs are written. Defaults to `<home>/runs`, or `./runs` when running from an installed wheel. |
| `SEGMENT_HOST`, `SEGMENT_WRITE_KEY`, … | Referenced from `config/destinations*.yaml` as `${VAR:-default}`. The default applies when the variable is unset or empty. |

## Destinations

```yaml
# config/destinations.yaml (gitignored). Falls back to config/destinations.example.yaml.
host: ${SEGMENT_HOST:-http://localhost:8088}       # SDK posts to {host}/v1/batch; must be http(s)
default_write_key: ${SEGMENT_WRITE_KEY:-dev-write-key}
sources:                                           # lookup: <vertical>_<platform>, then <vertical>, then default
  ecom_ios: ${ECOM_IOS_WRITE_KEY:-}
  gaming: ${GAMING_WRITE_KEY:-}
```

`--host` overrides the configured host (also when the configured host's variable is empty). Write keys never appear in
API responses, logs or exports; `/api/meta` shows `***` in their place.

## Exports

Each run writes `runs/<run-id>/`:

| File | Contents |
|---|---|
| `summary.json` | Scenario, totals, peaks, SDK counters, funnels, top events, errors and warnings. |
| `timeseries.jsonl` | One stats point per wall second, written during the run. |
| `timeseries.csv` | The same points flattened for spreadsheets. |
| `payloads-w<N>.jsonl` | Sampled payloads exactly as sent, when `sample_payloads` > 0. |

## Timestamps

- `timestamp` on every event is the **simulated** time, anchored at `start_at`. `sentAt` is the wall-clock time the SDK
  sent the batch.
- A run with `speed` > 1 that starts at `now` produces timestamps ahead of real time. The lead at the end of the run is
  `duration − duration / speed`: a 2-hour scenario at 60x ends about 118 minutes in the future.
  - Collectors that skew-correct with `sentAt` (Segment, RudderStack) will move those events back towards real time,
    which distorts the simulated shape.
  - Some warehouses and destinations reject future timestamps.
  - `trafficsim run` and the dashboard print a warning when a run will lead the wall clock by more than a minute.
- **For historical data**, start far enough in the past that sim time never passes the wall clock, for example
  `start_at: -24h` with `duration: 24h` at any speed. `ecom_black_friday` uses `-4d`. Turn off skew correction in the
  collector if it has that option.
- **Real-time runs** (`speed: 1`, `start_at: now`) have `timestamp ≈ sentAt`.
- The mock collector reports the median `sentAt − timestamp` in `/stats` (`timestamp_skew_s_median`).
- Regions use fixed UTC offsets; daylight saving time is not modelled.

## Sizing and tuning

Measured on a 10-core laptop with the collector on the same machine: `stress_100k` held ~100k concurrent sessions at
60x and sent ~100k events/s, all received.

- Events/s scales with `concurrency × speed`. 100k sessions in real time is about 1.7k events/s; at 60x it is about
  100k events/s.
- One worker sustains roughly 10k events/s. The default worker count is `min(CPUs, ceil(peak_sessions / 1500))`.
- **Sim lag** (dashboard, `max_lag_sim_seconds`) means workers are behind the clock. Timestamps stay correct, but the
  run takes longer in wall time. Add workers or machines, lower `speed`, or set `events_per_sec_cap`.
- **Dropped** means an SDK queue overflowed. Raise `sdk.max_queue_size`, add `sdk.threads`, increase
  `sdk.upload_size`, or slow down. Workers pause generation at 80 % queue fill, so drops usually mean the collector
  is slower than the generator.
- **Failed** means the collector rejected or did not answer after `sdk.max_retries`. Check the collector and the host.
- **Oversize** means a message exceeded 32 KB, usually from a custom catalog with very large properties.
- **Undelivered** means events were still queued when the 60 s drain deadline passed at stop.

## Mock collector

`trafficsim mock-collector` accepts `POST /v1/batch` and the single-call endpoints, gzip and Basic-auth or body write
keys. It counts events without storing them.

| Endpoint | Response |
|---|---|
| `GET /stats` | `events`, `batches`, `bytes`, `bad_requests`, `eps_last_1s`, `eps_last_10s`, `eps_avg`, `peak_eps_1s`, `by_write_key`, `by_type`, `timestamp_skew_s_median`. |
| `GET /sample` | Recent sampled payloads (one per `--sample-every` batches). |
| `POST /reset` | Clear counters. |

It counts events per request by occurrences of `"messageId"` in the body, which is exact for SDK-generated JSON and
much faster than parsing. It is a test tool with no authentication: keep it on localhost.

## Security

The dashboard is a local operator tool and has **no authentication**. Anyone who can reach its port can start runs that
send traffic to the configured collector with the configured write keys.

- Both servers bind to `127.0.0.1` by default. Do not bind to `0.0.0.0` on a shared network. To expose the dashboard,
  put it behind a reverse proxy that adds authentication and TLS.
- State-changing requests (`POST`) and WebSocket connections are refused when a browser `Origin` header does not match
  the `Host` (HTTP 403 for both requests and the WebSocket handshake). This blocks other websites from driving a dashboard on localhost.
- The dashboard has a strict Content-Security-Policy (`script-src 'self'`, no inline scripts, `frame-ancestors 'none'`)
  plus `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy`. Chart.js is vendored
  (`trafficsim/dashboard/vendor/`, SHA-256 recorded there); nothing loads from a CDN.
- Over the API, scenarios and catalogs can only be named (`[A-Za-z0-9_-]`, up to 64 characters), never given as file
  paths. Run files are served only if the resolved path is inside the runs directory.
- Limits: scenario YAML 256 KB, 200 overrides, 100 live events per run, `base_concurrency` ≤ 1 000 000, `speed` ≤
  10 000, overlay `magnitude` ≤ 100, overlay phases ≤ 30 days, series queries ≤ 7200 points. YAML anchors and aliases
  are rejected.
- Validation errors do not echo submitted values.
- Only one run can be active; concurrent start requests get HTTP 409.
- The mock collector caps request bodies at 4 MB (413) and decompressed bodies at 16 MB (400).
- Keep write keys in environment variables or the gitignored `config/destinations.yaml`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `trafficsim run` warns that timestamps run ahead | See [Timestamps](#timestamps). Use `--start-at -24h` or `speed: 1`. |
| `failed` grows, collector shows nothing | Wrong `host`, or the collector rejects the write key. `curl <host>/v1/batch` and check the collector's logs. |
| Run ends with "worker N did not finish … terminated" | A worker could not drain within 75 s, usually because the collector stopped answering. Counts for that worker's queued events show as undelivered. |
| Dashboard shows 403 on Start | The page was opened through a different host name than the request uses (for example a proxy that rewrites `Host`). Open it at the address `trafficsim serve` prints. |
| Python 3.10/3.11: `--start-at: expected one argument` | Fixed in the CLI; on older builds write `--start-at=-24h`. |
