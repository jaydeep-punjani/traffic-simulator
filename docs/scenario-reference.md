# Scenario reference

A scenario is a YAML file in `scenarios/`. Its id is the file name without `.yaml`. `trafficsim validate <id|path>`
checks it and prints the planned peak.

Unknown keys are rejected everywhere except inside `events` entries, `params`, `bias` and `featured`. YAML anchors and
aliases (`&a` / `*a`) are rejected so a small file cannot expand into a huge document.

## Durations and times

| Form | Example | Meaning |
|---|---|---|
| Duration | `90s`, `15m`, `2h`, `1d12h`, `3600` | Plain numbers are seconds. |
| Offset | `+2h`, `-24h` | Relative to the scenario start (`at`, `start`) or to now (`start_at`). |
| Absolute | `2026-11-27T00:00:00Z` | ISO-8601. A naive value is read as UTC. |

## Top-level fields

| Field | Default | Limits | Description |
|---|---|---|---|
| `name` | required | | Display name. |
| `description` | `""` | | Shown in the dashboard. |
| `start_at` | `now` | `now`, offset or ISO | Sim time of the first event. See [operations.md#timestamps](operations.md#timestamps). |
| `duration` | `1h` | > 0, or `null` | Sim time to run. `null` runs until stopped. |
| `speed` | `1` | 0 < speed ≤ 10 000 | Sim seconds per wall second. |
| `seed` | `42` | | Seeds users, journeys and arrivals. Same seed and worker count reproduce the same simulation. |
| `workers` | `null` | 1–256 | Worker processes. Default: `min(CPUs, ceil(peak / 1500))`. |
| `warm_start` | `true` | | Start at target concurrency instead of ramping up from zero. |
| `events_per_sec_cap` | `null` | > 0 | Global throttle on events per wall second, split across workers. |
| `sample_payloads` | `0` | 0–1 | Fraction of payloads written to `runs/<id>/payloads-w*.jsonl`. |
| `sdk` | | | Segment SDK tuning, see below. |
| `traffic` | | | Concurrency, regions and patterns, see below. |
| `verticals` | required | | One or more of `streaming`, `ecom`, `gaming`. |
| `events` | `[]` | | Scenario-event overlays, see below. |

### `sdk`

| Field | Default | Limits | Description |
|---|---|---|---|
| `upload_size` | `100` | 1–2000 | Events per `/v1/batch` request (the SDK also caps a batch at 500 KB). |
| `upload_interval` | `0.5` | > 0 | Seconds to wait to fill a batch. |
| `max_queue_size` | `50000` | ≥ 100 | Per-source queue. Overflow is counted as `dropped`. |
| `threads` | `1` | 1–16 | Upload threads per source per worker. |
| `gzip` | `false` | | Gzip request bodies. |
| `timeout` | `15` | > 0 | HTTP timeout, seconds. |
| `max_retries` | `5` | 0–20 | SDK retries with backoff before counting a batch as `failed`. |

### `traffic`

| Field | Default | Limits | Description |
|---|---|---|---|
| `base_concurrency` | `1000` | 0–1 000 000 | Concurrent sessions before patterns and overlays. |
| `regions` | `{us: 0.45, eu: 0.30, apac: 0.25}` | `us`, `eu`, `apac`, `latam`, `india` | Region weights. A region sets the UTC offset for patterns plus country, locale and timezone in contexts. |
| `patterns` | `[]` | | Multiplicative patterns, see below. |
| `arrival_tau_seconds` | `120` | > 0 | Sim seconds over which arrivals close a gap between active and target sessions. |

Region UTC offsets are fixed (`us` −5, `eu` +1, `apac` +8, `latam` −3, `india` +5.5). Daylight saving time is not
modelled.

### `verticals.<name>`

| Field | Default | Description |
|---|---|---|
| `enabled` | `true` | |
| `weight` | `1` | Share of `base_concurrency` relative to the other verticals' weights. |
| `platforms` | streaming/ecom: web, ios, android; gaming: pc, ps5 | Platform weights. Only the platforms listed in the default are allowed. |
| `session.mean_minutes` | streaming 35, ecom 9, gaming 55 | Mean session length (lognormal). |
| `session.sigma` | `0.8` | Lognormal shape, 0–3. |
| `session.min_minutes`, `max_minutes` | `0.5`, `480` | Clamp. |
| `catalog` | bundled | Name of a catalog in `catalogs/` or, from the CLI only, a path to one. |
| `event_names` | `{}` | Rename events: `{logical_key: "New Name"}`. |
| `params` | `{}` | Override catalog `params`, see below. |
| `user_pool_factor` | `5` | Distinct users per cohort ≈ factor × peak concurrency. Larger means fewer returning users. |

Catalog `params` the verticals read:

| Vertical | Param | Default | Effect |
|---|---|---|---|
| streaming | `content_titles` | `400` | Size of the content library. |
| streaming | `heartbeat_seconds` | `60` | Interval of `Video Content Playing` heartbeats. |
| streaming | `mean_watch_minutes` | `32` | Mean watch time per play. |
| ecom | `products` | `2000` | Size of the product catalog. |
| ecom | `currency` | `USD` | Currency in E-commerce Spec properties. |
| gaming | `games` | built in | List of games: `id`, `name`, `genre`, `weight`, `modes` (`{mode: [min_minutes, max_minutes]}`), `maps`. |
| gaming | `new_player_rate` | `0.03` | Chance that a session outside a `game_launch` is a player's first (emits `Game Installed` and the tutorial). |

Each target concurrency is

```
base_concurrency × vertical weight share × platform share × region share × Π patterns × Π overlay multipliers
```

## Patterns

Every pattern returns a multiplier. A pattern may be scoped with `verticals`, `platforms` and `regions` lists; it then
only affects matching cohorts.

| `type` | Fields | Shape |
|---|---|---|
| `constant` | `value` (1) | Flat multiplier. |
| `diurnal` | `profile` (the vertical's own), `curve` (24 values), `floor` (0), `strength` (1) | Hourly curve in each region's local time, linearly interpolated. Profiles: `streaming`, `ecom`, `gaming`. `strength` < 1 flattens it. |
| `weekly` | `factors` (7 values, Mon–Sun; default `[0.90, 0.88, 0.90, 0.95, 1.05, 1.20, 1.15]`) | Day-of-week factor in local time. |
| `ramp` | `start` or `at` (`+0s`), `duration` (1h), `from` (0), `to` (1) | Linear from `from` to `to`, flat afterwards. |
| `step` | `initial` (1), `steps: [{at, value}]` | Piecewise constant. |
| `spike` | `at`, `rise` (10m), `hold` (30m), `decay` (30m), `magnitude` (2) | 1 → magnitude → 1 envelope. |
| `sine` | `period` (1h), `amplitude` (0.2), `phase` (0) | `1 + amplitude × sin(...)`, floored at 0. |
| `custom` | `points: [[offset, value], ...]`, `repeat` | Linear interpolation between points; `repeat` loops it. |

## Scenario events (`events`)

An event is an overlay with a rise/hold/decay envelope. While it is active it:

- multiplies concurrency for its scope by up to `magnitude`;
- multiplies journey transition weights by `bias[state]` (scaled by the envelope, so it fades in and out);
- passes `featured` values to the vertical, which uses them in event properties (a premiere title, sale SKUs, a
  campaign).

| Field | Description |
|---|---|
| `type` | A preset below, or `custom`. |
| `at` | Start (offset from scenario start, or ISO). Default `+0s`. |
| `name` | Label in the dashboard and exports. |
| `vertical` | Preset's vertical. Required for `custom`. |
| `platforms`, `regions` | Narrow the scope. |
| `rise`, `hold`, `decay` | Envelope phases, each 0–30 days. |
| `magnitude` | Peak multiplier, 0–100. |
| `bias` | `{journey_state: multiplier}`. Merged over the preset's bias. |
| `featured` | Vertical-specific content. Merged over the preset's. |

Presets (`rise` / `hold` / `decay`, `magnitude`):

| Type | Vertical | Envelope | Bias | Featured |
|---|---|---|---|---|
| `premiere` | streaming | 20m / 2h / 3h, 2.5 | play ↑, browse ↓ | `title`, `asset_id`, `program`, `genre`, `share`, `length_minutes` |
| `live_event` | streaming | 8m / 3h / 20m, 4 | play ↑↑, search/browse ↓ | as premiere, plus `livestream: true` |
| `flash_sale` | ecom | 10m / 1h / 30m, 3 | product, add_to_cart, checkout, payment, order ↑; exit ↓ | `sale_name`, `featured_skus`, `share`, `discount`, `coupon` |
| `black_friday` | ecom | 6h / 3d / 12h, 2.2 | add_to_cart, checkout, order, coupon ↑ | as flash_sale |
| `campaign_push` | ecom (iOS, Android) | 2m / 15m / 45m, 2 | product, add_to_cart ↑ | `campaign` (`name`, `source`, `medium`), `push_open_share`, … |
| `game_launch` | gaming | 2h / 2d / 1d, 5 | matchmaking ↑ | `game`, `game_id`, `genre`, `share`, `new_player_share`, `build` |
| `patch_day` | gaming | 15m / 1h / 2h, 2.5 | store ↑ | `patch`, `build_bump`, `build` |
| `tournament` | gaming | 30m / 4h / 30m, 2 | matchmaking ↑↑, store/exit ↓ | `mode`, `tournament` |
| `weekend_event` | gaming | 4h / 48h / 4h, 1.6 | matchmaking, store ↑ | `mode`, `xp_multiplier` |
| `custom` | any | 10m / 1h / 30m, 1.5 | none | none |

`share` is the fraction of sessions (at full intensity) that pick the featured item. When several overlays feature
content at once, the most recently started one wins.

From the dashboard or `POST /api/runs/current/events`, events can be triggered during a run; they start at the current
sim time.

## Catalogs

`catalogs/<vertical>.yaml` defines the app, events, funnels and journey for a vertical. Copy one to customise it and
point a scenario at it with `verticals.<v>.catalog: my_catalog` (file in `catalogs/`) or a path (CLI only).

```yaml
vertical: ecom
app: {name: ShopNow, version: "5.4.1", build: 5041, namespace_ios: ..., namespace_android: ..., web_url: ...}
params: {products: 2000, currency: USD}
events:                 # logical key -> event name sent to Segment
  product_viewed: Product Viewed
funnels:                # dashboard funnels, as lists of logical keys
  purchase: [product_viewed, product_added, checkout_started, order_completed]
journey:
  start: landing
  states:
    landing:
      view: {name: Home, category: Home}     # page on web, screen on apps and consoles
      dwell: [3, 20]                          # seconds, uniform
      next: {search: 2, category: 3, sign_in: 0.3, exit: 1}
    checkout:
      params: {requires: cart, fallback: category}
      track: [checkout_started]
      next: {shipping: 1}
```

State fields:

| Field | Description |
|---|---|
| `view` | `{name, category?}` for the `page`/`screen` call. |
| `track` | Logical event keys to send, in order. Properties come from the vertical's property builders. |
| `dwell` | `[min, max]` seconds before moving on. |
| `next` | `{state: weight}`. `exit` ends the session. Weights are relative. |
| `handler` | Built-in behaviour instead of `view`/`track`: `auth` (all), `watch` (streaming), `login` and `match` (gaming). |
| `params.requires` | Precondition: `identified` (all), `cart`, `checkout` (ecom), `new_player` (gaming). |
| `params.fallback` | State to go to when `requires` is not met (default `exit`). |

Validation rejects unknown states, handlers or event keys, states without transitions, and states that cannot be
reached from `start` through `next` or `fallback`.

Bundled journey states, which are also the keys `bias` can target:

| Vertical | Start | States | Funnels |
|---|---|---|---|
| streaming | `launch` | launch, sign_in, home, browse, search, play | playback, ads |
| ecom | `landing` | landing, search, results, category, product, wishlist, add_to_cart, cart, remove, sign_in, checkout, coupon, shipping, payment, identify_gate, order | purchase |
| gaming | `login` | login, tutorial, tutorial_done, lobby, store, purchase, matchmaking | match, store |

## Overrides

Any field can be overridden without editing the file:

```bash
trafficsim run mixed_all --set traffic.base_concurrency=20000 --set verticals.gaming.weight=0.5 --set 'traffic.regions={us: 1}'
```

Values are parsed as YAML. The dashboard sends the same dotted keys in `POST /api/runs` `overrides`.
