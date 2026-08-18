"""The same event must get the same verdict whichever flag reached it.

`kb graph --node <id>` returned the id unchecked, so a typo produced an empty graph and
exit 0, while the identical miss reached through `--name` or `--search` exits 2 with the
usage banner. A script gating on the exit code could catch a mistyped `--name` and not a
mistyped `--node`.

`kb graph --site --repos <matches nothing>` had the same shape one level up: it wrote a
site of one fleet overview and zero repository pages, logged that zero honestly, and then
printed a green tick over it and exited 0.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contextlake.cli import main
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize.payload import seed_ids_from_args


def _store(tmp_path):
    s = SqliteStore(tmp_path / "index.sqlite")
    check_schema(s)
    s.upsert_nodes("svc-billing", [
        Node(id="svc-billing::charge_card", repo="svc-billing", kind="function",
             name="charge_card", file="billing.py", line_start=1)])
    return s


def test_a_node_id_the_store_does_not_hold_seeds_nothing(tmp_path):
    s = _store(tmp_path)
    try:
        assert seed_ids_from_args(s, SimpleNamespace(node="no-such-id")) == []
    finally:
        s.close()


def test_a_node_id_the_store_does_hold_still_seeds_itself(tmp_path):
    """The guard must reject the absent, not break the ordinary case."""
    s = _store(tmp_path)
    try:
        got = seed_ids_from_args(s, SimpleNamespace(node="svc-billing::charge_card"))
        assert got == ["svc-billing::charge_card"]
    finally:
        s.close()


def _cfg(tmp_path):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    return cfg


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def _indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    repo = tmp_path / "svc-billing"
    repo.mkdir()
    (repo / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    assert _run(["kb", "index", str(repo), "--config", str(cfg)]) == 0
    return cfg


def test_every_not_found_verdict_in_this_command_is_the_same(tmp_path, monkeypatch, capsys):
    """One event, one verdict, whichever flag carried it -- and a NAMED value, not merely
    an equal one.

    Asserting only that two codes match is satisfied by both being wrong together. The
    family is pinned to 1: a well-formed command whose target is not in the graph. Exit 2
    is reserved for asking for nothing at all, which is the one real usage error here.
    """
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    misses = {
        "--node": _run(["kb", "graph", "--node", "no-such-id", "--config", str(cfg)]),
        "--name": _run(["kb", "graph", "--name", "no_such_name", "--config", str(cfg)]),
        "--search": _run(["kb", "graph", "--search", "zzz_no_such_term", "--config", str(cfg)]),
        "--repo": _run(["kb", "graph", "--repo", "no-such-repo", "--config", str(cfg)]),
    }
    assert set(misses.values()) == {1}, f"not-found verdicts disagree: {misses}"


def test_asking_for_nothing_is_still_a_usage_error(tmp_path, monkeypatch, capsys):
    """The break-test for the split above: exit 2 must survive as its own case, or the
    change has simply collapsed two verdicts into one and lost a distinction."""
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    assert _run(["kb", "graph", "--config", str(cfg)]) == 2


def test_a_not_found_seed_is_named_rather_than_answered_with_a_syntax_banner(
        tmp_path, monkeypatch, capsys):
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    _run(["kb", "graph", "--node", "no-such-id", "--config", str(cfg)])
    out = capsys.readouterr()
    printed = out.out + out.err
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert "no-such-id" in printed, "the reader has to be told WHICH id was not found"
    assert "usage:" not in printed, (
        "a correctly spelled command answered with a syntax banner sends the reader to "
        "check syntax that was never wrong")


def test_a_site_filter_matching_nothing_is_refused(tmp_path, monkeypatch, capsys):
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    site = tmp_path / "site"
    code = _run(["kb", "graph", "--site", str(site), "--repos", "no-such-repo",
                 "--config", str(cfg)])
    out = capsys.readouterr()
    printed = out.out + out.err
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert code == 1
    assert "no-such-repo" in printed
    assert not (site / "index.html").exists(), (
        "a refused build must not leave a half-written site behind")


def test_a_site_filter_that_matches_still_builds(tmp_path, monkeypatch, capsys):
    """The guard must reject the empty filter, not every filter."""
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    site = tmp_path / "site2"
    code = _run(["kb", "graph", "--site", str(site), "--repos", "svc-billing",
                 "--config", str(cfg)])
    assert code == 0
    assert (site / "index.html").exists()


def test_an_unfiltered_site_still_builds(tmp_path, monkeypatch, capsys):
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    site = tmp_path / "site3"
    assert _run(["kb", "graph", "--site", str(site), "--config", str(cfg)]) == 0
    assert (site / "index.html").exists()


def test_a_c4_filter_matching_nothing_is_refused(tmp_path, monkeypatch, capsys):
    """Found 40 lines below the `--site` fix, in the same file, on the same flag.

    It wrote a ~600 KB diagram of an empty model, announced "0 namespaces, 0 repos", and
    exited 0 -- the count honest, the tick contradicting it, and a file on disk to make the
    contradiction look like a result.
    """
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    out_file = tmp_path / "c4.html"
    code = _run(["kb", "graph", "--c4", "--repos", "no-such-repo", "--output", str(out_file),
                 "--config", str(cfg)])
    printed = "".join(capsys.readouterr())
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert code == 1
    assert not out_file.exists(), "a refused diagram must not be written"


def test_a_c4_filter_that_matches_still_renders(tmp_path, monkeypatch, capsys):
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    out_file = tmp_path / "c4-ok.html"
    assert _run(["kb", "graph", "--c4", "--repos", "svc-billing", "--output", str(out_file),
                 "--config", str(cfg)]) == 0
    assert out_file.exists()


def test_repos_means_the_same_thing_to_site_and_c4(tmp_path, monkeypatch, capsys):
    """`--repos` matched over two different populations inside one command.

    `--site` looked at repos with parsed nodes, `--c4` at repos-table rows, so a repo in one
    and not the other matched one flag and not its neighbour on the same spelling.
    """
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    site = tmp_path / "s"
    c4 = tmp_path / "c4b.html"
    by_site = _run(["kb", "graph", "--site", str(site), "--repos", "svc-billing",
                    "--config", str(cfg)])
    by_c4 = _run(["kb", "graph", "--c4", "--repos", "svc-billing", "--output", str(c4),
                  "--config", str(cfg)])
    assert by_site == by_c4 == 0
    missing_site = _run(["kb", "graph", "--site", str(tmp_path / "s2"), "--repos", "nope",
                         "--config", str(cfg)])
    missing_c4 = _run(["kb", "graph", "--c4", "--repos", "nope",
                       "--output", str(tmp_path / "c4c.html"), "--config", str(cfg)])
    assert missing_site == missing_c4 == 1


def test_the_repo_match_covers_both_populations_not_just_one(tmp_path):
    """The union is the point, and it needs a fixture where the two sources DIFFER.

    A first version of this file exercised `--repos` only against a repo that appears in
    both the repos table and the parsed-node counts, so replacing the union with either
    half on its own broke nothing and the guard was untested. The two repos below each
    exist in exactly one source.
    """
    from contextlake.kb.cmds.graph import _repos_matching

    s = SqliteStore(tmp_path / "index.sqlite")
    check_schema(s)
    try:
        # A row with no parsed nodes: cloned and registered, never indexed.
        s.upsert_repo(Repo(id="svc-empty", path=str(tmp_path / "svc-empty")))
        # Nodes with no repos-table row: written by a partition-style writer.
        s.upsert_nodes("svc-nodes", [
            Node(id="svc-nodes::charge_card", repo="svc-nodes", kind="function",
                 name="charge_card", file="billing.py", line_start=1)])

        assert _repos_matching(s, ["svc-empty"]) == ["svc-empty"], (
            "a registered repo with no parsed nodes must still match")
        assert _repos_matching(s, ["svc-nodes"]) == ["svc-nodes"], (
            "a repo with parsed nodes but no table row must still match")
        assert _repos_matching(s, ["svc-nothing"]) == []
        assert set(_repos_matching(s, ["svc-*"])) == {"svc-empty", "svc-nodes"}
    finally:
        s.close()
