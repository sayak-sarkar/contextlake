"""The release verifier must never let "I could not look" read as "I looked and it was fine".

Three of the four correspondences a release has to satisfy were already gated inside the
workflows. The fourth -- what the index actually serves after publishing -- was a line of
prose in a runbook, run by a human, asserted nowhere. The publish step carries
`skip-existing: true` so a re-run is idempotent, and the cost of that is an earlier upload
under the same version being silently kept, which nothing downstream would notice.

This file tests the VERDICT logic, not the network: the outcomes are stubbed so each of the
three states can be produced deliberately.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_published_release",
    Path(__file__).resolve().parent.parent / "scripts" / "verify-published-release.py")
verifier = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verifier)


def _wheel(tmp_path, body: bytes = b"wheel bytes") -> Path:
    d = tmp_path / "download"
    d.mkdir(parents=True, exist_ok=True)
    w = d / "contextlake-7.24.0-py3-none-any.whl"
    w.write_bytes(body)
    return w


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Stub the download so a wheel with known bytes is what the index 'serves'."""
    body = b"wheel bytes"
    wheel = _wheel(tmp_path, body)
    monkeypatch.setattr(verifier, "_download",
                        lambda version, into, sdist=False: (wheel, ""))
    monkeypatch.setattr(verifier, "_version_from_install",
                        lambda w, work: ("contextlake 7.24.0", ""))
    return _digest(body)


def test_matching_bytes_verify(served):
    res = verifier.verify("7.24.0", expect_sha256=served, expect_sdist_sha256=served,
                          tag=None)
    assert res.ok
    assert all(s == verifier.VERIFIED for _c, s, _d in res.rows)


def test_different_bytes_are_broken_and_both_digests_are_named(served):
    res = verifier.verify("7.24.0", expect_sha256="deadbeef",
                          expect_sdist_sha256=served, tag=None)
    assert not res.ok
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    assert row[1] == verifier.BROKEN
    assert served in row[2] and "deadbeef" in row[2], (
        "a reader has to see both digests to know which side moved")


def test_a_version_the_index_does_not_serve_is_unverifiable_not_ok(monkeypatch):
    monkeypatch.setattr(verifier, "_download",
                        lambda version, into, sdist=False: (None, "No matching distribution"))
    res = verifier.verify("99.99.99", expect_sha256="abc", expect_sdist_sha256="abc",
                          tag="v99.99.99", check_sdist=False)
    assert not res.ok
    assert all(s == verifier.UNVERIFIABLE for _c, s, _d in res.rows)


def test_an_absent_expected_digest_is_unverifiable_not_verified(served):
    """The dangerous default: with nothing to compare against, the check did not happen.

    Reporting that as a pass would make a release run with a missing workflow output look
    exactly like one that was checked -- the same shape as every defect this release series
    has been removing from the product itself.
    """
    res = verifier.verify("7.24.0", expect_sha256=None, expect_sdist_sha256=None,
                          tag=None, check_sdist=False)
    assert not res.ok
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    assert row[1] == verifier.UNVERIFIABLE


def test_an_install_that_reports_another_version_is_broken(served, monkeypatch):
    monkeypatch.setattr(verifier, "_version_from_install",
                        lambda w, work: ("contextlake 7.23.0", ""))
    res = verifier.verify("7.24.0", expect_sha256=served, expect_sdist_sha256=served,
                          tag=None)
    assert not res.ok
    row = [r for r in res.rows if "reports the tagged version" in r[0]][0]
    assert row[1] == verifier.BROKEN


def test_an_install_that_could_not_run_is_unverifiable(served, monkeypatch):
    monkeypatch.setattr(verifier, "_version_from_install",
                        lambda w, work: (None, "pip failed"))
    res = verifier.verify("7.24.0", expect_sha256=served, expect_sdist_sha256=served,
                          tag=None)
    assert not res.ok
    row = [r for r in res.rows if "reports the tagged version" in r[0]][0]
    assert row[1] == verifier.UNVERIFIABLE


def test_the_unverifiable_case_still_lists_every_check(monkeypatch, tmp_path):
    """A short list would read as a short release rather than an unchecked one."""
    monkeypatch.setattr(verifier, "_download",
                        lambda version, into, sdist=False: (None, "nope"))
    monkeypatch.setattr(verifier, "_git", lambda args: (1, "no such tag"))
    res = verifier.verify("7.24.0", expect_sha256="abc", tag="v7.24.0")
    names = [c for c, _s, _d in res.rows]
    assert len(names) == 5, f"expected every check to be accounted for, got {names}"
    assert any("sdist" in n for n in names), "the sdist is half of what PyPI serves"
    assert any("tag" in n for n in names), (
        "the tag check reads git, not the index, so an unreachable index must not skip it")


def test_the_retry_budget_is_bounded():
    """An unbounded wait cannot tell "not yet" from "never"."""
    assert 1 <= verifier._DOWNLOAD_ATTEMPTS <= 20
    assert verifier._DOWNLOAD_ATTEMPTS * verifier._DOWNLOAD_PAUSE_S <= 300


# --- the second review round ------------------------------------------------------
#
# Every case below was named by a reviewer reading the first version of this file, which
# stubbed the network everywhere and left the tag path, the operator-facing report and the
# re-publish case with no coverage at all.


def test_a_rebuild_of_an_already_published_tag_is_not_a_tamper_alarm(served, monkeypatch):
    """The false alarm that would have destroyed trust in this check.

    `skip-existing` keeps the ORIGINAL upload, and a rebuild is not byte-identical because
    the wheel carries build timestamps. So re-running a published tag produces a different
    digest by an entirely innocent route, and calling that BROKEN would cry tamper on a
    routine re-run -- on the one check whose whole value is being believed.
    """
    monkeypatch.setattr(verifier, "_upload_time",
                        lambda version, filename: ("2026-08-18T09:00:00Z", ""))
    res = verifier.verify("7.24.0", expect_sha256="deadbeef", tag=None, check_sdist=False,
                          run_started_at="2026-08-18T10:00:00Z")
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    assert row[1] == verifier.UNVERIFIABLE
    assert "before this run began" in row[2]
    assert not res.ok, "unverifiable still exits non-zero; it is not a pass"


def test_a_digest_difference_in_bytes_this_run_uploaded_is_still_broken(served, monkeypatch):
    """The guard must excuse a re-publish, not every mismatch."""
    monkeypatch.setattr(verifier, "_upload_time",
                        lambda version, filename: ("2026-08-18T11:00:00Z", ""))
    res = verifier.verify("7.24.0", expect_sha256="deadbeef", tag=None, check_sdist=False,
                          run_started_at="2026-08-18T10:00:00Z")
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    assert row[1] == verifier.BROKEN


def test_an_unreadable_upload_time_is_unverifiable_not_broken(served, monkeypatch):
    monkeypatch.setattr(verifier, "_upload_time", lambda version, filename: (None, "offline"))
    res = verifier.verify("7.24.0", expect_sha256="deadbeef", tag=None, check_sdist=False,
                          run_started_at="2026-08-18T10:00:00Z")
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    assert row[1] == verifier.UNVERIFIABLE


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_expected_digest_is_unverifiable_not_broken(served, empty):
    """Reachable from a workflow step whose digest capture silently produced nothing.

    Reporting that as a mismatch tells an operator they have a supply-chain incident when
    what they have is a shell script that lost its exit status.
    """
    res = verifier.verify("7.24.0", expect_sha256=empty, tag=None, check_sdist=False)
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    assert row[1] == verifier.UNVERIFIABLE
    assert "compared to nothing" in row[2]


def test_the_tag_check_runs_even_when_the_index_is_unreachable(monkeypatch):
    """git needs no download, so an unreachable index must not silently skip it."""
    monkeypatch.setattr(verifier, "_download",
                        lambda version, into, sdist=False: (None, "offline"))
    calls = []

    def _fake_git(args):
        calls.append(args)
        if args[0] == "rev-parse":
            return 0, "abc123def456"
        return 0, '__version__ = "7.24.0"\n'

    monkeypatch.setattr(verifier, "_git", _fake_git)
    res = verifier.verify("7.24.0", expect_sha256="abc", tag="v7.24.0", check_sdist=False)
    assert calls, "the git check did not run at all"
    row = [r for r in res.rows if r[0].startswith("tag ")][0]
    assert row[1] == verifier.VERIFIED


def test_a_tag_that_packages_another_version_is_broken(monkeypatch):
    monkeypatch.setattr(verifier, "_download",
                        lambda version, into, sdist=False: (None, "offline"))
    monkeypatch.setattr(verifier, "_git", lambda args: (
        (0, "abc123") if args[0] == "rev-parse" else (0, '__version__ = "7.23.0"')))
    res = verifier.verify("7.24.0", expect_sha256="abc", tag="v7.24.0", check_sdist=False)
    row = [r for r in res.rows if r[0].startswith("tag ")][0]
    assert row[1] == verifier.BROKEN
    assert "7.23.0" in row[2]


def test_the_report_renders_every_state(capsys, served):
    """The operator-facing surface, which no test read before.

    `mark[status]` is an unguarded dict lookup, so an unrecognised state would raise inside
    the reporting rather than be reported.
    """
    res = verifier.Result()
    res.add("a", verifier.VERIFIED, "fine")
    res.add("b", verifier.BROKEN, "bad")
    res.add("c", verifier.UNVERIFIABLE, "unknown")
    res.report(as_json=False)
    printed = capsys.readouterr().out
    assert printed.strip(), "the capture must see something or this proves nothing"
    for token in ("ok", "FAIL", "????", "BROKEN: b", "COULD NOT CHECK: c"):
        assert token in printed, f"{token!r} missing from the operator's report"


def test_the_json_report_is_machine_readable(capsys):
    import json as _json

    res = verifier.Result()
    res.add("a", verifier.VERIFIED, "fine")
    res.add("b", verifier.UNVERIFIABLE, "unknown")
    res.report(as_json=True)
    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert [c["status"] for c in payload["checks"]] == [verifier.VERIFIED,
                                                        verifier.UNVERIFIABLE]


def test_main_exits_non_zero_when_a_check_is_unverifiable(monkeypatch, capsys):
    """`main` itself, including argument handling, which nothing exercised."""
    monkeypatch.setattr(verifier, "_download",
                        lambda version, into, sdist=False: (None, "offline"))
    code = verifier.main(["--version", "7.24.0", "--skip-install", "--no-sdist"])
    assert code == 1
    assert capsys.readouterr().out.strip()


def test_main_accepts_the_documented_flags(monkeypatch):
    """Every flag the runbook tells an operator to type must parse."""
    monkeypatch.setattr(verifier, "_download",
                        lambda version, into, sdist=False: (None, "offline"))
    monkeypatch.setattr(verifier, "_git", lambda args: (1, "no tag"))
    assert verifier.main([
        "--version", "7.24.0", "--tag", "v7.24.0", "--expect-sha256", "abc",
        "--expect-sdist-sha256", "def", "--run-started-at", "2026-08-18T10:00:00Z",
        "--skip-install", "--json"]) == 1


def _job_gate(job: str) -> str:
    """The `if:` of one `release.yml` job, as a Python expression over a `needs` result map."""
    text = (Path(__file__).resolve().parent.parent / ".github" / "workflows"
            / "release.yml").read_text(encoding="utf-8")
    block = text[text.index(f"\n  {job}:\n"):]
    expr = re.search(r"\n    if: >-\n(.*?)\n    runs-on:", block, re.S).group(1)
    expr = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", expr, re.S).group(1)
    expr = expr.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
    return " ".join(re.sub(r"needs\.([a-z-]+)\.result", r"r['\1']", expr).split())


def _release_runs(build: str, publish: str, verify: str) -> bool:
    results = {"build": build, "publish": publish, "verify-published": verify}
    return bool(eval(_job_gate("github-release"), {"r": results}))  # noqa: S307 (our own file)


@pytest.mark.parametrize("build,publish,verify,runs", [
    ("success", "success", "success", True),    # the normal release
    ("success", "success", "failure", False),   # PyPI took the upload and serves other bytes
    ("success", "failure", "failure", True),    # publish failed, so every check is unverifiable
    ("success", "failure", "success", True),
    ("failure", "skipped", "skipped", False),   # nothing was built
])
def test_the_github_release_survives_a_failed_publish_but_not_a_byte_mismatch(
        build, publish, verify, runs):
    """`verify-published` also runs, and exits 1, when `publish` failed: nothing is on the
    index, so every row is unverifiable. Gating on its result alone skipped the release in
    the case the job exists to cover (4.0.0)."""
    assert _release_runs(build, publish, verify) is runs


# --- a re-run of a published tag ----------------------------------------------------
#
# Timeline of "Re-run all jobs" on one tag: the first attempt uploads at 12:10 and PyPI keeps
# that file (`skip-existing`); the second attempt starts building at 14:00. The only
# difference between a re-publish and a swapped file is which side of the run start the
# upload falls on, so the run start has to be taken from THIS attempt.

_FIRST_UPLOAD = "2026-10-17T12:10:00.000000Z"
_ATTEMPT_2_ISO, _ATTEMPT_2_EPOCH = "2026-10-17T14:00:00Z", "1792245600"


@pytest.mark.parametrize("started", [_ATTEMPT_2_ISO, _ATTEMPT_2_EPOCH])
def test_an_upload_from_the_first_attempt_is_a_republish_in_either_spelling(
        served, monkeypatch, started):
    monkeypatch.setattr(verifier, "_upload_time", lambda version, filename: (_FIRST_UPLOAD, ""))
    res = verifier.verify("7.24.0", expect_sha256="deadbeef", tag=None, check_sdist=False,
                          run_started_at=started)
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    # Both "could not compare" and "re-publish" are UNVERIFIABLE, so the status alone would
    # pass for the wrong reason. The wording is what separates them.
    assert row[1] == verifier.UNVERIFIABLE
    assert "before this run began" in row[2], row[2]


@pytest.mark.parametrize("started", [_ATTEMPT_2_ISO, _ATTEMPT_2_EPOCH])
def test_an_upload_after_this_attempt_began_is_broken_in_either_spelling(
        served, monkeypatch, started):
    monkeypatch.setattr(verifier, "_upload_time",
                        lambda version, filename: ("2026-10-17T14:20:00.000000Z", ""))
    res = verifier.verify("7.24.0", expect_sha256="deadbeef", tag=None, check_sdist=False,
                          run_started_at=started)
    row = [r for r in res.rows if "matches the wheel CI built" in r[0]][0]
    assert row[1] == verifier.BROKEN, row


@pytest.mark.parametrize("junk", ["", "not a time", "99999999999999999999", "2026-10-17"])
def test_a_run_start_that_cannot_be_read_compares_nothing(junk):
    """None, not an exception and not a guess. `2026-10-17` has no zone, so it is refused."""
    assert verifier._uploaded_before(_FIRST_UPLOAD, junk) is None


def _release_job(job: str) -> str:
    text = (Path(__file__).resolve().parent.parent / ".github" / "workflows"
            / "release.yml").read_text(encoding="utf-8")
    start = text.index(f"\n  {job}:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def test_the_run_start_is_read_from_this_attempt_and_not_from_the_event():
    """An empty value would not fail anywhere: the script reads "" as "no time given" and
    reports a plain mismatch. So follow the value through every hop it takes."""
    verify = _release_job("verify-published")
    env = re.search(r"RUN_STARTED_AT: \$\{\{ (.+?) \}\}", verify).group(1)
    assert "github.event" not in env, (
        "the event payload is reused by a re-run, so an upload from the first attempt "
        "is later than it")
    job, output = re.fullmatch(r"needs\.([a-z-]+)\.outputs\.(\w+)", env).groups()
    needs = re.search(r"\n    needs: \[([^\]]*)\]", verify).group(1)
    assert job in [n.strip() for n in needs.split(",")], f"verify-published does not need {job}"

    producer = _release_job(job)
    step_id, key = re.search(
        rf"\n      {output}: \$\{{\{{ steps\.(\w+)\.outputs\.(\w+) \}}\}}\n", producer).groups()
    step = [s for s in producer.split("\n      - ") if f"id: {step_id}\n" in s]
    assert len(step) == 1, f"{job} has no single step with id {step_id}"
    assert re.search(rf'echo "{key}=\$\(date -u \+%Y-%m-%dT%H:%M:%SZ\)" >> "\$GITHUB_OUTPUT"',
                     step[0]), step[0]
