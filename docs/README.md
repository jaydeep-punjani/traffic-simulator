# Documentation

Start with the [README](../README.md) for a five-minute tour. Then pick by what you want to do:

| I want to… | Read |
|---|---|
| Run it for the first time | [README: Quick start](../README.md#quick-start) |
| Run it in containers | [docker.md](docker.md) |
| Use the dashboard or drive it over HTTP | [dashboard.md](dashboard.md) |
| Write or change a scenario | [scenario-reference.md](scenario-reference.md) |
| Rename events or change journeys | [scenario-reference.md#catalogs](scenario-reference.md#catalogs) |
| Send to RudderStack, Jitsu, Segment or my own collector | [compatibility.md#collectors](compatibility.md#collectors), [operations.md#destinations](operations.md#destinations) |
| Know exactly what is sent and how it maps to the Segment specs | [compatibility.md](compatibility.md) |
| Keep timestamps realistic | [operations.md#timestamps](operations.md#timestamps) |
| Push to 100k users or tune throughput | [operations.md#sizing-and-tuning](operations.md#sizing-and-tuning) |
| Fix a failing or slow run | [operations.md#troubleshooting](operations.md#troubleshooting) |
| Understand the internals | [architecture.md](architecture.md) |
| Contribute code | [CONTRIBUTING.md](../CONTRIBUTING.md), [AGENTS.md](../AGENTS.md) |
| Cut or verify a release | [releasing.md](releasing.md) |
| Report a vulnerability | [SECURITY.md](../SECURITY.md) |

## All documents

| Document | Contents |
|---|---|
| [architecture.md](architecture.md) | Processes, modules, arrival model, sim clock, SDK accounting, run lifecycle |
| [compatibility.md](compatibility.md) | Segment HTTP API and SDK usage, context per platform, Video and E-commerce spec coverage, collectors, runtimes, measured delivery |
| [dashboard.md](dashboard.md) | Dashboard walkthrough, REST API and WebSocket messages |
| [docker.md](docker.md) | Image, compose stack, real collectors, exports, sizing, hardening |
| [operations.md](operations.md) | Commands, environment, destinations, exports, timestamps, tuning, security, troubleshooting |
| [scenario-reference.md](scenario-reference.md) | Every scenario field, pattern, event preset and catalog option, with defaults and limits |
| [releasing.md](releasing.md) | Release process, verifying artefacts, repository settings and rulesets |
| [audit-2026-11.md](audit-2026-11.md) | November 2026 audit: findings, fixes and verification |

## External references

| Reference | Used for |
|---|---|
| [Segment Spec](https://www.twilio.com/docs/segment/connections/spec) | `identify`, `track`, `page`, `screen`, `alias`, common fields |
| [Segment Video Spec](https://www.twilio.com/docs/segment/connections/spec/video) | Streaming events |
| [Segment E-commerce Spec v2](https://www.twilio.com/docs/segment/connections/spec/ecommerce/v2) | E-commerce events |
| [Segment HTTP Tracking API](https://www.twilio.com/docs/segment/connections/sources/catalog/libraries/server/http-api) | `/v1/batch`, auth, size limits, `sentAt` |
| [segment-analytics-python](https://github.com/segmentio/analytics-python) | The SDK every event is sent through |
| [RudderStack event spec](https://www.rudderstack.com/docs/event-spec/standard-events/) | RudderStack as a destination |
| [Jitsu](https://jitsu.com/docs) | Jitsu as a destination |
