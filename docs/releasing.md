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
     **account-scoped** ("Entire account"), project-scoped tokens only exist
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
   ruff check src tests
   pytest
   ```

   `src tests`, not `.`, because that is the scope `ci.yml`, `release.yml` and
   `.pre-commit-config.yaml` all use. This step said `ruff check .` for a release cycle and
   failed every time it was run: the repo-root launcher trips `S606` by design, so anyone
   following the checklist literally hit a red gate that CI does not have. A checklist whose
   first step cannot pass gets skipped, which is worse than not having it.

2. **Bump the version** in one place: `src/contextlake/__init__.py` →
   `__version__ = "X.Y.Z"`. This is the single source of truth; `pyproject.toml`
   reads it dynamically (`[tool.setuptools.dynamic] version = { attr = ... }`),
   and the CLI `--version` and MCP serverInfo read the same string, so they can
   never drift apart.

3. **Update `CHANGELOG.md`:** move the items under `## [Unreleased]` into a new
   `## [X.Y.Z] - YYYY-MM-DD` section.

4. **Commit and push `main` first. Then wait for `ci.yml` to go green on that exact
   commit. Only then push the tag.**

   ```bash
   git add src/contextlake/__init__.py CHANGELOG.md
   git commit -m "chore(release): X.Y.Z"
   git push origin main

   # wait for the full Python matrix to finish on the commit you just pushed
   gh run list --workflow=ci.yml --commit "$(git rev-parse HEAD)"

   git tag -a vX.Y.Z -m "contextlake X.Y.Z"
   git push origin vX.Y.Z
   ```

   The order is not stylistic. Both `release.yml` and `binaries.yml` open with a
   gate that asks the API for a **completed** `ci.yml` run on the tagged commit and
   refuses to publish otherwise. Push the tag while CI is still running and that
   gate reads `missing`, both workflows fail immediately, and every real job is
   skipped. Nothing is broken and nothing is published: re-run the two failed runs
   once CI is green (`gh run rerun <id> --failed`) and they proceed normally. This
   is the gate doing its job, and it is easy to trip because the tag push is fast
   and the matrix is not.

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

7. **Cut a GitHub Release** from the tag, **only if you are publishing by hand.** A tag push already
   creates one: `release.yml`'s `github-release` job runs whenever the build succeeded, and
   `binaries.yml` uploads onto it, whichever finishes first creating it. Reach for this command only
   when both workflows are out of the picture:

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
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/), short-lived
OIDC tokens minted per run, **no API token stored anywhere**. With this set up, a
release is just steps 1–4 above (bump, changelog, commit, **push the `vX.Y.Z`
tag**); the workflow then verifies the tag matches the package version, runs lint
+ the full test suite (knowledge layer included), builds, and uploads.

**One-time PyPI configuration** (do this once, on PyPI):

1. Go to <https://pypi.org/manage/project/contextlake/settings/publishing/>.
2. **Add a new trusted publisher → GitHub** with:
   - **Owner:** `sayak-sarkar`
   - **Repository name:** `contextlake`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. Save. (These must match the workflow exactly, including the `pypi` environment.)

After the first successful tag-triggered publish, you can **delete the stored API
token** and remove `~/.pypirc`, the workflow no longer needs them. (Manual
`twine upload` remains available as a fallback.)

## Container image (ghcr.io)

The same tag push also builds and publishes **two** Docker image variants to the
**GitHub Container Registry** via the `docker` job in `release.yml` (using the
built-in `GITHUB_TOKEN` with `packages: write`, no extra secret), both built from
the same multi-stage [`Dockerfile`](../Dockerfile) (see also
[`docker/prefetch_models.py`](../docker/prefetch_models.py)), non-root, with no
compiler toolchain in the final image:

- **full** (`--target full`), the `[kb,kb-local,llm-local]` extras with the pinned
  models **baked in** (model2vec embedder + a small OpenVINO wiki LLM), so `docker run`
  needs no Ollama, no API key, and no model download at runtime. Useful for
  zero-config or air-gapped use, at the cost of a larger pull.
- **slim** (`--target slim`), the `[kb,kb-local,kb-vec]` extras only: no
  `openvino-genai`, no baked model, much smaller pull. Semantic search still works
  out of the box (model2vec is pure Python); point the wiki tier at
  Ollama/OpenAI/Anthropic/`cli` instead of the built-in LLM.

```bash
docker run -v "$PWD/repositories:/work/repositories" \
  ghcr.io/sayak-sarkar/contextlake doctor          # full
docker run -v "$PWD/repositories:/work/repositories" \
  ghcr.io/sayak-sarkar/contextlake:slim doctor     # slim
```

Tags published: the release version and `latest` for the full image (e.g. `7.3.0`,
`latest`), and the release version + `-slim`, plus rolling `slim`/`latest-slim`
aliases, for the slim image (e.g. `7.3.0-slim`, `slim`, `latest-slim`). PyPI remains
the **primary** distribution; GitHub Packages does not
host PyPI-style Python packages, so these images are the only relevant GitHub
Packages artifacts. The full image is still large (it bundles the OpenVINO runtime
and a ~349 MB model) and its build downloads the models from HuggingFace, fine on
GitHub's runners. To **build locally behind a TLS-inspecting proxy**, the in-build Hugging Face
download has to trust your OS CA bundle. The `Dockerfile` declares no build argument for this, so
add one to your local copy, above the `python docker/prefetch_models.py` line in the `build-full`
stage:

```dockerfile
ARG REQUESTS_CA_BUNDLE
ENV REQUESTS_CA_BUNDLE=${REQUESTS_CA_BUNDLE}
```

Then pass your bundle's path (Debian and Ubuntu below; on Fedora it is
`/etc/pki/tls/certs/ca-bundle.crt`):

```bash
docker build --network=host \
  --build-arg REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  --target full -t contextlake:full .
```

Building on a network without interception avoids the whole problem.

Both images are **public by default for public repos**; check the package's
visibility under the repo's *Packages* once published.

## Single-binary releases (PyApp)

The same tag push also triggers [`.github/workflows/binaries.yml`](../.github/workflows/binaries.yml),
a **separate** workflow from `release.yml` so a binary-build failure there can never
block the PyPI publish. It builds one launcher per platform (Linux x86_64, macOS
arm64, Windows x86_64) via [PyApp](https://ofek.dev/pyapp/), a small Rust binary
that embeds `contextlake`'s project metadata (`PYAPP_PROJECT_NAME`,
`PYAPP_PROJECT_VERSION` from the tag, `PYAPP_PROJECT_FEATURES=kb-full,llm-local`,
`PYAPP_EXEC_SPEC=contextlake.cli:main`) and bootstraps a private Python + the
package into its own cache the first time it runs, nothing needs to be
preinstalled, not even Python. Binaries are uploaded as assets on the same
GitHub Release `release.yml` creates (whichever workflow finishes first creates
the release; the other edits/uploads onto it).

`llm-local` rides along and needs no special pip flags: `openvino-genai` is an
ordinary manylinux wheel, so the first run installs it on a machine chosen for
having nothing installed. Earlier releases set `PYAPP_PIP_EXTRA_ARGS` to attach a
per-accelerator wheel index for `llama-cpp-python`; that variable is gone, and
re-adding it would point at a package contextlake no longer depends on.

To reproduce a build locally (needs a Rust toolchain, `rustup` on any platform):

```bash
PYAPP_PROJECT_NAME=contextlake PYAPP_PROJECT_VERSION=7.3.0 \
  PYAPP_PROJECT_FEATURES=kb-full,llm-local PYAPP_EXEC_SPEC="contextlake.cli:main" \
  cargo install pyapp --root pyapp-out
./pyapp-out/bin/pyapp doctor   # first run bootstraps; every run after is instant
```

## Supply-chain artefacts a tag push produces

Three things ship alongside the wheel, the images and the binaries. None of them needs a manual
step, but a maintainer should know they exist, because a failure in any of them shows up as a red
workflow on an otherwise-successful release:

| Artefact | Produced by | What it covers |
| --- | --- | --- |
| CycloneDX SBOM, `contextlake-<version>.cdx.json` | `release.yml`, the SBOM job | The dependency closure of `contextlake[kb-full]`. **Not** `kb-fastembed`, **not** `kb-pdf` and **not** `llm-local`, so it does not span the Docker images or the binaries, which both carry `llm-local`. `security.yml`'s `pip-audit-resolved` is what watches that set |
| SLSA build provenance on both images | `release.yml`, `provenance: true` on the image build | The image bytes pushed to ghcr.io. The images are also `cosign`-signed |
| Sigstore build-provenance attestation on the three launchers | `binaries.yml`, `actions/attest-build-provenance` | The **launcher** only. The Python payload is fetched from PyPI on the user's machine at first run, after any signature here, so the attestation says nothing about it. `docs/install.md` states that limit to users too, and the release notes must not imply otherwise |

## Troubleshooting

**A red `binaries.yml` on the attestation step, with a `502` from Sigstore.** Transient, and it has
recurred often enough to expect it. The launchers themselves built fine; only the signing call
failed. Re-run the failed jobs, the same remedy as a tripped CI gate:

```bash
gh run rerun <run-id> --failed
```

If it fails a second time in a row, check <https://status.sigstore.dev/> before looking at the
workflow.

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

Do **not** disable verification (`--insecure` / `verify=False`), reuse the real
root from your OS store instead.

**`File already exists`** on upload. PyPI is immutable: a version can never be
re-uploaded, even after deletion. Bump to a new `PATCH` version and release again.
