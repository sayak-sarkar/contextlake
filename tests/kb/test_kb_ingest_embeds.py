"""`kb ingest` embeds what it stores, and fails loudly when the embedder does.

Until this file, CI never ran `cmd_ingest`'s embed branch. CI has no embedding engine,
so `build_embedder` returns None there and the branch is skipped. The dev machine has
model2vec, so the same tests took the branch there by downloading a model from the Hub,
and a network failure failed six of them at once (flake #17). A fake embedder runs the
branch on every machine, with no network.
"""

from __future__ import annotations

import json
import types

import pytest

from contextlake.cli import main
from contextlake.kb import embeddings
from contextlake.kb.embeddings.store import build_vector_store


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        return [[1.0, float(i)] for i, _ in enumerate(texts)]


class _DeadEmbedder:
    name = "dead"

    def embed(self, texts):
        raise ConnectionError("embedder unreachable")


def _ingest(tmp_path, monkeypatch, embedder):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\nstep one\n")
    (docs / "faq.md").write_text("# FAQ\nq and a\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs), "--config", str(cfg)])
    return e.value.code


def _vector_count(store_dir, part="@ingest:cli"):
    vs = build_vector_store(store_dir / "embeddings.sqlite")
    try:
        return vs.count_repo(part)
    finally:
        vs.close()


def test_ingest_embeds_every_document_it_stores(tmp_path, monkeypatch, capsys):
    assert _ingest(tmp_path, monkeypatch, _FakeEmbedder()) == 0

    assert _vector_count(tmp_path / "kb") == 2
    assert "2 embedded into the semantic store" in capsys.readouterr().out


def test_an_embedder_that_fails_makes_ingest_exit_nonzero_and_say_so(
        tmp_path, monkeypatch, capsys):
    assert _ingest(tmp_path, monkeypatch, _DeadEmbedder()) == 1

    out = capsys.readouterr().out
    assert "EMBED INCOMPLETE: 0 of 2" in out
    assert "Ingest incomplete" in out
    # The store check refuses a mismatched model only; a dead embedder is the embed phase's.
    assert "This run changed nothing" not in out


class _JsonResponse:
    """A 200 answer carrying ``payload`` as JSON, shaped like what the api source reads."""

    class headers:                                           # noqa: N801 - mimics a message
        @staticmethod
        def get_content_charset():
            return "utf-8"

        @staticmethod
        def get(_name, default=None):
            return default

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ingest_api_records(tmp_path, monkeypatch, records):
    """Run `kb ingest --path <url> --source-type api` over ``records``.

    Returns the exit code and the `(node_id, name)` rows of the `@ingest:cli` partition.
    """
    import sqlite3

    import contextlake.kb.sources.api as apimod

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(apimod, "_OPENER", types.SimpleNamespace(
        open=lambda req, timeout=None: _JsonResponse(records)))
    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: _FakeEmbedder())
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", "http://items.invalid/all", "--source-type", "api",
              "--config", str(cfg)])

    db = sqlite3.connect(tmp_path / "kb" / "index.sqlite")
    try:
        rows = db.execute("SELECT node_id, name FROM nodes WHERE repo_id = '@ingest:cli' "
                          "ORDER BY name").fetchall()
    finally:
        db.close()
    return e.value.code, rows


def test_records_that_repeat_or_lack_an_id_are_all_stored_and_counted(
        tmp_path, monkeypatch, capsys):
    """Four records, two sharing the id d1 and two with a null id, ran as `4 document(s)`
    and `4 embedded` while the store held two nodes and two vectors: the later record of
    each pair replaced the earlier one, and nothing said so."""
    from contextlake.kb.sources.base import id_without_one

    code, rows = _ingest_api_records(tmp_path, monkeypatch, [
        {"id": "d1", "title": "First", "text": "one"},
        {"id": "d1", "title": "Second", "text": "two"},
        {"id": None, "title": "Third", "text": "three"},
        {"id": None, "title": "Fourth", "text": "four"},
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert sorted(rows) == sorted([
        (f"@ingest:cli:{id_without_one('Third', 'three')}", "Third"),
        (f"@ingest:cli:{id_without_one('Fourth', 'four')}", "Fourth"),
        ("@ingest:cli:d1", "First"), ("@ingest:cli:d1#2", "Second")])
    assert _vector_count(tmp_path / "kb") == 4
    assert "cli: 1 document(s) repeated the id of an earlier one" in out
    assert "Set id_field to a field that is unique per record" in out
    assert "cli: 4 document(s), 4 embedded" in out
    assert "Ingest complete: 4 document(s) aggregated, 4 embedded" in out


def test_a_record_without_an_id_never_takes_the_id_of_a_record_that_has_one(
        tmp_path, monkeypatch):
    """A null id took its position in the list, so a null at position 1 took the id "1" and
    the real record with that id was renamed "1#2": a real record's node id depended on
    the other records of its source."""
    from contextlake.kb.sources.base import id_without_one

    code, rows = _ingest_api_records(tmp_path, monkeypatch, [
        {"id": "a", "title": "Named", "text": "named"},
        {"id": None, "title": "Anon", "text": "no id"},
        {"id": "1", "title": "Real one", "text": "a real id that looks like a position"},
        {"title": "Absent", "text": "no id key at all"},
    ])

    assert code == 0
    assert dict((name, nid) for nid, name in rows) == {
        "Named": "@ingest:cli:a", "Real one": "@ingest:cli:1",
        "Anon": f"@ingest:cli:{id_without_one('Anon', 'no id')}",
        "Absent": f"@ingest:cli:{id_without_one('Absent', 'no id key at all')}"}


def test_the_repeated_id_line_names_id_field_only_for_a_source_that_has_one(
        tmp_path, monkeypatch, capsys):
    """The hint was worded for the api source and printed for every type."""
    import contextlake.kb.sources as sources
    from contextlake.kb.sources.base import Document

    class _Repeats:
        def iter_documents(self):
            return iter([Document(id="x", title="One", text="one"),
                         Document(id="x", title="Two", text="two")])

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sources, "build_source", lambda _type, **_o: _Repeats())
    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: None)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')

    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(tmp_path), "--config", str(cfg)])

    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "cli: 1 document(s) repeated the id of an earlier one" in out
    assert "id_field" not in out


def test_a_suffix_never_takes_an_id_that_a_later_record_has():
    from contextlake.kb.cmds.ingest import _distinct_id

    ids = ("a", "a", "a#2", "a", "b")
    taken: set[str] = set()
    out = [_distinct_id(i, taken, frozenset(ids)) for i in ids]
    assert out == ["a", "a#3", "a#2", "a#4", "b"]
    assert len(set(out)) == len(out)


class _IdentifiedEmbedder:
    """Differs from another instance only in `identity`, as two models do."""

    def __init__(self, identity, *, flaky_first=False, script=()):
        self.identity = self.name = identity
        self._flaky_first = flaky_first
        self._script = list(script)  # replies for the first calls: an exception or a list
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self._flaky_first and self.calls == 1:
            raise ConnectionError("first call timed out")
        if self._script:
            reply = self._script.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return [[1.0, float(i)] for i, _ in enumerate(texts)]


def test_a_changed_embedder_is_refused_before_any_vector_is_cleared(
        tmp_path, monkeypatch, capsys):
    """Changing `[embeddings] model` and running `kb ingest` printed the identity-guard error
    and exited 1, but only after the partition's vectors had been cleared: semantic search
    lost the vectors of the first source's documents, and the second source never ran."""
    import contextlake.kb.cmds.ingest as ingest_cmd

    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"{name}.md").write_text(f"# {name}\n{name} body\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n' + "".join(
        f'[[sources]]\nname = "{n}"\ntype = "files"\npath = "{tmp_path / n}"\n'
        for n in ("alpha", "beta")))

    def run(identity):
        monkeypatch.setattr(embeddings, "build_embedder",
                            lambda _cfg: _IdentifiedEmbedder(identity))
        with pytest.raises(SystemExit) as e:
            main(["kb", "ingest", "--config", str(cfg)])
        return e.value.code

    assert run("openai:fake-a") == 0
    store_dir = tmp_path / "kb"
    assert _vector_count(store_dir, "@ingest:alpha") == 1
    assert _vector_count(store_dir, "@ingest:beta") == 1
    capsys.readouterr()

    shards: list = []
    real_write = ingest_cmd.write_shard
    monkeypatch.setattr(ingest_cmd, "write_shard",
                        lambda *a, **k: (shards.append(a[1].repo), real_write(*a, **k))[1])
    assert run("openai:fake-b") == 1

    out = capsys.readouterr().out
    assert _vector_count(store_dir, "@ingest:alpha") == 1
    assert _vector_count(store_dir, "@ingest:beta") == 1
    assert shards == [], "the refused run rewrote a partition"
    assert "built with embedder 'openai:fake-a' but the current embedder is 'openai:fake-b'" in out
    assert "This run changed nothing" in out


def _two_runs(tmp_path, monkeypatch, files):
    """A `run(embedder)` helper over one files source `alpha` that holds ``files``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / "alpha"
    folder.mkdir()
    for name in files:
        (folder / name).write_text(f"# {name}\n{name} body\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n[[sources]]\nname = "alpha"\n'
                   f'type = "files"\npath = "{folder}"\n')

    def run(embedder):
        monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)
        with pytest.raises(SystemExit) as e:
            main(["kb", "ingest", "--config", str(cfg)])
        return e.value.code

    return folder, run


def test_a_probe_that_got_no_answer_does_not_let_a_changed_embedder_clear_vectors(
        tmp_path, monkeypatch, capsys):
    """The probe returned "no verdict" for an embedder that timed out, and the run went on
    as if the guard had passed: the sweep cleared the partition's vectors, and the next
    embed call (which answered, with a new model) was refused by the guard afterwards."""
    _folder, run = _two_runs(tmp_path, monkeypatch, ["a.md"])
    assert run(_IdentifiedEmbedder("openai:fake-a")) == 0
    store_dir = tmp_path / "kb"
    assert _vector_count(store_dir, "@ingest:alpha") == 1
    capsys.readouterr()

    assert run(_IdentifiedEmbedder("openai:fake-b", flaky_first=True)) == 1

    out = capsys.readouterr().out
    assert _vector_count(store_dir, "@ingest:alpha") == 1, "a refused run cleared the vectors"
    assert "built with embedder 'openai:fake-a' but the current embedder is 'openai:fake-b'" in out
    # The documents were rewritten before the first batch reached the guard.
    assert "This run changed nothing" not in out


@pytest.mark.parametrize(
    ("files", "reply"),
    [(["a.md"], []),
     (["a.md"], [[]]),
     (["a.md", "b.md"], [[1.0, 0.0], []]),
     (["a.md", "b.md"], [[1.0, 0.0], [1.0, 0.0, 0.0]])],
    ids=["no-vectors", "empty-vector", "empty-second-vector", "mixed-lengths"])
def test_a_probe_that_got_no_answer_and_a_batch_with_no_vector_clear_nothing(
        tmp_path, monkeypatch, capsys, files, reply):
    """With the probe unanswered the guard has not run, so the deferred sweep waits for a
    batch whose vectors all hold a value and share one length. A first batch that came back
    empty used to reach the sweep, and the strict zip raised after the partition's vectors
    were gone. A batch with good first vector and a bad second one passed the check on the
    first vector alone, and the vector store raised after the sweep."""
    _folder, run = _two_runs(tmp_path, monkeypatch, files)
    assert run(_IdentifiedEmbedder("openai:fake-a")) == 0
    store_dir = tmp_path / "kb"
    assert _vector_count(store_dir, "@ingest:alpha") == len(files)
    capsys.readouterr()

    # call 1 is the probe (no answer), call 2 is the first batch (no usable vector). The
    # model is the seeded one, so the guard cannot refuse and only the batch check can stop
    # the sweep.
    embedder = _IdentifiedEmbedder("openai:fake-a",
                                   script=[ConnectionError("probe timed out"), reply])
    assert run(embedder) == 1

    assert _vector_count(store_dir, "@ingest:alpha") == len(files), \
        "a reply with no usable vector cleared them"
    assert "EMBED INCOMPLETE" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [[], ["--exit-zero-on-partial"]],
                         ids=["default", "exit-zero-on-partial"])
def test_a_refusal_after_an_unanswered_probe_ends_the_run_and_says_what_did_not_run(
        tmp_path, monkeypatch, capsys, extra):
    """With the probe unanswered, the guard first runs on the first batch of the first
    source. Its ValueError escaped `cmd_ingest`: the second source never ran, no summary
    printed, and the output ended in one bare error line."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"{name}.md").write_text(f"# {name}\n{name} body\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n' + "".join(
        f'[[sources]]\nname = "{n}"\ntype = "files"\npath = "{tmp_path / n}"\n'
        for n in ("alpha", "beta")))

    def run(embedder, *flags):
        monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)
        with pytest.raises(SystemExit) as e:
            main([*flags, "kb", "ingest", "--config", str(cfg)])
        return e.value.code

    assert run(_IdentifiedEmbedder("openai:fake-a")) == 0
    store_dir = tmp_path / "kb"
    capsys.readouterr()

    assert run(_IdentifiedEmbedder("openai:fake-b",
                                   script=[ConnectionError("probe timed out")]), *extra) == 1

    out = capsys.readouterr().out
    assert "alpha: 1 document(s), EMBED REFUSED: this embedding store was built" in out
    assert "The vectors were kept." in out
    assert "Not run, because every source shares the vector store: beta" in out
    assert "Ingest incomplete: 1 document(s) aggregated (1 source(s) failed; " \
           "1 source(s) not run)" in out
    assert "beta:" not in out
    assert _vector_count(store_dir, "@ingest:alpha") == 1
    assert _vector_count(store_dir, "@ingest:beta") == 1


@pytest.mark.parametrize(
    ("script", "cleared", "alpha", "beta", "tail"),
    [
        # The probe answers with the stored length, the sweep clears alpha, alpha's first
        # batch comes back with another length and is refused.
        ([[[1.0, 0.0]], [[1.0, 0.0, 0.0]]], "alpha", 0, 1, True),
        # The probe gets no answer, alpha embeds, and beta, cleared because alpha passed,
        # is refused on its first batch.
        ([ConnectionError("probe timed out"), [[1.0, 0.0]], [[1.0, 0.0, 0.0]]],
         "beta", 1, 0, False),
    ],
    ids=["answered-probe", "earlier-source-passed"])
def test_a_refusal_after_the_sweep_says_the_vectors_were_cleared(
        tmp_path, monkeypatch, capsys, script, cleared, alpha, beta, tail):
    """A source is cleared before it writes once a vector has passed the guard in the run
    (the probe answered, or an earlier source embedded). A batch with another vector length
    is still refused after that, and the output said `The vectors were kept.` over a
    partition the sweep had emptied."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"{name}.md").write_text(f"# {name}\n{name} body\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n' + "".join(
        f'[[sources]]\nname = "{n}"\ntype = "files"\npath = "{tmp_path / n}"\n'
        for n in ("alpha", "beta")))

    def run(embedder):
        monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)
        with pytest.raises(SystemExit) as e:
            main(["kb", "ingest", "--config", str(cfg)])
        return e.value.code

    assert run(_IdentifiedEmbedder("openai:fake-a")) == 0
    store_dir = tmp_path / "kb"
    assert _vector_count(store_dir, "@ingest:alpha") == 1
    assert _vector_count(store_dir, "@ingest:beta") == 1
    capsys.readouterr()

    assert run(_IdentifiedEmbedder("openai:fake-a", script=script)) == 1

    out = capsys.readouterr().out
    assert f"{cleared}: 1 document(s), EMBED REFUSED: this embedding store was built with " \
           "dimension 2 but the current embedder produces 3" in out
    assert f"The vectors of {cleared} were cleared before the refusal." in out
    assert "The vectors were kept." not in out
    assert ("Not run, because every source shares the vector store: beta" in out) is tail
    assert _vector_count(store_dir, "@ingest:alpha") == alpha
    assert _vector_count(store_dir, "@ingest:beta") == beta


def test_a_probe_that_got_no_answer_still_sweeps_once_the_embedder_passes(
        tmp_path, monkeypatch, capsys):
    """The sweep moves behind the guard; it does not go away. A document deleted from the
    source must lose its vector on a run whose probe timed out and whose embedder answered."""
    folder, run = _two_runs(tmp_path, monkeypatch, ["a.md", "b.md"])
    assert run(_IdentifiedEmbedder("openai:fake-a")) == 0
    store_dir = tmp_path / "kb"
    assert _vector_count(store_dir, "@ingest:alpha") == 2
    (folder / "b.md").unlink()
    capsys.readouterr()

    assert run(_IdentifiedEmbedder("openai:fake-a", flaky_first=True)) == 0

    assert _vector_count(store_dir, "@ingest:alpha") == 1, "the deleted document kept its vector"


def test_a_source_after_one_that_passed_the_check_clears_before_it_writes(
        tmp_path, monkeypatch, capsys):
    """With the probe unanswered, the first source waits to clear until a batch passes the
    check. Once one source has embedded, the guard has passed for the run, so each later
    source clears before it writes, as on a run whose probe answered. The docs say so: an
    embedder that answers alpha and then stops leaves beta without vectors."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"{name}.md").write_text(f"# {name}\n{name} body\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n' + "".join(
        f'[[sources]]\nname = "{n}"\ntype = "files"\npath = "{tmp_path / n}"\n'
        for n in ("alpha", "beta")))

    def run(embedder):
        monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)
        with pytest.raises(SystemExit) as e:
            main(["kb", "ingest", "--config", str(cfg)])
        return e.value.code

    assert run(_IdentifiedEmbedder("openai:fake-a")) == 0
    store_dir = tmp_path / "kb"
    assert _vector_count(store_dir, "@ingest:alpha") == 1
    assert _vector_count(store_dir, "@ingest:beta") == 1
    capsys.readouterr()

    # call 1 is the probe (no answer), call 2 is alpha's batch (answers), call 3 is beta's.
    assert run(_IdentifiedEmbedder("openai:fake-a", script=[
        ConnectionError("probe timed out"), [[1.0, 0.0]], ConnectionError("down")])) == 1

    assert "EMBED INCOMPLETE" in capsys.readouterr().out
    assert _vector_count(store_dir, "@ingest:alpha") == 1
    assert _vector_count(store_dir, "@ingest:beta") == 0, \
        "once alpha passed the check, beta's vectors are cleared before its own batch"


def test_an_embedder_that_stays_down_leaves_the_stored_vectors_alone(
        tmp_path, monkeypatch, capsys):
    """A dead embedder cleared the partition's vectors and then failed to write new ones,
    so one outage emptied semantic search for everything the run had read."""
    _folder, run = _two_runs(tmp_path, monkeypatch, ["a.md"])
    assert run(_IdentifiedEmbedder("openai:fake-a")) == 0
    store_dir = tmp_path / "kb"
    capsys.readouterr()

    assert run(_DeadEmbedder()) == 1

    assert "EMBED INCOMPLETE" in capsys.readouterr().out
    assert _vector_count(store_dir, "@ingest:alpha") == 1, "an outage cleared the vectors"


def test_a_sweep_behind_the_guard_still_clears_when_every_body_is_empty(tmp_path):
    """No batch is ever embedded for a partition whose documents all have empty bodies, so
    the sweep cannot wait for one: the old vectors still have to go. That holds for an
    embedder that stays down too, because no embed call is made and the old vectors belong
    to text that no longer exists."""
    from contextlake.kb.cmds.ingest import _embed_documents
    from contextlake.kb.model import Node

    for embedder in (_FakeEmbedder(), _DeadEmbedder()):
        store_dir = tmp_path / embedder.name
        store_dir.mkdir()
        vs = build_vector_store(store_dir / "embeddings.sqlite")
        try:
            vs.upsert([("n1", "@ingest:alpha", [1.0, 0.0])])
            assert vs.count_repo("@ingest:alpha") == 1
            node = Node(id="@ingest:alpha:n1", repo="@ingest:alpha", kind="document", name="n1")
            assert _embed_documents(vs, embedder, "@ingest:alpha", [node], [""], 8,
                                    sweep=True) == (0, 0, None)
            assert vs.count_repo("@ingest:alpha") == 0
        finally:
            vs.close()
