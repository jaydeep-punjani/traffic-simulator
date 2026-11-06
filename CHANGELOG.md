# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The public interface covered by SemVer is the scenario and
catalog YAML schema, the CLI commands and flags, the REST and WebSocket API, and the export file formats. Breaking
changes to any of them require a new major version.

## [Unreleased]

## [1.0.0] - 2026-11-07

First public release.

### Added

- Simulation engine: coordinator plus sharded worker processes, a simulated clock with real-time and accelerated
  speeds, Poisson arrivals driven by Little's law, and journey state machines per vertical.
- Three verticals:
  - Video streaming (web, iOS, Android), following the Segment Video Spec.
  - E-commerce (web, iOS, Android), following the Segment E-commerce v2 Spec.
  - Gaming (PC, PS5), with a custom game event set.
- Platform-accurate Segment `context` for each platform: library, device, OS, app, network, screen, userAgent, locale
  and timezone. `context.library` names the current Segment SDKs (`analytics.js`, `analytics-swift`,
  `analytics-kotlin`, `Analytics-CSharp`).
- Video Spec ID properties: `content_asset_ids` / `content_pod_ids` on playback events, `content_pod_id` on
  heartbeats and `pod_id` on content events.
- Composable traffic patterns: constant, diurnal, weekly, ramp, step, spike, sine and custom.
- Scenario-event overlays: premiere, live event, flash sale, Black Friday, campaign push, game launch, patch day,
  tournament and weekend event.
- Events are sent through `segment-analytics-python` to any Segment-compatible `/v1/batch` endpoint, with per-source
  write keys and full delivery accounting (sent, failed, dropped, oversize, undelivered).
- Ten bundled scenarios, including `stress_100k` (100k concurrent sessions at 60x).
- CLI commands: `run`, `serve`, `validate`, `list`, `mock-collector` and `--version`.
- Web dashboard: scenario picker, parameter editing, live controls, charts, funnels, SDK health and run history.
- Exports: `summary.json`, `timeseries.csv`, `timeseries.jsonl` and optionally sampled payloads.
- Docker image (non-root, multi-stage) and a compose stack with a mock collector, the dashboard and headless runs.
- Documentation:
  - Architecture, scenario reference, operations, Docker, dashboard and API, compatibility, and releasing.
  - Contributor guide and agent guide (`AGENTS.md`).
- CI:
  - Lint, type checks, tests on Python 3.10–3.13 and macOS, wheel smoke test and Docker smoke test.
  - CodeQL, dependency review and Dependabot.
  - Tag-driven releases that publish wheels, a multi-arch GHCR image and build provenance.

[Unreleased]: https://github.com/jaydeep-punjani/traffic-simulator/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jaydeep-punjani/traffic-simulator/releases/tag/v1.0.0
