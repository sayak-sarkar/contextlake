"""CLI integration tests for the knowledge-layer verbs (the Phase 2.0 DoD)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from contextlake import style
from contextlake.cli import main
from contextlake.kb import commands as commands_mod
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "fixtures" / "sample-graph.json"


def _kb_config(tmp_path) -> Path:
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg


def _embeddings_enabled_config(tmp_path) -> Path:
    """A config with embeddings on. The relevance floor is gated on that, because
    it is also what decides whether the MCP server exposes semantic search at all
    -- so the two surfaces agree exactly where both actually do vector search."""
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n\n[embeddings]\nenabled = true\n')
    return cfg


def _bare_git_repo(path: Path) -> None:
    """A real (structurally valid), remote-less, commit-less git repo -- unlike
    a bare ``mkdir(".git")``, this is a state ``git`` itself recognizes as this
    directory's own repo, which discover_repos's is_own_gitdir guard requires."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True,
                   capture_output=True, text=True)


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


class _SpyProgress:
    """Stand-in for style.Progress that only records call counts (no rendering),
    so the wire-through test stays deterministic and stream-free. Same pattern
    as test_kb_wiki.py's _SpyProgress."""

    instances: list["_SpyProgress"] = []

    def __init__(self, total, **kwargs):
        self.total = total
        self.label = kwargs.get("label")
        self.advance_calls = 0
        self.done_calls = 0
        _SpyProgress.instances.append(self)

    def advance(self, *args, **kwargs):
        self.advance_calls += 1

    def done(self, *args, **kwargs):
        self.done_calls += 1


def test_index_then_query_round_trip(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0

    # the store on disk is populated
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert store.get_node("demo_app_catalogservice").name == "CatalogService"
    assert store.stats().edges == 1
    store.close()

    # query finds it (cited)
    capsys.readouterr()
    assert _run(["kb", "query", "CatalogService", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "CatalogService" in out and "demo/app" in out


def test_query_retriever_semantic_degrades_to_fts_without_embeddings(tmp_path, capsys):
    # embeddings explicitly disabled (not just left unset -- load_kb_config still
    # merges ~/.contextlake/kb.toml's [embeddings] table over an unset one, so this
    # must override it, not just omit it) -- --retriever semantic must still find
    # the symbol via an honest fts fallback, not error out or try to download a model.
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n\n[embeddings]\nenabled = false\n')
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    assert _run(["kb", "query", "CatalogService", "--config", str(cfg),
                "--retriever", "semantic"]) == 0
    captured = capsys.readouterr()
    assert "CatalogService" in captured.out
    assert "showing fts results instead" in (captured.out + captured.err)


def test_query_retriever_semantic_respects_kind_filter(tmp_path, capsys, monkeypatch):
    # eval's --kind is a harmless no-op under semantic/hybrid (nothing renders it), but
    # query prints results straight to the user -- `--kind function --retriever semantic`
    # silently ignoring --kind would show a class result with no signal it was unfiltered.
    # Bypasses the real embedder (network-touching, see the fts-degrade test above) by
    # monkeypatching _semantic_results directly with both fixture node ids/kinds.
    from contextlake.kb.cmds import query as query_mod

    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0

    monkeypatch.setattr(
        query_mod, "_semantic_results",
        lambda args, store, text, limit: ["demo_app_catalogservice", "demo_app_charge"])

    capsys.readouterr()
    # "CatalogService" is indexed, so the query clears the relevance floor and the
    # stubbed retriever's ids get through -- this test is about --kind, not the floor.
    assert _run(["kb", "query", "CatalogService", "--config", str(cfg),
                "--retriever", "semantic", "--kind", "function"]) == 0
    out = capsys.readouterr().out
    assert "charge" in out and "CatalogService" not in out


def test_index_workspace_indexes_each_repo(tmp_path):
    ws = tmp_path / "ws"
    _bare_git_repo(ws / "r1")
    (ws / "r1" / "a.py").write_text("def f():\n    pass\n")
    _bare_git_repo(ws / "r2")
    (ws / "r2" / "b.py").write_text("class C:\n    def m(self):\n        pass\n")
    cfg = _kb_config(tmp_path)

    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert {r.id for r in store.list_repos()} == {"r1", "r2"}
    assert store.nodes_by_name("f") and store.nodes_by_name("C")
    store.close()


def test_index_transparently_migrates_a_store_from_the_old_id_scheme(tmp_path):
    """An upgrade scenario: a store built before repo_id canonicalization has a
    row keyed by the old workspace-relative path. Running `index` again -- the
    normal, only command a user would run -- must migrate it to the canonical
    id and re-derive its content, with no manual step and no leftover ghost row
    under the old id."""
    import subprocess

    from contextlake.kb.model import Repo
    from contextlake.kb.store.shards import GraphShard, shard_path, write_shard

    ws = tmp_path / "ws"
    repo_dir = ws / "team" / "widgets"
    repo_dir.mkdir(parents=True)
    (repo_dir / "a.py").write_text("def f():\n    pass\n")
    for args in (["init", "-q"], ["config", "user.email", "a@b.c"],
                 ["config", "user.name", "a"], ["add", "-A"],
                 ["commit", "-q", "-m", "init"],
                 ["remote", "add", "origin", "https://example.com/acme/widgets.git"]):
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True)

    cfg = _kb_config(tmp_path)
    store_dir = tmp_path / "kb"
    old_id = "team/widgets"   # the pre-canonicalization scheme: path relative to --workspace
    store = SqliteStore(store_dir / "index.sqlite")
    store.upsert_repo(Repo(id=old_id, path=str(repo_dir)))
    write_shard(store_dir, GraphShard(repo=old_id, nodes=[], edges=[]))
    store.close()

    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0

    store = SqliteStore(store_dir / "index.sqlite")
    ids = {r.id for r in store.list_repos()}
    assert ids == {"example.com/acme/widgets"}   # migrated, not duplicated
    assert store.nodes_by_name("f")               # re-derived, not just relabeled
    store.close()
    assert not shard_path(store_dir, old_id).exists()   # old shard cleaned up


def test_index_empty_workspace_fails_honestly(tmp_path, capsys):
    # 0 repos indexed = an empty graph no agent can cite from; that must be a
    # loud non-zero exit, not a green checkmark (it also makes bootstrap abort).
    ws = tmp_path / "empty-ws"
    ws.mkdir()
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 1
    assert "No git repositories found" in capsys.readouterr().out


def test_index_without_source_indexes_cwd(tmp_path, monkeypatch):
    cfg = _kb_config(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("def widget():\n    pass\n")
    monkeypatch.chdir(proj)  # no --source/--workspace -> index the current directory
    assert _run(["kb", "index", "--config", str(cfg)]) == 0
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert store.nodes_by_name("widget")  # cwd got indexed
    assert {r.id for r in store.list_repos()} == {"proj"}  # repo id = cwd dir name
    store.close()


def test_index_without_source_warns_when_cwd_bundles_nested_repos(tmp_path, monkeypatch, capsys):
    """cwd itself isn't a git repo but contains one that is -- found live,
    indexing a workspace root this way silently bundled a real mirrored repo's
    files into one made-up repo id instead of the nested repo's own identity,
    duplicating data once `index --workspace .` was later run properly. Not
    changed (still a valid, if narrower, use case) -- just warned about."""
    cfg = _kb_config(tmp_path)
    workspace = tmp_path / "workspace"
    _bare_git_repo(workspace / "nested-repo")
    (workspace / "nested-repo" / "app.py").write_text("def widget():\n    pass\n")
    monkeypatch.chdir(workspace)
    assert _run(["kb", "index", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "nested-repo" in out and "--workspace ." in out


def test_index_missing_source_errors_cleanly(tmp_path, capsys):
    """A --source that is on no disk and in no store: one clear line, no traceback.
    It reads as an id lookup because that is the only thing left to try once the
    path is not there, and the message says so rather than reporting a missing
    file for what may well have been a valid repository id."""
    cfg = _kb_config(tmp_path)
    code = _run(["kb", "index", "--config", str(cfg), "--source", str(tmp_path / "nope.json")])
    out = capsys.readouterr().out
    assert code == 1
    assert "Traceback" not in out
    assert "neither a path on disk nor an indexed repository id" in out


def test_index_invalid_shard_errors_cleanly(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text('{"nodes": []}')  # valid JSON, missing required 'repo'
    code = _run(["kb", "index", "--config", str(cfg), "--source", str(bad)])
    out = capsys.readouterr().out
    assert code == 1
    assert "Traceback" not in out and "not a valid graph shard" in out


def test_query_without_text_is_usage_error(tmp_path):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "query", "--config", str(cfg)]) == 2


def test_doctor_reports_ok(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out.lower()
    assert "doctor" in out
    assert "fts5" in out
    assert code == 0  # git + fts5 present in the test environment


def test_doctor_reports_builtin_model_presence(tmp_path, capsys):
    store_dir = tmp_path / "kb"
    # Point the model cache at an empty temp dir so the presence report is deterministic:
    # otherwise doctor inspects the machine-global default (~/.contextlake/models) and a
    # developer who has already fetched the models sees "downloaded", failing the assertion.
    cache_dir = tmp_path / "models"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n'
        f'[embeddings]\nenabled = true\nprovider = "builtin"\ncache_dir = "{cache_dir}"\n'
        f'[llm]\nenabled = true\nprovider = "builtin"\ncache_dir = "{cache_dir}"\n'
    )
    _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    # filesystem-only presence report against the empty cache -> not downloaded
    assert "built-in embedder model" in out
    assert "potion-base-8M" in out
    assert "Qwen2.5-0.5B-Instruct-GGUF" in out
    assert "not downloaded" in out


def test_doctor_warns_when_builtin_llm_runtime_missing(tmp_path, capsys, monkeypatch):
    # doctor must not show a green wiki-LLM when the model file is present but the
    # llama-cpp-python runtime is absent (it would fail at wiki time). Simulate the
    # missing runtime by making find_spec('llama_cpp') return None.
    real_find_spec = commands_mod.importlib.util.find_spec

    def fake_find_spec(name, *a, **k):
        return None if name == "llama_cpp" else real_find_spec(name, *a, **k)

    monkeypatch.setattr(commands_mod.importlib.util, "find_spec", fake_find_spec)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        f'[llm]\nenabled = true\nprovider = "builtin"\ncache_dir = "{tmp_path / "models"}"\n'
    )
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "runtime not installed" in out
    # the hint is the executable remediation, not a pip line the user must know to
    # amend with the CPU wheel index (llama-cpp-python ships no PyPI wheels)
    assert "doctor --fix llm-local" in out
    assert code == 0  # optional tier: a missing wiki-LLM runtime is advisory, not a failure


def test_doctor_reports_per_source_reachability(tmp_path, capsys, monkeypatch):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n'
        '[[sources]]\ntype = "atlassian"\nname = "jira"\nmcp = "https://x"\n'
        '[[sources]]\ntype = "figma"\nname = "designs"\nmcp = "https://y"\n'
    )
    # cmd_doctor imports verify_source lazily from source_cmd at call time (it
    # stays off commands.py's own import graph, see the tomlkit-eagerness
    # test below), so the patch target is source_cmd, not commands.
    from contextlake.kb import source_cmd

    calls = []

    def fake_verify_source(src, timeout=None):
        calls.append(timeout)
        if src.name == "jira":
            return True, "2 site(s) reachable"
        return False, "MCP configured, but design file 'X' was not reachable"

    monkeypatch.setattr(source_cmd, "verify_source", fake_verify_source)
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "jira" in out and "atlassian" in out and "2 site(s) reachable" in out
    assert "designs" in out and "figma" in out and "not reachable" in out
    # a source being unreachable is advisory -- it never fails doctor's verdict
    assert code == 0
    # doctor bounds every per-source reachability call so an unreachable
    # connector can't stall it at the connector's own default timeout
    assert calls == [8, 8]


def test_doctor_source_with_no_reachability_check_is_advisory_not_fatal(
        tmp_path, capsys):
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n'
        '[[sources]]\ntype = "gitlab"\nname = "gl"\n'
    )
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "gl" in out and "gitlab" in out
    assert "no reachability check" in out
    assert code == 0


def test_read_only_commands_do_not_import_tomlkit_eagerly(tmp_path):
    """commands.py must not pull in tomlkit merely by being imported, or by
    running a read-only command (query/index) -- config_edit's "read path
    stays dependency-light" contract only holds if commands.py's own imports
    of source_cmd/config_edit are lazy. Run in a subprocess: other tests in
    this session may already have imported tomlkit, which would make an
    in-process sys.modules check unreliable. The subprocess cwd is pinned to
    tmp_path (not the repo root) on general principle: this test isn't about
    repo-root state, so it shouldn't run from a directory that has any."""
    import subprocess
    import sys

    fixture = str(FIXTURE)
    code = (
        "import sys\n"
        "assert 'tomlkit' not in sys.modules\n"
        "import contextlake.kb.commands\n"
        "assert 'tomlkit' not in sys.modules, 'tomlkit imported by module import'\n"
        "from contextlake.cli import main\n"
        "import tempfile, os\n"
        "d = tempfile.mkdtemp()\n"
        "cfg = os.path.join(d, 'kb.toml')\n"
        "open(cfg, 'w').write('[kb]\\nstore_dir = \"' + d + '/kb\"\\n')\n"
        "try:\n"
        "    main(['kb', 'index', '--config', cfg, '--source', " + repr(fixture) + "])\n"
        "except SystemExit as e:\n"
        "    assert e.code == 0\n"
        "try:\n"
        "    main(['kb', 'query', 'CatalogService', '--config', cfg])\n"
        "except SystemExit as e:\n"
        "    assert e.code == 0\n"
        "assert 'tomlkit' not in sys.modules, 'tomlkit imported by index/query'\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_unavailable_hint_is_actionable():
    """The embed no-op must tell the user how to turn semantic search on."""
    import importlib.util

    from contextlake.kb.commands import _embed_unavailable_hint
    from contextlake.kb.config import EmbeddingsCfg

    off = _embed_unavailable_hint(EmbeddingsCfg(enabled=False))
    assert "enabled = true" in off                       # the opt-in step is always named
    on_no_engine = _embed_unavailable_hint(EmbeddingsCfg(enabled=True))

    if importlib.util.find_spec("model2vec") is None:
        assert "kb-full" in off                          # tell them to install the embedder
        assert "kb-full" in on_no_engine or "Ollama" in on_no_engine
    else:
        assert "doctor" in on_no_engine                  # engine present -> point at diagnostics


def test_index_workspace_repos_filter(tmp_path):
    # --repos scopes a workspace index to matching repos (glob/substring), matched
    # against the local workspace-relative path (these bare `.git` dirs have no
    # remote, so each repo_id itself falls back to just its own dirname -- the
    # filter still has to see "team/api"-shaped patterns via the local path).
    ws = tmp_path / "ws"
    for r in ("team/api", "team/web", "billing/core", "billing/reports"):
        _bare_git_repo(ws / r)
        (ws / r / "m.py").write_text("class X:\n    pass\n")
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws),
                 "--repos", "billing/*,team/api"]) == 0
    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    assert {r.id for r in store.list_repos()} == {"core", "reports", "api"}
    store.close()


def test_index_workspace_repos_filter_no_match_fails(tmp_path, capsys):
    ws = tmp_path / "ws"
    _bare_git_repo(ws / "team/api")
    (ws / "team/api" / "m.py").write_text("class X:\n    pass\n")
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws),
                 "--repos", "zzz-nope"]) == 1
    assert "matching --repos" in capsys.readouterr().out


def test_index_workspace_reports_progress_and_drops_inline_bar_from_stdout(
    tmp_path, capsys, monkeypatch
):
    """Wire-through: Progress.advance fires once per indexed repo and done() once
    on the shared Progress helper (stderr channel); the stdout detail line keeps
    the 'n nodes, m edges' summary but no longer carries the inline [####] bar,
    which now renders on stderr via Progress instead.

    Goes through cli.main(), which calls setup_logging() and (re)attaches a
    real stdout console handler, so capsys reliably sees log() output here
    (unlike the direct cmd_wiki(...) calls in test_kb_wiki.py, where gls_logs
    is required instead -- see the comment there).

    index_workers = 1 forces the serial _report path deterministically; the
    parallel as_completed path calls the same _report function (see
    src/contextlake/kb/commands.py), so this exercises the shared code that
    both paths run through.
    """
    ws = tmp_path / "ws"
    repo_ids = ["r1", "r2", "r3"]
    for rid in repo_ids:
        _bare_git_repo(ws / rid)
        (ws / rid / "a.py").write_text("def f():\n    pass\n")
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\nindex_workers = 1\n')

    _SpyProgress.instances = []
    monkeypatch.setattr(commands_mod.style, "Progress", _SpyProgress)

    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0

    assert len(_SpyProgress.instances) == 1
    p = _SpyProgress.instances[0]
    assert p.total == len(repo_ids)
    assert p.advance_calls == len(repo_ids)
    assert p.done_calls == 1

    text = capsys.readouterr().out
    detail_lines = [line for line in text.splitlines()
                    if any(f"{rid}: 2 nodes, 1 edges" in line for rid in repo_ids)]
    assert len(detail_lines) == len(repo_ids)
    # the old inline style.bar(...) rendered as e.g. "[██████░░░░░░░░] 1/3" -- assert
    # that block-bar glyph is gone from every per-repo detail line (the timestamp
    # prefix "[HH:MM:SS]" also uses "[", so check for the bar's fill/void glyphs
    # specifically rather than a bare "[").
    for line in detail_lines:
        assert "█" not in line and "░" not in line


def test_index_workspace_summary_points_at_the_log_on_partial_failure(
    tmp_path, capsys, monkeypatch
):
    """A bare '(N failed)' count with no next step leaves the user to scroll
    back through a long fleet-index run to find which repo broke."""
    from contextlake.kb import parse as parse_mod

    ws = tmp_path / "ws"
    repo_ids = ["r1", "r2"]
    for rid in repo_ids:
        _bare_git_repo(ws / rid)
        (ws / rid / "a.py").write_text("def f():\n    pass\n")
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\nindex_workers = 1\n')

    real = parse_mod.index_repo_dir

    def _flaky(path, repo_id, **kw):
        if repo_id == "r2":
            raise RuntimeError("parse failed for r2")
        return real(path, repo_id, **kw)

    monkeypatch.setattr(parse_mod, "index_repo_dir", _flaky)

    rc = _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)])
    assert rc == 1

    text = capsys.readouterr().out
    assert "1 failed" in text
    assert "Re-run to retry" in text


def test_owners_unknown_repo_suggests_close_id(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    # indexing the fixture creates repo id 'demo/app'
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    # a prefix-stripped id ('app') should point at the stored 'demo/app'
    rc = _run(["kb", "owners", "app", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "demo/app" in out and "Did you mean" in out


def test_graph_unknown_repo_suggests_close_id(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    rc = _run(["kb", "graph", "--repo", "demo/ap", "--format", "json", "--config", str(cfg)])
    captured = capsys.readouterr()  # json format redirects logs to stderr
    assert rc == 1
    assert "demo/app" in (captured.out + captured.err)


def test_query_no_match_multiword_hints_semantic_search(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    # a multi-word natural-language query with no keyword hit gets a semantic hint
    assert _run(["kb", "query", "how does the loyalty flow work", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "No matches" in out
    assert "embed" in out and "semantic" in out.lower()


def test_query_json_emits_a_clean_parseable_array(tmp_path, capsys):
    """Before --json existed, piping query's plain-text output to jq failed
    outright -- the only way to consume a hit programmatically."""
    import json

    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    assert _run(["kb", "query", "CatalogService", "--config", str(cfg), "--json"]) == 0
    captured = capsys.readouterr()  # --json redirects logs to stderr like graph does
    assert captured.out.strip(), "payload must be on stdout"
    hits = json.loads(captured.out)
    assert hits == [{
        "repo": "demo/app", "file": "src/catalog.py", "line": 1,
        "kind": "class", "name": "CatalogService",
        "qualified_name": "demo.app.order.CatalogService",
    }]


def test_query_json_empty_argument_is_still_valid_json(tmp_path, capsys):
    """An empty query with --json must fail with a structured JSON error, not a
    plain-text 'usage: ...' log line -- a CI script piping to jq would otherwise
    break on the exact case it asked --json to protect it from."""
    import json

    cfg = _kb_config(tmp_path)
    rc = _run(["kb", "query", "", "--config", str(cfg), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    payload = json.loads(captured.out)
    assert payload["error"] == "missing_argument"


def test_owners_json_emits_a_clean_parseable_object(tmp_path, capsys):
    import json

    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    assert _run(["kb", "owners", "demo/app", "--config", str(cfg), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["repo"] == "demo/app"
    assert payload["path"] is None
    assert isinstance(payload["owners"], list)


def test_owners_json_unknown_repo_reports_the_error_as_json_too(tmp_path, capsys):
    import json

    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    rc = _run(["kb", "owners", "app", "--config", str(cfg), "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out)
    assert payload["error"] == "unknown_repo"
    assert "demo/app" in payload["suggestions"]


def test_owners_json_empty_argument_is_still_valid_json(tmp_path, capsys):
    import json

    cfg = _kb_config(tmp_path)
    rc = _run(["kb", "owners", "", "--config", str(cfg), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    payload = json.loads(captured.out)
    assert payload["error"] == "missing_argument"


def test_lint_json_emits_a_clean_parseable_object(tmp_path, capsys):
    """A fixture repo indexed from a graph-shard JSON never had a checkout, so
    there is no history for it to be behind: lint reports it as shard-imported and
    exits 0. The exit-code contract --json shares with the human path (exit 1 on an
    unclean graph, not just on a malformed request) is pinned by deleting a real
    repo's checkout in the same store, so a CI script checking $? sees the same
    signal a human running `contextlake lint` would."""
    import json
    import shutil

    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    assert _run(["kb", "lint", "--config", str(cfg), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert set(payload) == {"repos", "checked", "stale", "dangling",
                            "parser_stale", "empty", "unreadable", "shard",
                            "stale_repos", "empty_repos", "shard_repos",
                            "unreadable_repos", "parser_stale_repos",
                            "dangling_sample"}
    # Neither stale nor unreadable: telling the reader to re-run index, or to
    # re-clone something that was never cloned, is advice that cannot work.
    assert payload["stale"] == 0 and payload["empty"] == 0
    assert payload["unreadable"] == 0 and payload["shard"] == 1
    # ...but parser-stale it is: the fixture is a hand-written shard carrying no
    # parser_version, so no parser this build knows produced it. That count is
    # reported and deliberately kept out of the exit code.
    assert payload["parser_stale"] == 1
    assert payload["parser_stale_repos"] == payload["shard_repos"]

    # Now make the store genuinely unclean, and --json must say so in its exit code.
    ws = tmp_path / "ws"
    _bare_git_repo(ws / "vanishing")
    (ws / "vanishing" / "a.py").write_text("def f():\n    pass\n")
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0
    shutil.rmtree(ws / "vanishing")
    capsys.readouterr()
    assert _run(["kb", "lint", "--config", str(cfg), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["unreadable"] == 1


def test_impact_json_empty_argument_is_still_valid_json(tmp_path, capsys):
    import json

    cfg = _kb_config(tmp_path)
    rc = _run(["kb", "impact", "", "--config", str(cfg), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    payload = json.loads(captured.out)
    assert payload["error"] == "missing_argument"


def test_impact_json_unknown_target_reports_the_error_as_json_too(tmp_path, capsys):
    import json

    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    rc = _run(["kb", "impact", "TotallyBogusSymbol", "--config", str(cfg), "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out)
    assert payload == {"error": "not_found", "target": "TotallyBogusSymbol"}


def test_impact_json_emits_a_clean_parseable_object(tmp_path, capsys):
    import json

    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    rc = _run(["kb", "impact", "CatalogService", "--config", str(cfg), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["target"]["name"] == "CatalogService"
    assert payload["hops"] == 3
    assert isinstance(payload["affected"], list)


def test_query_no_match_singleword_no_hint(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    # a single-token lookup (a symbol) stays quiet: no semantic-hint noise
    assert _run(["kb", "query", "NoSuchSymbol", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "No matches" in out
    assert "semantic" not in out.lower()


# --- serve ------------------------------------------------------------------

def _serve_args(cfg, **kw):
    defaults = {"config": str(cfg), "transport": None, "host": None, "port": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_serve_http_logs_the_bind_url(tmp_path, gls_logs, monkeypatch):
    # A blocking server that never says where it listens reads as broken, not
    # busy -- the http transport must print its reachable host:port before it
    # blocks in run_server. run_server itself is monkeypatched out so the test
    # doesn't actually block.
    monkeypatch.setenv("FORCE_COLOR", "1")  # deterministic color path
    cfg = _kb_config(tmp_path)
    calls = []
    monkeypatch.setattr(
        "contextlake.kb.server.run_server",
        lambda *a, **kw: calls.append((a, kw)))

    rc = commands_mod.cmd_serve(_serve_args(cfg, transport="http"))

    assert rc == 0
    assert len(calls) == 1  # run_server was reached (and would have blocked)
    # gls_logs.text is ANSI-stripped by pytest's LogCaptureHandler, so read the
    # raw record messages (log()'s actual argument) to compare the colored line.
    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    # streamable-http is mounted at the SDK's streamable_http_path ("/mcp"), not
    # at the bare root -- the root really is a 404, so the path is part of the
    # contract this line reports, not decoration.
    assert style.ok("MCP server on http://127.0.0.1:8765/mcp") in msgs


def test_serve_http_logs_the_configured_host_and_port(tmp_path, gls_logs, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")  # deterministic color path
    cfg = _kb_config(tmp_path)
    monkeypatch.setattr("contextlake.kb.server.run_server", lambda *a, **kw: None)

    # allow_remote: a non-loopback bind is refused without it (see
    # test_serve_matrix.py). Passed here so this test stays about the log line.
    rc = commands_mod.cmd_serve(_serve_args(
        cfg, transport="http", host="0.0.0.0", port=9999, allow_remote=True))

    assert rc == 0
    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    # Asserted with the "/mcp" path: style.ok() only colors the leading glyph, so
    # a path-less expectation stays a substring of the real line and would pass
    # whether or not the path is printed at all.
    assert style.ok("MCP server on http://0.0.0.0:9999/mcp") in msgs


def test_serve_sse_dispatches_the_legacy_transport_and_logs_the_bind_url(
    tmp_path, gls_logs, monkeypatch
):
    # --transport sse is a distinct, real SDK transport (legacy HTTP+SSE), not
    # an alias for stdio or http -- assert it reaches run_server as "sse"
    # exactly, and (like http) reports its bind URL since it also listens on
    # a host:port.
    monkeypatch.setenv("FORCE_COLOR", "1")
    cfg = _kb_config(tmp_path)
    calls = []
    monkeypatch.setattr(
        "contextlake.kb.server.run_server",
        lambda *a, **kw: calls.append((a, kw)))

    rc = commands_mod.cmd_serve(_serve_args(
        cfg, transport="sse", host="0.0.0.0", port=9999, allow_remote=True))

    assert rc == 0
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["transport"] == "sse"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9999
    msgs = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "Serving knowledge graph over MCP (sse)" in msgs
    # SSE clients connect at the sse_path, not the bare root (which is a 404).
    # Same rule as streamable-http's "/mcp" above: each network transport is
    # mounted at its own path, so both bind URLs carry one.
    assert style.ok("MCP server on http://0.0.0.0:9999/sse") in msgs


def test_serve_stdio_does_not_log_a_bind_url(tmp_path, gls_logs, monkeypatch):
    # stdio has no bind address -- it must stay silent on that front (and stdout
    # is reserved for the MCP JSON-RPC stream, not human-facing banners).
    cfg = _kb_config(tmp_path)
    calls = []
    monkeypatch.setattr(
        "contextlake.kb.server.run_server",
        lambda *a, **kw: calls.append((a, kw)))

    rc = commands_mod.cmd_serve(_serve_args(cfg, transport="stdio"))

    assert rc == 0
    assert len(calls) == 1
    # positive control: capture is live on this path (stdio calls use_stderr(),
    # which retargets the console handler's stream but must not silence gls_logs)
    # so the absence checks below are meaningful, not a vacuous empty-log pass.
    assert "Serving knowledge graph over MCP (stdio)" in gls_logs.text
    assert "http://" not in gls_logs.text
    assert "MCP server on" not in gls_logs.text


def test_serve_ctrl_c_closes_the_store_then_hard_exits(tmp_path, gls_logs, monkeypatch):
    """Reproduced directly (a real subprocess, 3 rapid SIGINTs): a second/third
    Ctrl-C landing in the brief window while Python joins the mcp SDK's
    background stdio-reader thread surfaces as a harmless but noisy "Exception
    ignored while joining a thread in _thread._shutdown()" traceback fragment.
    The fix is a hard os._exit() once OUR OWN cleanup has run -- this test
    can't spawn that real subprocess scenario (os._exit would kill the test
    process too), so it mocks os._exit to record the call instead of actually
    exiting, and asserts store.close() happened strictly BEFORE it -- the
    fix must not skip real cleanup on the way to skipping Python's."""
    cfg = _kb_config(tmp_path)
    monkeypatch.setattr(
        "contextlake.kb.server.run_server",
        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt))
    order = []
    real_close = SqliteStore.close
    monkeypatch.setattr(SqliteStore, "close",
                        lambda self: (order.append("store.close"), real_close(self)))
    monkeypatch.setattr("os._exit", lambda code: order.append(f"os._exit({code})"))

    rc = commands_mod.cmd_serve(_serve_args(cfg, transport="stdio"))

    assert rc == 0  # the function's own return value is unaffected by the mock
    assert order == ["store.close", "os._exit(0)"]
    assert "Stopping MCP server" in gls_logs.text


def test_query_semantic_applies_the_same_relevance_floor_as_mcp(tmp_path, capsys,
                                                                monkeypatch):
    """One knowledge base must not answer the same question two ways.

    `semantic_search`/`hybrid_search` over MCP refuse a query with no anchor in
    the index: a nearest-neighbour search returns its k nearest however far away
    they are, and every one is a real node with a real file and line, so the
    answer reads as cited while being about nothing that was asked. `kb query
    --retriever semantic` called the retriever factories directly and skipped
    that check, so the CLI happily printed the k unrelated hits the MCP tool had
    just refused, from the same store.

    The retriever is stubbed to return real indexed ids, which is exactly what a
    real vector index does for an unanchored query -- it has no way to return
    nothing.
    """
    from contextlake.kb.cmds import query as query_mod

    cfg = _embeddings_enabled_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    monkeypatch.setattr(
        query_mod, "_semantic_results",
        lambda args, store, text, limit: ["demo_app_catalogservice", "demo_app_charge"])

    capsys.readouterr()
    rc = _run(["kb", "query", "SamlAssertionValidator", "--config", str(cfg),
               "--retriever", "semantic"])
    out = capsys.readouterr()
    text = out.out + out.err
    # "nothing in here is about that" is a valid answer, so the exit code is 0...
    assert rc == 0
    # ...but nothing unrelated is presented as if it were one.
    assert "CatalogService" not in text and "charge" not in text
    # The refusal names the term, so it is checkable and retryable, not a dead end.
    assert "'SamlAssertionValidator'" in text


def test_query_semantic_json_refusal_is_an_empty_list(tmp_path, capsys, monkeypatch):
    """--json keeps its shape through the refusal: a bare array of hits, empty.
    The reason goes to stderr, where --json already sends every log line, so a
    script piping stdout into a parser is unaffected."""
    import json

    from contextlake.kb.cmds import query as query_mod

    cfg = _embeddings_enabled_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    monkeypatch.setattr(
        query_mod, "_semantic_results",
        lambda args, store, text, limit: ["demo_app_catalogservice"])

    capsys.readouterr()
    assert _run(["kb", "query", "SamlAssertionValidator", "--config", str(cfg),
                 "--retriever", "semantic", "--json"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == []
    assert "SamlAssertionValidator" in out.err


def _repo_with_remote(path: Path, remote: str) -> None:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    (path / "a.py").write_text("def f():\n    pass\n")
    for args in (["init", "-q"], ["config", "user.email", "a@b.c"],
                 ["config", "user.name", "a"], ["add", "-A"],
                 ["commit", "-q", "-m", "init"], ["remote", "add", "origin", remote]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_index_source_accepts_an_indexed_repo_id(tmp_path, capsys):
    """`kb lint` reports a repository by its logical id, whose on-disk path is
    something else entirely, and every obvious way to act on that id failed with
    "No such file or directory". Feeding the id straight back to --source now
    re-indexes that repository, under the id it was already filed as.
    """
    ws = tmp_path / "ws"
    _repo_with_remote(ws / "checkout-dir", "https://example.com/team/widgets.git")
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0

    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    ids = {r.id for r in store.list_repos()}
    store.close()
    assert ids == {"example.com/team/widgets"}   # id and directory name differ

    capsys.readouterr()
    assert _run(["kb", "index", "--config", str(cfg), "--force",
                 "--source", "example.com/team/widgets"]) == 0
    out = capsys.readouterr().out
    assert "is an indexed repository id" in out
    assert "checkout-dir" in out                 # it says where that id actually lives

    store = SqliteStore(tmp_path / "kb" / "index.sqlite")
    try:
        # Re-indexed in place, not filed a second time under the directory name.
        assert {r.id for r in store.list_repos()} == {"example.com/team/widgets"}
    finally:
        store.close()


def test_index_source_accepts_a_unique_repo_id_tail(tmp_path, capsys):
    """The bare name a reader would type resolves when only one repo ends that
    way, so the id does not have to be pasted in full."""
    ws = tmp_path / "ws"
    _repo_with_remote(ws / "d1", "https://example.com/team/widgets.git")
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0

    capsys.readouterr()
    assert _run(["kb", "index", "--config", str(cfg), "--force",
                 "--source", "widgets"]) == 0
    assert "example.com/team/widgets" in capsys.readouterr().out


def test_index_source_unknown_id_says_what_the_store_knows(tmp_path, capsys):
    """The failure mode being fixed is a dead end, so the error has to point
    somewhere: either at the near-miss, or at the ids that do exist."""
    ws = tmp_path / "ws"
    _repo_with_remote(ws / "d1", "https://example.com/team/widgets.git")
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0

    capsys.readouterr()
    assert _run(["kb", "index", "--config", str(cfg),
                 "--source", "./repositories/example.com/team/widget"]) == 1
    out = capsys.readouterr().out
    assert "neither a path on disk nor an indexed repository id" in out
    # The same did-you-mean list every other command gives an unknown repo id.
    assert "Did you mean: example.com/team/widgets" in out

    # Nothing close at all: fall back to naming ids that do exist, so the message
    # still points somewhere rather than repeating the bad name back.
    capsys.readouterr()
    assert _run(["kb", "index", "--config", str(cfg), "--source", "zzqq"]) == 1
    assert "Indexed ids include: example.com/team/widgets" in capsys.readouterr().out


def test_index_source_known_id_with_a_vanished_checkout_says_where_it_was(tmp_path,
                                                                         capsys):
    """Resolving the id is only half an answer: if its recorded checkout is gone,
    say the path, because that is the fact the reader is missing."""
    import shutil

    ws = tmp_path / "ws"
    _repo_with_remote(ws / "d1", "https://example.com/team/widgets.git")
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--workspace", str(ws)]) == 0
    shutil.rmtree(ws / "d1")

    capsys.readouterr()
    assert _run(["kb", "index", "--config", str(cfg),
                 "--source", "example.com/team/widgets"]) == 1
    out = capsys.readouterr().out
    assert "no longer a directory" in out and "d1" in out
