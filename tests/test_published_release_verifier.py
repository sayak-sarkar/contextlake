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
