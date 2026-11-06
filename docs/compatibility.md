# Compatibility

What trafficsim sends, which public APIs and specs it follows, and how closely. Numbers on this page come from
running the code, not from reading it. The last section shows how to reproduce them.

- [At a glance](#at-a-glance)
- [Wire protocol: Segment HTTP Tracking API](#wire-protocol-segment-http-tracking-api)
- [SDK: segment-analytics-python](#sdk-segment-analytics-python)
- [Spec methods](#spec-methods)
- [Common fields and context by platform](#common-fields-and-context-by-platform)
- [Video Spec coverage](#video-spec-coverage)
- [E-commerce Spec v2 coverage](#e-commerce-spec-v2-coverage)
- [Gaming events](#gaming-events)
- [Collectors](#collectors)
- [Runtime support](#runtime-support)
- [Measured delivery](#measured-delivery)
- [Reproducing these numbers](#reproducing-these-numbers)

## At a glance

| Area | Reference | Status |
|---|---|---|
| Transport | [Segment HTTP Tracking API](https://www.twilio.com/docs/segment/connections/sources/catalog/libraries/server/http-api), `POST /v1/batch` | Full. All events go through the official SDK. |
| SDK | [segment-analytics-python](https://github.com/segmentio/analytics-python) `>=2.3,<3` (2.4.0 tested) | Full |
| Spec methods | [identify](https://www.twilio.com/docs/segment/connections/spec/identify), [track](https://www.twilio.com/docs/segment/connections/spec/track), [page](https://www.twilio.com/docs/segment/connections/spec/page), [screen](https://www.twilio.com/docs/segment/connections/spec/screen), [alias](https://www.twilio.com/docs/segment/connections/spec/alias) | 5 of 6 (no `group`) |
| Common fields | [Spec: Common Fields](https://www.twilio.com/docs/segment/connections/spec/common) | Required fields on every message; context per platform |
| Video | [Video Spec](https://www.twilio.com/docs/segment/connections/spec/video) | **14 / 17 events**, 88% of spec properties on emitted events |
| E-commerce | [E-commerce Spec v2](https://www.twilio.com/docs/segment/connections/spec/ecommerce/v2) | **12 / 28 events** (full purchase funnel), 77% of spec properties on emitted events |
| Gaming | No Segment spec exists | 15 custom events, Segment naming conventions |
| Collectors | Segment, RudderStack, Jitsu, Hightouch Events, bundled mock | See [Collectors](#collectors) |

## Wire protocol: Segment HTTP Tracking API

trafficsim never builds HTTP requests itself. Every event goes through `segment-analytics-python`, which posts batches
to `{host}/v1/batch`. A wire capture of a `mixed_all` run (800 sessions, 120x, 2 workers) recorded:

| Property | Observed | Segment documented limit |
|---|---|---|
| Method and path | `POST /v1/batch` on every request | `/v1/batch` |
| Auth | `Authorization: Basic base64(writeKey:)`, plus `writeKey` in the body | Basic auth or `writeKey` in body |
| `Content-Type` | `application/json` (gzip off by default; `sdk.gzip: true` turns it on) | JSON, optionally gzip |
| `User-Agent` | `analytics-python/2.4.0` | |
| Batch envelope | `batch`, `sentAt`, `writeKey` | |
| Events per batch | ≤ 100 (`sdk.upload_size`) | 2,500 |
| Largest batch | 144,917 bytes | 500 KB |
| Largest single event | 2,658 bytes | 32 KB |
| Duplicate `messageId`s | 0 of 84,527 | Used for deduplication |

Every message carries `type`, `messageId`, `timestamp` and `context`. `track`, `page` and `screen` carry `anonymousId`
and, once the user has signed in, `userId`. `identify` carries `userId` and `traits`. `alias` carries `previousId` and
`userId`. Nothing sets `integrations`.

**Timestamps.** `timestamp` is the *simulated* time of the event; `sentAt` is the real time the batch left. Segment
corrects client clock skew with `timestamp = receivedAt - (sentAt - originalTimestamp)`, so accelerated runs or
runs anchored in the past may be shifted on arrival. See [operations.md#timestamps](operations.md#timestamps) for
how to keep simulated time.

**Region.** Point `host` at `https://api.segment.io` (Oregon) or `https://events.eu1.segmentapis.com` (Dublin) to send
to Segment itself. Mind Segment's recommended ceiling of about 1,000 requests/s per workspace: at the default
`upload_size: 100`, that is about 100k events/s.

## SDK: segment-analytics-python

| Setting | SDK default | trafficsim default | Why |
|---|---|---|---|
| `upload_size` | 100 | 100 | Same |
| `upload_interval` | 0.5 s | 0.5 s | Same |
| `max_queue_size` | 10,000 | 50,000 | Absorbs bursts at 60x without dropping |
| `max_retries` | 10 | 5 | Fails faster so failures show up on the dashboard |
| `timeout` | 15 s | 15 s | Same |
| `gzip` | off | off | Same |
| `thread` | 1 | 1 per source per worker | Same per client; one client per source |

trafficsim subclasses the SDK's `Client` and `Consumer` (see [architecture.md#sdk-accounting](architecture.md#sdk-accounting)).
Batching, retries with backoff, auth, gzip and `sentAt` are unchanged SDK code. The subclass only adds delivery
counters (sent, failed, dropped, undelivered) and keeps the platform's `context.library` instead of
`analytics-python`. This depends on SDK internals, so the dependency is pinned below 3.0.

## Spec methods

| Method | Used by | When |
|---|---|---|
| `page` | Web | Every page view in a journey |
| `screen` | iOS, Android, PC, PS5 | Every screen view in a journey |
| `track` | All | Every action (catalog `events`) |
| `identify` | All | Sign-in and sign-up; carries traits (plan, country, signup date, …) |
| `alias` | All | Sign-up only, linking the anonymous ID to the new `userId` |
| `group` | – | Not generated (no B2B accounts in these verticals) |

## Common fields and context by platform

Each platform mimics the current Segment client SDK for that platform, including its exact `context.library.name`.
analytics-ios and analytics-android reached end of support in March 2026, so iOS and Android report their successors.

| Platform | `context.library` | Calls | SDK it mimics |
|---|---|---|---|
| Web | `analytics.js` `next-1.84.3` | page, track, identify, alias | [analytics-next](https://github.com/segmentio/analytics-next) |
| iOS / iPadOS | `analytics-swift` `1.10.4` | screen, track, identify, alias | [analytics-swift](https://github.com/segmentio/analytics-swift) |
| Android | `analytics-kotlin` `1.27.0` | screen, track, identify, alias | [analytics-kotlin](https://github.com/segmentio/analytics-kotlin) |
| PC (Windows) | `Analytics-CSharp` `3.0.0` | screen, track, identify, alias | [analytics-csharp](https://github.com/segmentio/analytics-csharp) (Unity / .NET game clients) |
| PS5 | `Analytics-CSharp` `3.0.0` | screen, track, identify, alias | analytics-csharp |

Context fields per platform. ✓ means always present; *app-specific* fields are custom extensions inside a spec object.

| Context field | Web | iOS / Android | PC | PS5 |
|---|---|---|---|---|
| `library` | ✓ | ✓ | ✓ | ✓ |
| `ip`, `locale`, `timezone` | ✓ | ✓ | ✓ | ✓ |
| `userAgent` | ✓ browser mix (Chrome, Safari, Firefox, Edge) | ✓ app UA | – | – |
| `page` (path, referrer, search, title, url) | ✓ | – | – | – |
| `screen` (width, height, density) | ✓ | ✓ | ✓ width, height | ✓ width, height |
| `app` (name, version, build) | – | ✓ plus `namespace` | ✓ plus `launcher` (Steam, Epic) | ✓ plus `title_id` |
| `device` (id, manufacturer, model, name, type) | – | ✓ plus `advertisingId`, `adTrackingEnabled` | ✓ (no `name`) plus `gpu`, `cpu_cores`, `ram_gb` | ✓ (`type: console`) |
| `os` (name, version) | – | ✓ | ✓ Windows 10 / 11, plus `label` | ✓ `PlayStation OS` |
| `network` (wifi, cellular, carrier, bluetooth) | – | ✓ | ✓ wifi | ✓ wifi |

Not generated: `campaign`, `referrer`, `groupId`, `traits` in context, `userAgentData`, `channel`, `active`, `device.token`.

## Video Spec coverage

Source: [Video Spec](https://www.twilio.com/docs/segment/connections/spec/video). Used by the streaming vertical on
web, iOS and Android. "Props" counts the spec's documented properties present on emitted events.

| Event | Emitted | Props | Not sent |
|---|---|---|---|
| Video Playback Started | ✓ | 13 / 16 | `ad_asset_id`, `ad_pod_id`, `ad_type` |
| Video Playback Paused | ✓ | 13 / 16 | `ad_asset_id`, `ad_pod_id`, `ad_type` |
| Video Playback Interrupted | – | | |
| Video Playback Buffer Started | ✓ | 13 / 13 | |
| Video Playback Buffer Completed | ✓ | 13 / 13 | |
| Video Playback Seek Started | ✓ | 14 / 14 | |
| Video Playback Seek Completed | ✓ | 13 / 13 | |
| Video Playback Resumed | ✓ | 13 / 13 | |
| Video Playback Completed | ✓ | 13 / 13 | |
| Video Playback Exited | ✓ | 13 / 13 | |
| Video Quality Updated | – | | |
| Video Content Started | ✓ | 16 / 19 | `bitrate`, `description`, `framerate` |
| Video Content Playing | ✓ | 16 / 19 | `bitrate`, `description`, `framerate` |
| Video Content Completed | ✓ | 16 / 19 | `bitrate`, `description`, `framerate` |
| Video Ad Started | ✓ | 8 / 13 | `content`, `load_type`, `pod_length`, `pod_position`, `quartile` |
| Video Ad Playing | – | | |
| Video Ad Completed | ✓ | 8 / 13 | `content`, `load_type`, `pod_length`, `pod_position`, `quartile` |
| **Total** | **14 / 17** | **182 / 207 (88%)** | |

`Video Playback Started` sends the plural `content_asset_ids` and `content_pod_ids` arrays; later playback events send
singular `content_asset_id` and `content_pod_id`, as the spec requires. Content heartbeats (`Video Content Playing`)
fire every `heartbeat_seconds` of simulated time (default 60). The streaming vertical also tracks `Signed In`,
`Searched` and `Content Browsed`.

## E-commerce Spec v2 coverage

Source: [E-commerce Spec v2](https://www.twilio.com/docs/segment/connections/spec/ecommerce/v2). Used by the
ecom vertical on web, iOS and Android. The generated events cover the whole purchase funnel; post-purchase,
promotion, sharing and review events are not generated.

| Event | Emitted | Props | Not sent |
|---|---|---|---|
| Products Searched | ✓ | 1 / 1 | |
| Product List Viewed | ✓ | 11 / 14 | `products[].coupon`, `.image_url`, `.url` |
| Product List Filtered | – | | |
| Promotion Viewed / Clicked | – | | |
| Product Clicked | – | | |
| Product Viewed | ✓ | 12 / 14 | `coupon`, `position` |
| Product Added | ✓ | 9 / 13 | `coupon`, `image_url`, `position`, `url` |
| Product Removed | ✓ | 9 / 13 | `coupon`, `image_url`, `position`, `url` |
| Cart Viewed | ✓ | 9 / 13 | `products[].coupon`, `.image_url`, `.position`, `.url` |
| Checkout Started | ✓ | 17 / 21 | `products[].coupon`, `.image_url`, `.position`, `.url` |
| Checkout Step Viewed | – | | |
| Checkout Step Completed | ✓ | 3 / 4 | `payment_method` |
| Payment Info Entered | ✓ | 4 / 5 | `shipping_method` |
| Order Completed | ✓ | 18 / 23 | `subtotal`, `products[].coupon`, `.image_url`, `.position`, `.url` |
| Order Updated / Refunded / Cancelled | – | | |
| Coupon Entered / Denied / Removed | – | | |
| Coupon Applied | ✓ | 5 / 5 | |
| Product Added to Wishlist | ✓ | 10 / 14 | `coupon`, `image_url`, `position`, `url` |
| Product Removed from Wishlist, Wishlist Product Added to Cart | – | | |
| Product Shared, Cart Shared, Product Reviewed | – | | |
| **Total** | **12 / 28** | **108 / 140 (77%)** | |

Extra properties outside the spec (allowed by Segment): `currency` and `value` on Product Added and Cart Viewed, and
`payment_method` and `value` on Order Completed. The ecom vertical also tracks `Signed In`, `Signed Up` and
`Push Notification Tapped` (from `campaign_push` events).

To generate a missing event, add it to `catalogs/ecom.yaml` and a journey handler; see
[AGENTS.md](../AGENTS.md#rename-or-add-an-event-in-an-existing-vertical).

## Gaming events

Segment has no gaming spec. The gaming vertical (PC and PS5) uses a custom event set that follows Segment's
[naming conventions](https://www.twilio.com/docs/segment/protocols/tracking-plan/best-practices) (Object Action,
Title Case, snake_case properties).

| Event | Properties | Notes |
|---|---|---|
| Game Installed | 5 | `game_launch` overlay and new players |
| Game Launched | 6 | Every session |
| Tutorial Started / Completed | 1 / 2 | New players |
| Login Completed | 3 | |
| Lobby Joined | 3 | |
| Matchmaking Started | 5 | Surges during `tournament` |
| Match Found | 4 | |
| Match Started | 5 | |
| Match Completed | 9 | Result, score, duration, XP |
| Level Up | 3 | |
| Achievement Unlocked | 3 | |
| Store Viewed | 2 | |
| Item Purchased | 7 | Virtual currency |
| Game Exited | 3 | |

Names and properties live in `catalogs/gaming.yaml`; rename events there.

## Collectors

trafficsim works with any endpoint that accepts Segment's `/v1/batch`. Set `host` (or `SEGMENT_HOST`) to the base URL;
the SDK appends `/v1/batch`.

| Collector | Endpoint | Auth | Status | Reference |
|---|---|---|---|---|
| Bundled mock collector | `http://127.0.0.1:8088` | Any write key | **Tested in CI** on every push | `trafficsim mock-collector` |
| Segment | `https://api.segment.io` or `https://events.eu1.segmentapis.com` | Basic (write key) | Compatible by construction (official SDK) | [HTTP API](https://www.twilio.com/docs/segment/connections/sources/catalog/libraries/server/http-api) |
| RudderStack | `https://<data-plane-url>` | Basic (write key) | Compatible; `/v1/batch` in rudder-server's gateway | [rudder-server gateway](https://github.com/rudderlabs/rudder-server/tree/master/gateway) |
| Jitsu | `https://<jitsu-ingest-host>` | `writeKey` in body or Basic | Compatible; `/v1/batch` routed in Jitsu's ingest | [jitsu ingest router](https://github.com/jitsucom/jitsu/blob/newjitsu/bulker/ingest/router.go) |
| Hightouch Events | `https://us-east-1.hightouch-events.com` | Basic (write key) | Compatible per their HTTP API docs | [Hightouch HTTP API](https://docs.hightouch.com/events/sdks/http) |

Only the bundled mock is tested automatically. The others accept the same request shape according to their docs or
source code. If you run trafficsim against one of them, please report the result in a
[discussion](https://github.com/jaydeep-punjani/traffic-simulator/discussions) so this table can say "verified".

## Runtime support

| Runtime | Status |
|---|---|
| Python 3.10, 3.11, 3.12, 3.13 on Linux | Tested in CI |
| Python 3.12 on macOS | Tested in CI |
| Python 3.14+ | Not in CI yet; the test suite passes locally on 3.15 |
| Windows | Not tested. Workers use `multiprocessing`; use Docker or WSL. |
| Docker `linux/amd64`, `linux/arm64` | Published image, built and smoke-tested on release |
| Browsers (dashboard) | Current Chrome, Firefox, Safari, Edge |

## Measured delivery

`stress_100k` with 10 workers on an Apple M2 Pro laptop against the bundled mock collector:

| Metric | Result |
|---|---|
| Peak concurrent sessions | 104.1k (target 100k) |
| Speed | 60x (2 simulated hours in about 130 s wall time) |
| Events sent by the SDK | 14,289,970 |
| Events received by the collector | 14,289,970 |
| Failed / dropped / undelivered | 0 / 0 / 0 |
| Average throughput | about 110k events/s |

## Reproducing these numbers

```bash
# Delivery at scale
trafficsim mock-collector &                       # :8088
trafficsim run stress_100k --workers 10 --max-wall 150
curl -s http://127.0.0.1:8088/stats               # compare "events" with the run's sdk.sent

# Spec coverage: payloads are recorded in-process, nothing is sent
pytest tests/test_engine.py -k "context or video_spec or catalogs"
```

To see the payloads, run any scenario with `--sample 1.0`. It writes the exact messages handed to the SDK to
`runs/<run_id>/payloads-w*.jsonl` (one file per worker).
