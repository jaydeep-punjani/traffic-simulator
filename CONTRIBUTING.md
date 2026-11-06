# Contributing to trafficsim

Thanks for your interest in improving trafficsim. Bug reports, documentation fixes, new scenarios, catalog
improvements and code are all welcome.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). AI coding agents should also read
[AGENTS.md](AGENTS.md), which has the repository map, invariants and change recipes.

## Ways to contribute

- **Report a bug** with the [bug report form](https://github.com/jaydeep-punjani/traffic-simulator/issues/new/choose).
- **Suggest a feature** with the feature request form, or start a
  [discussion](https://github.com/jaydeep-punjani/traffic-simulator/discussions) if the idea needs shaping.
- **Share a scenario** that models real traffic you have seen (peak shape, platform mix, journey bias).
- **Improve spec fidelity**: the coverage tables in [docs/compatibility.md](docs/compatibility.md) list Segment spec
  events and properties that are not generated yet.
- **Test against another collector** and report the result so the compatibility matrix can be updated.

For anything larger than a small fix, please open an issue first so we can agree on the approach before you spend time
on it.

## Development setup

You need Python 3.10–3.13 (3.12 recommended) and, optionally, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
git clone https://github.com/jaydeep-punjani/traffic-simulator.git
cd traffic-simulator
uv venv --python 3.12 .venv                      # or: python3.12 -m venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate
pre-commit install                               # run lint/format checks on every commit
```

Try it end to end:

```bash
trafficsim mock-collector          # terminal 1: local Segment-compatible collector on :8088
trafficsim serve                   # terminal 2: dashboard on http://127.0.0.1:8000
```

## Checks

Pull requests must pass CI. Run the same checks locally first:

```bash
ruff check .                 # lint
ruff format --check .        # formatting (run `ruff format .` to fix)
mypy trafficsim              # types
pytest                       # tests (add --cov for coverage)
trafficsim validate          # all bundled scenarios load
pre-commit run --all-files   # everything above that pre-commit covers, plus YAML/workflow checks
```

[AGENTS.md](AGENTS.md#verify-every-change) lists extra checks for changes to the emitter, dashboard, Docker files,
workflows and packaging.

## Coding guidelines

- Follow the existing style: ruff (line length 140) and type hints on public functions. Keep Python 3.10 compatibility.
- Simulation code must be deterministic for a given seed: use the injected `rng`, never module-level `random`.
- Events must go through the Segment SDK and every event must be accounted for (`sent`, `failed`, `dropped`, `oversize`
  or `undelivered`). See the rules in [AGENTS.md](AGENTS.md#rules-that-must-not-be-broken).
- Streaming and e-commerce events follow the Segment Video Spec and E-commerce v2 Spec. Keep property names and types
  spec-compliant.
- The dashboard is plain HTML/CSS/JS with a strict CSP: no inline scripts or styles, no CDN, no build step.
- Add tests for behaviour changes. Tests must not depend on the current date or timezone.
- Never commit write keys, `.env`, `config/destinations.yaml` or `runs/`.

## Documentation

Update the docs in the same pull request as the change. [AGENTS.md](AGENTS.md#documentation-to-update) maps each kind of
change to the documents that cover it. Add a line to the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md) for
user-visible changes.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make focused commits with imperative subjects, for example `Add tournament overlay preset`.
3. Run the checks above.
4. Open a pull request and fill in the template: what changed, why, and how you verified it.
5. A maintainer reviews it. Please respond to feedback by pushing new commits; the branch is squash-merged.

`main` is protected. A pull request can merge only when the CI and CodeQL checks pass, the branch is up to date
with `main`, and all review conversations are resolved. If you are an outside contributor, a maintainer approves
your first workflow runs before they start.

## Releases

Maintainers cut releases by tagging `vX.Y.Z`; see [docs/releasing.md](docs/releasing.md).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
