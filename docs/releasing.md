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

6. Watch the `release` workflow. It builds and smoke-tests the wheel, pushes and smoke-tests the image, then creates the
   GitHub Release with the changelog section as notes.

If a release fails after the image was pushed, fix the problem, delete the GitHub Release and tag if they exist
(`gh release delete vX.Y.Z --cleanup-tag`), and release the next patch version. Do not move a published tag.

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
