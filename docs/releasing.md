# Releasing

Releases are tag-driven. Pushing a `vX.Y.Z` tag runs [`.github/workflows/release.yml`](../.github/workflows/release.yml),
which publishes:

- an sdist and a wheel, attached to a [GitHub Release](https://github.com/jaydeep-punjani/traffic-simulator/releases),
- a multi-arch image `ghcr.io/jaydeep-punjani/traffic-simulator:{X.Y.Z, X.Y, latest}` for `linux/amd64` and
  `linux/arm64`, with an SBOM and provenance,
- build provenance attestations for the wheel, the sdist and the image.

There is no PyPI package.

## Steps

1. Make sure `main` is green.
2. Set the version in `trafficsim/__init__.py` (`__version__`). It is the single source of truth: `pyproject.toml`
   and the API read it, and the workflow refuses a tag that does not match. Follow [SemVer](https://semver.org/).
3. In `CHANGELOG.md`, rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a fresh empty `[Unreleased]` section, and
   update the compare links at the bottom. The release notes are taken from this section; the workflow fails if it
   is missing.
4. Open a pull request with those two changes and merge it.
5. Tag the merge commit and push the tag:

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. Watch the **Release** workflow. It builds and smoke-tests the wheel, pushes and smoke-tests the image, then creates the
   GitHub Release with the changelog section as notes. The image and release jobs run in the `release` environment,
   which only accepts `v*` tags.

Releases are immutable: once published, their assets and tag cannot be changed, and the version cannot be reused.
Publish a new patch version to fix a bad release. If the workflow fails before the GitHub Release is created, fix the
problem on `main`, delete the tag (`git push --delete origin vX.Y.Z`, which needs a repository admin because of the tag
ruleset) and release the next patch version. Never move a published tag.

## Verifying a release

Every artefact has a signed [build provenance attestation](https://docs.github.com/actions/security-for-github-actions/using-artifact-attestations)
that ties it to this repository and the workflow run that built it.

```bash
# Wheel or sdist from the Releases page
gh release download vX.Y.Z --repo jaydeep-punjani/traffic-simulator
gh attestation verify trafficsim-X.Y.Z-py3-none-any.whl --repo jaydeep-punjani/traffic-simulator

# Container image
gh attestation verify oci://ghcr.io/jaydeep-punjani/traffic-simulator:X.Y.Z --repo jaydeep-punjani/traffic-simulator

# SBOM attached to the image
docker buildx imagetools inspect ghcr.io/jaydeep-punjani/traffic-simulator:X.Y.Z --format '{{ json .SBOM }}'
```

## Installing a release

```bash
# Container
docker run --rm -p 127.0.0.1:8000:8000 ghcr.io/jaydeep-punjani/traffic-simulator:latest

# Wheel
pip install https://github.com/jaydeep-punjani/traffic-simulator/releases/download/vX.Y.Z/trafficsim-X.Y.Z-py3-none-any.whl
```

## Repository settings

The repository enforces the following settings. Keep them in place when changing workflows or job names.

**`main` branch ruleset**

- Changes land through pull requests, which are squash-merged. Review threads must be resolved, and new pushes dismiss
  earlier approvals.
- Required status checks must pass on a branch that is up to date with `main`: every job in the **CI** workflow
  (lint, the test matrix, package, Docker) and every **CodeQL** analysis. Renaming a job renames its check, so update
  the ruleset in the same pull request.
- CodeQL must not report new errors or high-severity security alerts.
- Linear history and signed commits are required; force pushes and deletion are blocked.
- Repository admins can bypass the ruleset.

**`v*` tag ruleset**: tags cannot be updated, force-pushed or deleted, except by repository admins.

**Actions**

- Workflows get a read-only `GITHUB_TOKEN` by default and cannot approve pull requests.
- Only GitHub-owned and verified-creator actions may run, and every action must be pinned to a full commit SHA.
- Workflows from outside contributors' pull requests need a maintainer's approval before they run.

**Security**

- Dependabot alerts, Dependabot security updates, secret scanning with push protection, and private vulnerability
  reporting are enabled.
- The **Dependency Review** workflow blocks pull requests that add dependencies with known vulnerabilities.

**Releases**: releases are immutable, and publishing runs in the `release` environment, which only `v*` tags can use.
