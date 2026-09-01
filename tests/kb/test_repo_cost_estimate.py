"""The per-repository memory budget, and the estimate it rests on.

Why this exists, from the root-cause investigation of 2026-08-31: a repository
took a 15.4 GB machine down, and NONE of the existing guards could have stopped
it. `max_file_bytes` is per-FILE and the largest file in that tree was 3.57 MB
against a 4.77 MB cap, so it never fired once while the aggregate reached
671 MB. The shard-item guard checks node and edge counts AFTER the shard is
built, which is after the memory is already spent.

A per-file cap cannot bound a repository that is wide rather than deep.
"""
from __future__ import annotations

import pytest

from contextlake.kb import parse


def _filter():
    allowed, names, hcl, sql = parse._source_filter(None)
    return dict(allowed_exts=allowed, allowed_names=names,
                index_hcl=hcl, index_sql=sql, max_file_bytes=5_000_000)


def _write(root, rel, size):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size, encoding="utf-8")
    return path


# ---- the estimate ---------------------------------------------------------

def test_the_estimate_weights_code_far_above_markup(tmp_path):
    """Code costs 19.6x its bytes and XML 3.5x, both measured. An estimate that
    weighted them equally would wave through the code-heavy repositories, which
    are the dangerous ones: the fleet's worst offender is 376 MB of almost pure
    code."""
    code = tmp_path / "code"
    _write(code, "a.py", 100_000)
    xml = tmp_path / "xml"
    _write(xml, "a.xml", 100_000)

    code_est, _ = parse.estimate_repo_cost(code, **_filter())
    xml_est, _ = parse.estimate_repo_cost(xml, **_filter())

    assert code_est > xml_est * 4, (code_est, xml_est)
    assert code_est == pytest.approx(100_000 * parse.KIND_COST[parse._CODE])


def test_the_estimate_counts_only_what_would_be_indexed(tmp_path):
    """The tree that broke the machine is 5.6 GB on disk and 926 MB of
    ingestable bytes: 1.4 GB of .dll, 1.1 GB of .pdb and 305 MB of .png are
    never parsed. Estimating from the tree would refuse repositories that are
    mostly binaries and index fine."""
    _write(tmp_path, "real.py", 10_000)
    _write(tmp_path, "vendor.dll", 5_000_000)
    _write(tmp_path, "debug.pdb", 5_000_000)
    _write(tmp_path, "logo.png", 1_000_000)

    estimate, by_kind = parse.estimate_repo_cost(tmp_path, **_filter())

    assert set(by_kind) == {parse._CODE}
    assert by_kind[parse._CODE] == 10_000
    assert estimate == pytest.approx(10_000 * parse.KIND_COST[parse._CODE])


def test_the_estimate_matches_what_the_walker_actually_selects(tmp_path):
    """The load-bearing test. Two code paths deciding "is this file indexed?"
    drift, and this repository has already shipped that defect once, where the
    single-source and workspace index paths diverged.

    Builds a tree exercising every skip the walker applies, then asserts the
    estimator selected exactly the same files.
    """
    _write(tmp_path, "keep.py", 500)
    _write(tmp_path, "nested/keep.js", 400)
    _write(tmp_path, "data.xml", 300)
    _write(tmp_path, "skipme.dll", 900_000)          # no parser claims it
    _write(tmp_path, "node_modules/dep/index.js", 700)   # pruned directory
    _write(tmp_path, ".git/objects/blob", 700)           # pruned directory
    _write(tmp_path, "huge.py", 6_000_000)               # over max_file_bytes

    counts = parse.WalkCounts()
    walked = {
        sf.rel.replace("\\", "/")
        for sf in parse._walk_source_files(
            tmp_path, skip_generated=False, counts=counts, **_filter())
    }
    _, by_kind = parse.estimate_repo_cost(tmp_path, **_filter())

    # The walker read these; the estimator must have counted the same bytes.
    assert walked == {"keep.py", "nested/keep.js", "data.xml"}, walked
    assert by_kind == {parse._CODE: 900, parse._XML: 300}, by_kind


def test_an_oversized_code_file_costs_nothing_because_the_walk_drops_it(tmp_path):
    _write(tmp_path, "huge.py", 6_000_000)
    estimate, by_kind = parse.estimate_repo_cost(tmp_path, **_filter())
    assert estimate == 0
    assert by_kind == {}


def test_an_unmeasured_kind_takes_the_cheap_default_not_the_worst_case():
    """A kind nobody measured must not inherit code's 19.6x and refuse a
    repository on a number that was never measured."""
    assert parse.DEFAULT_KIND_COST < parse.KIND_COST[parse._CODE]
    assert parse.DEFAULT_KIND_COST == parse.KIND_COST[parse._SQL]


# ---- the refusal ----------------------------------------------------------

def test_a_repo_over_budget_is_refused_before_anything_is_parsed(tmp_path, monkeypatch):
    """Refused on the estimate, not after the shard exists. Proven by making
    the parser explode: if a single file reached it, this raises the wrong
    exception."""
    _write(tmp_path, "big.py", 200_000)

    def _boom(*a, **kw):
        raise AssertionError("a file was parsed despite the budget refusal")

    monkeypatch.setattr(parse, "_walk_source_files", _boom)

    with pytest.raises(parse.RepoTooLarge) as excinfo:
        parse.index_repo_dir(str(tmp_path), "wide", max_repo_memory=1_000_000)

    exc = excinfo.value
    assert exc.repo_id == "wide"
    assert exc.estimate_bytes > exc.budget_bytes
    # The message has to name the cause and the escape, or the user is told
    # only that something is too big.
    text = str(exc)
    assert "code" in text
    assert "max_repo_memory" in text
    # `kb.languages`, the config key, not `--languages`: there has never been such a
    # flag, and this assertion is why the wrong advice survived. It pinned the message
    # to a control the CLI does not have.
    assert "kb.languages" in text


def test_a_repo_within_budget_indexes_normally(tmp_path):
    _write(tmp_path, "small.py", 200)
    shard = parse.index_repo_dir(str(tmp_path), "small", max_repo_memory=3 * 1024 ** 3)
    assert shard.repo == "small"


def test_no_budget_means_no_check(tmp_path):
    """None is the default and every existing caller gets it, so adding the
    parameter cannot change behaviour for anyone who has not opted in."""
    _write(tmp_path, "big.py", 200_000)
    shard = parse.index_repo_dir(str(tmp_path), "unbounded", max_repo_memory=None)
    assert shard.repo == "unbounded"


def test_the_budget_bounds_aggregate_where_the_per_file_cap_cannot(tmp_path):
    """The exact shape of the real failure, in miniature: many files, every one
    comfortably under the per-file cap, adding up to more than the budget.

    `max_file_bytes` is deliberately left at its default here. If the aggregate
    budget were removed this repository would index, which is what happened on
    the real fleet.
    """
    for i in range(60):
        _write(tmp_path, f"part{i:03d}.py", 50_000)      # each 0.05 MB, cap is 4.77 MB

    estimate, by_kind = parse.estimate_repo_cost(tmp_path, **_filter())
    assert by_kind[parse._CODE] == 60 * 50_000
    assert max(
        (tmp_path / f"part{i:03d}.py").stat().st_size for i in range(60)
    ) < 5_000_000, "the per-file cap must not be what refuses this"

    with pytest.raises(parse.RepoTooLarge):
        parse.index_repo_dir(str(tmp_path), "wide", max_repo_memory=int(estimate) - 1)
