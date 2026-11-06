## What and why

<!-- What does this change, and why? Link the issue: "Fixes #123". -->

## How it was verified

<!-- Commands you ran and their results. AGENTS.md lists the extra checks for emitter, dashboard, Docker and workflow changes. -->

- [ ] `ruff check .` and `ruff format --check .`
- [ ] `mypy trafficsim`
- [ ] `pytest`
- [ ] `trafficsim validate`

## Checklist

- [ ] Tests added or updated for behaviour changes
- [ ] Docs updated (README, `docs/`, and `docs/compatibility.md` for event or context changes)
- [ ] `CHANGELOG.md` updated under `Unreleased` for user-visible changes
- [ ] No write keys, `.env`, `config/destinations.yaml` or `runs/` committed
