"""A clean-room run has to prove a clean machine can do the whole thing.

The runner installs from the index and drives the shipped command; these are the functions
that turn what it observed into a verdict, and a wrong answer would come from here. Testing
them needs no install, which is why they are separate.

The charter singles out four shapes because each has broken before and each fails quietly:
an editable install masking a version mismatch, a re-index that silently rebuilds an
unchanged tree, an `--offline` run that reaches the network anyway, and a repository with no
manifest at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "g6_checks",
    Path(__file__).resolve().parent.parent / "benchmarks" / "g6-cleanroom" / "checks.py")
checks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checks)


def test_the_installed_version_must_be_the_released_one():
    assert checks.installed_version("contextlake 7.30.0", "7.30.0")[0] == checks.VERIFIED


def test_a_version_mismatch_is_the_defect_this_gate_exists_for():
    """An editable install has masked exactly this twice: the tree and the published
    artefact were different builds, and every check that read the tree agreed with itself."""
    status, detail = checks.installed_version("contextlake 7.29.0", "7.30.0")
    assert status == checks.BROKEN
    assert "7.29.0" in detail and "7.30.0" in detail


def test_no_version_at_all_is_unverifiable():
    assert checks.installed_version(None, "7.30.0")[0] == checks.UNVERIFIABLE


def test_all_six_outputs_are_named_individually():
    produced = dict.fromkeys(checks.OUTPUTS, True)
    status, detail = checks.produced_outputs(produced)
    assert status == checks.VERIFIED
    for name in checks.OUTPUTS:
        assert name in detail


def test_a_partial_run_names_what_is_missing_rather_than_counting():
    """"5 of 6" would let a reader assume the missing one was minor."""
    produced = dict.fromkeys(checks.OUTPUTS, True)
    produced["fleet view"] = False
    status, detail = checks.produced_outputs(produced)
    assert status == checks.BROKEN
    assert "fleet view" in detail


def test_an_output_never_checked_is_not_an_output_that_failed():
    produced = {name: True for name in checks.OUTPUTS if name != "wiki"}
    status, detail = checks.produced_outputs(produced)
    assert status == checks.UNVERIFIABLE
    assert "wiki" in detail


def test_a_second_index_over_an_unchanged_tree_must_rebuild_nothing():
    assert checks.reindex_is_quiet(1, 0)[0] == checks.VERIFIED
    status, detail = checks.reindex_is_quiet(1, 1)
    assert status == checks.BROKEN
    assert "unchanged tree" in detail


def test_a_first_run_that_rebuilt_nothing_is_no_baseline():
    """The pair (0, 0) used to pass and say "second rebuilt nothing".

    The fixture indexes one repository, so a first run reporting zero rebuilds means the
    count was not read, not that there was nothing to do. A check that passes when its own
    measurement failed is worse than no check.
    """
    status, detail = checks.reindex_is_quiet(0, 0)
    assert status == checks.UNVERIFIABLE
    assert "baseline" in detail


def test_offline_must_refuse_a_network_bound_command():
    """The check asks whether the guard REFUSED, not whether a command happened to exit 0.

    A first version ran a command with no network path at all under poisoned proxies and
    read the zero exit as proof, which tested nothing.
    """
    assert checks.offline_run(True, 2, "mirror fetch")[0] == checks.VERIFIED
    status, detail = checks.offline_run(False, 0, "mirror fetch")
    assert status == checks.BROKEN
    assert "preference rather than a promise" in detail
    assert checks.offline_run(None, None, "mirror fetch")[0] == checks.UNVERIFIABLE


def test_a_tree_with_no_manifest_is_a_repository_not_an_error():
    """The evidence is ITS OWN symbol, not a repository count.

    A first version asked whether the store held two or more repositories, which the
    previously-indexed tree had already made true, so an empty row would have passed.
    """
    assert checks.repo_without_manifest(0, True)[0] == checks.VERIFIED
    status, detail = checks.repo_without_manifest(0, False)
    assert status == checks.BROKEN and "no symbol of its own" in detail
    assert checks.repo_without_manifest(1, True)[0] == checks.BROKEN


def test_the_summary_names_the_interpreter_each_result_came_from():
    """"It passed" without saying where is the claim this gate exists to refuse."""
    ok, line = checks.summarise([
        ("3.10", "outputs", checks.VERIFIED, ""),
        ("3.13", "outputs", checks.BROKEN, "fleet view missing"),
    ])
    assert ok is False
    assert "3.13:outputs" in line


def test_an_untested_check_counts_against_the_run():
    ok, line = checks.summarise([
        ("3.10", "offline", checks.VERIFIED, ""),
        ("3.13", "offline", checks.UNVERIFIABLE, "did not run"),
    ])
    assert ok is False
    assert "not tested" in line and "3.13:offline" in line


def test_all_verified_is_the_only_pass():
    rows = [("3.10", "a", checks.VERIFIED, ""), ("3.13", "a", checks.VERIFIED, "")]
    assert checks.summarise(rows)[0] is True


def test_a_run_where_nothing_executed_does_not_pass():
    """The purest form of the defect this file is about, and an earlier version of this test
    blessed it: `summarise([])` returned True, so a gate that ran nothing reported success."""
    ok, line = checks.summarise([])
    assert ok is False
    assert "proved nothing" in line
