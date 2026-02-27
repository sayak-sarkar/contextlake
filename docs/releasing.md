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

2. **Bump the version** in both places (they must match):
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/contextlake/__init__.py` → `__version__ = "X.Y.Z"`

3. **Update `CHANGELOG.md`:** move the items under `## [Unreleased]` into a new
   `## [X.Y.Z] - YYYY-MM-DD` section.

4. **Commit + tag** (annotated) and push:

   ```bash
   git add pyproject.toml src/contextlake/__init__.py CHANGELOG.md
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

## Tokenless publishing (recommended long-term)

PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) lets a
GitHub Actions workflow publish via short-lived OIDC tokens — no stored secret at
all. Once configured, pushing a `vX.Y.Z` tag builds and uploads automatically.
This is the preferred path for repeat releases.

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
