#!/usr/bin/env python3
"""Check that what PyPI serves for a version is what this repository released.

Three of the four correspondences a release has to satisfy are already gated inside the
workflows: the tag matches the packaged version, the tag points at a commit whose full CI
matrix passed, and the SBOM describes the shipped wheel rather than the build environment.
The fourth was prose in a runbook -- "pip install --upgrade && contextlake --version" -- run
by a human, asserted nowhere.

That gap is not theoretical. The publish step carries `skip-existing: true`, which exists so
a re-run is idempotent, and its cost is that an earlier upload under the same version number
is silently KEPT. Nothing downstream compares bytes, so a wheel that never came from this
commit could serve that version forever and every other gate would still be green.

Every check here reports one of three outcomes, never two. A check that could not RUN --
no network, the version not on the index yet -- is UNVERIFIABLE and exits non-zero, because
the one thing this script must never do is let "I could not look" read as "I looked and it
was fine". That is the defect class this project has spent a release series removing from its
own commands, and a verifier that could commit it would be worse than none.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

VERIFIED, BROKEN, UNVERIFIABLE = "verified", "broken", "unverifiable"

#: Bounded, because an unbounded wait cannot tell "not yet" from "never": PyPI's CDN takes a
#: few seconds to serve a fresh upload, and a release that is genuinely absent must fail
#: rather than hang a workflow until its own timeout kills it with no diagnosis.
_DOWNLOAD_ATTEMPTS = 6
_DOWNLOAD_PAUSE_S = 10


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, check: str, status: str, detail: str = "") -> None:
        self.rows.append((check, status, detail))

    @property
    def ok(self) -> bool:
        return all(status == VERIFIED for _check, status, _detail in self.rows)

    def report(self, *, as_json: bool) -> None:
        if as_json:
            print(json.dumps({"ok": self.ok, "checks": [
                {"check": c, "status": s, "detail": d} for c, s, d in self.rows]}, indent=2))
            return
        mark = {VERIFIED: "ok  ", BROKEN: "FAIL", UNVERIFIABLE: "????"}
        for check, status, detail in self.rows:
            print(f"  [{mark[status]}] {check}" + (f" -- {detail}" if detail else ""))
        if self.ok:
            print("All correspondences verified.")
        else:
            failed = [c for c, s, _d in self.rows if s == BROKEN]
            unknown = [c for c, s, _d in self.rows if s == UNVERIFIABLE]
            if failed:
                print(f"BROKEN: {', '.join(failed)}")
            if unknown:
                print(f"COULD NOT CHECK: {', '.join(unknown)}. "
                      f"Not the same as passing, which is why this exits non-zero.")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(version: str, into: Path, *, sdist: bool = False) -> tuple[Path | None, str]:
    """The file PyPI serves for ``version``, or ``(None, why)``.

    ``--no-cache-dir`` because pip will otherwise satisfy this from a local wheel it
    already has, and a file the index has since yanked or replaced would still "verify" --
    a check that never touches the thing it claims to be checking.
    """
    kind = ["--no-binary", ":all:"] if sdist else ["--only-binary", ":all:"]
    pattern = "contextlake-*.tar.gz" if sdist else "contextlake-*.whl"
    last = ""
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "download", "--no-deps", "--no-cache-dir", *kind,
             f"contextlake=={version}", "-d", str(into)],
            capture_output=True, text=True, check=False)
        wheels = sorted(into.glob(pattern))
        if proc.returncode == 0 and wheels:
            return wheels[0], ""
        last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
        last = last[0]
        if attempt < _DOWNLOAD_ATTEMPTS - 1:
            time.sleep(_DOWNLOAD_PAUSE_S)
    return None, last or "pip download produced no file"


def _upload_time(version: str, filename: str) -> tuple[str | None, str]:
    """When PyPI says ``filename`` was uploaded, or ``(None, why)``.

    The one fact that separates "somebody replaced our bytes" from "this workflow was
    re-run". `skip-existing` keeps the ORIGINAL upload, and a rebuild is not byte-identical
    (the wheel carries build timestamps), so a re-run of an already-published tag produces a
    different digest through an entirely innocent route. Without this, that reads as tamper
    evidence -- a false alarm on the one check whose whole value is being believed.
    """
    import urllib.error
    import urllib.request

    url = f"https://pypi.org/pypi/contextlake/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https URL
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return None, f"{type(e).__name__}: {e}"
    for entry in data.get("urls", []):
        if entry.get("filename") == filename:
            stamp = entry.get("upload_time_iso_8601") or entry.get("upload_time")
            return (stamp, "") if stamp else (None, "no upload time recorded")
    return None, f"{filename} is not among the files PyPI lists for {version}"


def _uploaded_before(stamp: str, run_started_at: str) -> bool | None:
    """Was the file uploaded before this run started? ``None`` if unparseable."""
    from datetime import datetime

    def _parse(s: str):
        s = s.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    a, b = _parse(stamp), _parse(run_started_at)
    if a is None or b is None:
        return None
    if a.tzinfo is None or b.tzinfo is None:   # compare like with like or not at all
        return None
    return a < b


def _version_from_install(wheel: Path, workdir: Path) -> tuple[str | None, str]:
    """``contextlake --version`` from a venv holding ONLY this wheel."""
    env_dir = workdir / "venv"
    venv.create(env_dir, with_pip=True, clear=True)
    py = env_dir / "bin" / "python"
    if not py.exists():  # Windows layout
        py = env_dir / "Scripts" / "python.exe"
    install = subprocess.run([str(py), "-m", "pip", "install", "--quiet", str(wheel)],
                             capture_output=True, text=True, check=False)
    if install.returncode != 0:
        return None, (install.stderr or install.stdout).strip()[-300:]
    run = subprocess.run([str(py), "-m", "contextlake", "--version"],
                         capture_output=True, text=True, check=False)
    if run.returncode != 0:
        return None, (run.stderr or run.stdout).strip()[-300:]
    return run.stdout.strip(), ""


def _git(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _check_digest(res: Result, label: str, path: Path, expected: str | None, version: str,
                  run_started_at: str | None) -> None:
    got = _sha256(path)
    if not (expected or "").strip():
        # UNVERIFIABLE and not BROKEN. An empty value is what a broken plumbing step
        # produces -- a workflow output that failed to write -- and reporting that as a
        # digest mismatch tells an operator they have a supply-chain incident when what
        # they have is a shell script that lost its exit status.
        res.add(label, UNVERIFIABLE,
                "no expected digest was supplied, so the bytes were compared to nothing")
        return
    if got.lower() == expected.strip().lower():
        res.add(label, VERIFIED, got)
        return
    if run_started_at:
        stamp, why = _upload_time(version, path.name)
        if stamp is None:
            res.add(label, UNVERIFIABLE,
                    f"digests differ and the upload time could not be read ({why}), so this "
                    f"cannot be told apart from a re-run of an already-published tag")
            return
        earlier = _uploaded_before(stamp, run_started_at)
        if earlier is None:
            res.add(label, UNVERIFIABLE,
                    f"digests differ and the timestamps ({stamp} vs {run_started_at}) could "
                    f"not be compared")
            return
        if earlier:
            res.add(label, UNVERIFIABLE,
                    f"digests differ, but {path.name} was uploaded at {stamp}, before this "
                    f"run began at {run_started_at} -- these are not the bytes this run "
                    f"produced, so the difference is a re-publish and not evidence of "
                    f"anything")
            return
    res.add(label, BROKEN,
            f"index serves {got}, this build produced {expected.strip().lower()}")


def _check_tag(res: Result, tag: str, version: str) -> None:
    """Runs whether or not a wheel was reachable: git needs no download."""
    label = f"tag {tag} packages version {version}"
    code, out = _git(["rev-parse", f"{tag}^{{commit}}"])
    if code != 0:
        res.add(label, UNVERIFIABLE, out[-200:])
        return
    sha = out
    code, packaged = _git(["show", f"{tag}:src/contextlake/__init__.py"])
    if code != 0:
        res.add(label, UNVERIFIABLE, "cannot read the tagged version file")
        return
    stated = ""
    for line in packaged.splitlines():
        if line.startswith("__version__"):
            stated = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if stated == version:
        res.add(label, VERIFIED, f"{tag} -> {sha[:12]}")
    else:
        res.add(label, BROKEN, f"{tag} packages {stated!r}, not {version}")


def verify(version: str, *, expect_sha256: str | None = None,
           expect_sdist_sha256: str | None = None, tag: str | None = None,
           run_started_at: str | None = None, skip_install: bool = False,
           check_sdist: bool = True) -> Result:
    res = Result()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        wheel, why = _download(version, work / "wheel")
        if wheel is None:
            res.add("PyPI serves this version", UNVERIFIABLE, why)
            # One row per check even here. A caller counting verified checks must not see a
            # short list and read it as a short release.
            res.add("published wheel matches the wheel CI built", UNVERIFIABLE,
                    "no wheel to hash")
            if not skip_install:
                res.add("installed wheel reports the tagged version", UNVERIFIABLE,
                        "no wheel to install")
        else:
            res.add("PyPI serves this version", VERIFIED, wheel.name)
            _check_digest(res, "published wheel matches the wheel CI built", wheel,
                          expect_sha256, version, run_started_at)
            if not skip_install:
                reported, why = _version_from_install(wheel, work)
                if reported is None:
                    res.add("installed wheel reports the tagged version", UNVERIFIABLE, why)
                elif reported.split()[-1] == version:
                    res.add("installed wheel reports the tagged version", VERIFIED, reported)
                else:
                    res.add("installed wheel reports the tagged version", BROKEN,
                            f"reported {reported!r}, expected {version}")

        if check_sdist:
            # Half of what PyPI serves. The first version of this script checked the wheel
            # and called the correspondence closed, which left the sdist -- the thing anyone
            # building from source gets -- outside the gate entirely.
            sdist, why = _download(version, work / "sdist", sdist=True)
            if sdist is None:
                res.add("published sdist matches the sdist CI built", UNVERIFIABLE, why)
            else:
                _check_digest(res, "published sdist matches the sdist CI built", sdist,
                              expect_sdist_sha256, version, run_started_at)

    if tag:
        _check_tag(res, tag, version)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify the published release corresponds to what this repo built.")
    ap.add_argument("--version", required=True, help="the released version, e.g. 7.24.0")
    ap.add_argument("--expect-sha256", default=None,
                    help="sha256 of the wheel the release workflow built")
    ap.add_argument("--expect-sdist-sha256", default=None,
                    help="sha256 of the sdist the release workflow built")
    ap.add_argument("--tag", default=None, help="the git tag, e.g. v7.24.0")
    ap.add_argument("--run-started-at", default=None,
                    help="ISO timestamp this release run began; a file uploaded before it "
                         "was published by an earlier run, so a digest difference is a "
                         "re-publish rather than a mismatch")
    ap.add_argument("--skip-install", action="store_true",
                    help="hash and compare only; do not build a venv")
    ap.add_argument("--no-sdist", action="store_true", help="check the wheel only")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.tag and not shutil.which("git"):
        print("git is not on PATH, so --tag cannot be checked", file=sys.stderr)
        return 1
    res = verify(args.version, expect_sha256=args.expect_sha256,
                 expect_sdist_sha256=args.expect_sdist_sha256, tag=args.tag,
                 run_started_at=args.run_started_at, skip_install=args.skip_install,
                 check_sdist=not args.no_sdist)
    res.report(as_json=args.json)
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
