# Security policy

## Supported versions

Security fixes go into the latest release. Older releases are not patched.

| Version | Supported |
|---|---|
| Latest `0.x` release | Yes |
| Anything older | No |

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for security problems.**

Report vulnerabilities privately through GitHub:

1. Go to the repository's [Security tab](https://github.com/jaydeep-punjani/traffic-simulator/security).
2. Choose **Report a vulnerability** (GitHub private vulnerability reporting).
3. Describe the issue, the affected version or commit, and steps to reproduce. A proof of concept helps.

You should get an acknowledgement within 7 days and an assessment within 14 days. If the report is confirmed, a fix is
developed in a private fork, released, and published as a GitHub Security Advisory that credits you unless you prefer
otherwise.

## Scope

trafficsim is a load-generation tool meant to run on a developer machine, in CI, or inside a private network. Reports
that are in scope include, for example:

- Ways for a web page in the user's browser to control the dashboard API or WebSocket (cross-origin requests, DNS
  rebinding, CSP bypasses).
- Reading files, or loading scenarios from arbitrary paths, through the API.
- Write keys leaking through API responses, logs, exports or the container image.
- Vulnerabilities in the Docker image or the release pipeline (for example, artefacts that could be tampered with).

Out of scope:

- The dashboard has no authentication by design and binds to `127.0.0.1` by default. Exposing it on a public interface
  without an authenticating proxy is a deployment choice; see [docs/operations.md#security](docs/operations.md#security).
- Load generated against a collector you have configured. trafficsim sends exactly the traffic you ask for; do not point
  it at systems you are not authorised to test.
- Vulnerabilities in third-party dependencies with no demonstrated impact on trafficsim. Dependabot tracks those.

## Hardening already in place

The dashboard rejects cross-origin writes and WebSocket connections, serves a strict Content Security Policy with
vendored Chart.js, accepts scenario names but never paths, and bounds request sizes and numeric inputs. Write keys are
read from environment variables or a gitignored file and are masked in API output. The container runs as a non-root
user. Releases ship with build provenance attestations; see [docs/releasing.md](docs/releasing.md#verifying-a-release).
