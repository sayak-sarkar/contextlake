# Releasing & publishing to PyPI

The maintainer runbook for cutting a versioned release and publishing it to
[PyPI](https://pypi.org/project/contextlake/). contextlake follows
[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

## One-time setup

Install the release tooling and make sure you can publish:

```bash
pip install -e ".[release]"        # build + twine
```

**A PyPI account + token (first time):**

1. Create an account at <https://pypi.org/account/register/> and verify your email.
2. Enable two-factor auth (PyPI **requires** it to upload).
3. Create an API token at <https://pypi.org/manage/account/token/>.
   - For the **very first** publish of this project, the token must be
     **account-scoped** ("Entire account") — project-scoped tokens only exist
     once the project is on PyPI.
   - After the first publish, create a new token **scoped to `contextlake`**,
     store that, and delete the account-wide one.

**Store the token outside the repository.** Never commit a token. Either let
`twine` prompt for it each time, or save it in `~/.pypirc` (not in the repo):

```ini
# ~/.pypirc   (chmod 600)
[pypi]
  username = __token__
  password = pypi-AgEI...your-token...
```

## Cutting a release

1. **Green build.** From a clean `main`:

   ```bash
   ruff check .
   pytest
   ```

2. **Bump the version** in one place: `src/contextlake/__init__.py` →
   `__version__ = "X.Y.Z"`. This is the single source of truth; `pyproject.toml`
   reads it dynamically (`[tool.setuptools.dynamic] version = { attr = ... }`),
   and the CLI `--version` and MCP serverInfo read the same string, so they can
   never drift apart.

3. **Update `CHANGELOG.md`:** move the items under `## [Unreleased]` into a new
   `## [X.Y.Z] - YYYY-MM-DD` section.

4. **Commit + tag** (annotated) and push:

   ```bash
   git add src/contextlake/__init__.py CHANGELOG.md
   git commit -m "chore(release): X.Y.Z"
   git tag -a vX.Y.Z -m "contextlake X.Y.Z"
   git push origin main
   git push origin vX.Y.Z
   ```

5. **Build and validate** the distribution:

   ```bash
   rm -rf dist build src/*.egg-info
   python -m build            # creates dist/contextlake-X.Y.Z.{tar.gz,whl}
   twine check dist/*         # must report PASSED for both artifacts
   ```

   Optional clean-room smoke test:

   ```bash
   python -m venv /tmp/cltest
   /tmp/cltest/bin/pip install dist/contextlake-X.Y.Z-py3-none-any.whl
   /tmp/cltest/bin/contextlake --version    # expect: contextlake X.Y.Z
   rm -rf /tmp/cltest
   ```

6. **Publish:**

   ```bash
   twine upload dist/*
   # username: __token__   password: <your PyPI token>   (skipped if ~/.pypirc is set)
   ```

7. **Cut a GitHub Release** from the tag (optional but recommended):

   ```bash
   gh release create vX.Y.Z --title "contextlake X.Y.Z" --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md)
   ```

8. **Verify it's live:**

   ```bash
   pip install --upgrade contextlake && contextlake --version
   ```

## Tokenless publishing via GitHub Actions (recommended)

[`.github/workflows/release.yml`](../.github/workflows/release.yml) publishes to
PyPI automatically using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — short-lived
OIDC tokens minted per run, **no API token stored anywhere**. With this set up, a
release is just steps 1–4 above (bump, changelog, commit, **push the `vX.Y.Z`
tag**); the workflow then verifies the tag matches the package version, runs lint
+ core tests, builds, and uploads.

**One-time PyPI configuration** (do this once, on PyPI):

1. Go to <https://pypi.org/manage/project/contextlake/settings/publishing/>.
2. **Add a new trusted publisher → GitHub** with:
   - **Owner:** `sayak-sarkar`
   - **Repository name:** `contextlake`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. Save. (These must match the workflow exactly, including the `pypi` environment.)

After the first successful tag-triggered publish, you can **delete the stored API
token** and remove `~/.pypirc` — the workflow no longer needs them. (Manual
`twine upload` remains available as a fallback.)

## Container image (ghcr.io)

The same tag push also builds and publishes a Docker image to the **GitHub
Container Registry** via the `docker` job in `release.yml` (using the built-in
`GITHUB_TOKEN` with `packages: write` — no extra secret). The image bundles the
`[kb]` + built-in model extras and **bakes in the pinned models** (see
[`Dockerfile`](../Dockerfile) and [`docker/prefetch_models.py`](../docker/prefetch_models.py)),
so `docker run` needs no model download at runtime — useful for zero-config or
air-gapped use:

```bash
docker run -v "$PWD/repositories:/work/repositories" \
  ghcr.io/sayak-sarkar/contextlake doctor
```

Tags published: the release version (e.g. `2.1.5`) and `latest`. PyPI remains the
**primary** distribution; GitHub Packages does not host PyPI-style Python packages,
so the image is the only relevant GitHub Packages artifact. Note the image is large
(it compiles `llama-cpp-python` and bundles a GGUF) and the build downloads the
models from HuggingFace — fine on GitHub's runners. To **build locally behind a
TLS-inspecting proxy**, pass your OS CA bundle so the in-build HF download trusts it,
e.g. `docker build --network=host --build-arg ... ` after baking
`REQUESTS_CA_BUNDLE` into the build (or build on a network without interception).

The image is **public by default for public repos**; check the package's visibility
under the repo's *Packages* once published.

## Troubleshooting

**`SSLError: CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`**
on upload or install. You're likely behind a TLS-inspecting proxy that re-signs
HTTPS with a corporate root CA. Your OS trust store has that root (so `curl`/`git`
work), but Python tools use their own bundled `certifi`. Point them at the system
bundle instead:

```bash
# path varies by distro, e.g.
#   Fedora/RHEL: /etc/pki/tls/certs/ca-bundle.crt
#   Debian/Ubuntu: /etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   # requests / twine
export PIP_CERT=/etc/ssl/certs/ca-certificates.crt             # pip
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt        # stdlib ssl
```

Do **not** disable verification (`--insecure` / `verify=False`) — reuse the real
root from your OS store instead.

**`File already exists`** on upload. PyPI is immutable: a version can never be
re-uploaded, even after deletion. Bump to a new `PATCH` version and release again.
