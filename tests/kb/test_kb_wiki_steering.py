"""`.contextlake/wiki.toml`: a repository's own say in how its wiki page is written.

Two keys, two very different risks. `notes` is prose quoted into a generated page, so it is
attributed rather than absorbed and it is bounded. `pages` selects among modules the graph
already found, so a file cannot invent a page however it is written.
"""


import pytest

from contextlake.kb.wiki.steering import (
    MAX_NOTE_CHARS,
    STEERING_PATH,
    read_wiki_steering,
    steering_file,
)


def _write(root, body: str):
    d = root / ".contextlake"
    d.mkdir(parents=True, exist_ok=True)
    (d / "wiki.toml").write_text(body, encoding="utf-8")
    return root


# --- the path itself ------------------------------------------------------------------

def test_the_steering_path_is_exactly_dot_contextlake_wiki_toml():
    """Pinned as LITERAL strings, deliberately not built from `STEERING_PATH`.

    The first version of this module shipped `"\\u200b.contextlake"` -- a zero-width space had
    crept into the literal. Every byte of the feature worked; it simply looked for a directory
    no filesystem contains. Ruff cannot see it, and a fixture that writes its file via
    `STEERING_PATH` would have written to the same wrong place and passed. Comparing against
    typed literals is the only assertion that fails.
    """
    assert STEERING_PATH == (".contextlake", "wiki.toml")
    assert steering_file("/tmp/x").as_posix() == "/tmp/x/.contextlake/wiki.toml"
    for part in STEERING_PATH:
        assert part.isprintable() and part == part.strip()
        assert all(ord(c) < 128 for c in part), f"non-ASCII in path component {part!r}"


# --- reading --------------------------------------------------------------------------

def test_a_repo_with_no_steering_file_gets_empty_lists(tmp_path):
    assert read_wiki_steering(tmp_path) == {"notes": [], "pages": []}
    assert read_wiki_steering(None) == {"notes": [], "pages": []}


def test_notes_accepts_a_single_string_or_a_list(tmp_path):
    """Both are natural things to write, so neither is a silent no-op."""
    one = read_wiki_steering(_write(tmp_path, 'notes = "a thin client"'))
    assert one["notes"] == ["a thin client"]
    many = read_wiki_steering(_write(tmp_path, 'notes = ["first", "second"]'))
    assert many["notes"] == ["first", "second"]


def test_a_note_that_is_not_a_string_is_dropped(tmp_path):
    """Quoting a number's repr into a wiki page would put obvious nonsense in front of a
    reader with contextlake's name on it."""
    out = read_wiki_steering(_write(tmp_path, 'notes = ["real", 42, "also real"]'))
    assert out["notes"] == ["real", "also real"]


def test_a_note_is_bounded(tmp_path):
    out = read_wiki_steering(_write(tmp_path, f'notes = "{"x" * (MAX_NOTE_CHARS + 500)}"'))
    assert len(out["notes"][0]) == MAX_NOTE_CHARS


def test_malformed_toml_is_reported_and_ignored_not_fatal(tmp_path, gls_logs):
    """One unparseable file in one clone must not cost a fleet-wide run its output."""
    out = read_wiki_steering(_write(tmp_path, "notes = [unclosed"))
    assert out == {"notes": [], "pages": []}
    assert any("not readable TOML" in r.getMessage() for r in gls_logs.records)


def test_pages_preserves_order_and_drops_duplicates(tmp_path):
    out = read_wiki_steering(_write(tmp_path, 'pages = ["api", "core", "api", "ui"]'))
    assert out["pages"] == ["api", "core", "ui"]


# --- the page ---------------------------------------------------------------------------

def test_notes_are_quoted_and_attributed_never_asserted(tmp_path):
    """Everything else on the page is derived from the graph. This is the repository
    asserting something about itself, and a reader weighs those differently."""
    from contextlake.kb.wiki.structural import render_structural_page

    page = render_structural_page({"langs": {"go": 1}}, repo_id="t/a",
                                  notes=["Prefer the server docs."])
    assert "> Prefer the server docs." in page
    assert "From the repository's own `.contextlake/wiki.toml`" in page
    assert "not derived from the graph" in page
    # above the first section, because it is guidance for reading them
    assert page.index("Prefer the server docs") < page.index("## ")


def test_a_page_without_notes_is_unchanged(tmp_path):
    from contextlake.kb.wiki.structural import render_structural_page

    plain = render_structural_page({"langs": {"go": 1}}, repo_id="t/a")
    assert "wiki.toml" not in plain
    assert plain == render_structural_page({"langs": {"go": 1}}, repo_id="t/a", notes=[])


# --- the page plan ------------------------------------------------------------------------

class _Store:
    def __init__(self, modules):
        self._modules = modules


@pytest.fixture
def patched_modules(monkeypatch):
    def _set(modules):
        import contextlake.kb.visualize.payload as payload
        monkeypatch.setattr(payload, "repo_modules", lambda store, repo_id: modules)
    return _set


def test_an_explicit_page_list_replaces_the_heuristic(patched_modules):
    """A maintainer naming subsystems is better evidence than a node-count threshold -- and
    it must work on a SMALL repo, which the heuristic would refuse outright."""
    from contextlake.kb.cmds.wiki import _module_page_plan

    patched_modules([{"prefix": "api", "nodes": 10}, {"prefix": "ui", "nodes": 5}])
    modules, prune = _module_page_plan(_Store(None), "t/a", node_count=50,
                                       override=["ui", "api"])
    assert [m["prefix"] for m in modules] == ["ui", "api"], "order the file gave, not the graph's"
    assert prune is True


def test_a_named_module_the_graph_does_not_hold_is_dropped_not_invented(
        patched_modules, gls_logs):
    from contextlake.kb.cmds.wiki import _module_page_plan

    patched_modules([{"prefix": "api", "nodes": 10}])
    modules, _ = _module_page_plan(_Store(None), "t/a", node_count=50,
                                   override=["api", "does-not-exist"])
    assert [m["prefix"] for m in modules] == ["api"]
    assert any("does not hold" in r.getMessage() for r in gls_logs.records)


def test_an_entirely_wrong_page_list_falls_back_rather_than_deleting_everything(
        patched_modules):
    """Answering a typo with an empty page set would let one bad line silently delete a
    repository's whole module set on the next prune."""
    from contextlake.kb.cmds.wiki import _module_page_plan

    patched_modules([{"prefix": "api", "nodes": 10}])
    modules, _ = _module_page_plan(_Store(None), "t/a", node_count=50, override=["nope"])
    assert modules == [], "small repo, so the heuristic correctly declines"
    # and with a large federated repo the heuristic answers normally
    patched_modules([{"prefix": "api", "nodes": 100}, {"prefix": "ui", "nodes": 90}])
    modules, _ = _module_page_plan(_Store(None), "t/a", node_count=6000, override=["nope"])
    assert [m["prefix"] for m in modules] == ["api", "ui"]


def test_no_override_leaves_the_heuristic_exactly_as_it_was(patched_modules):
    from contextlake.kb.cmds.wiki import _module_page_plan

    patched_modules([{"prefix": "api", "nodes": 100}, {"prefix": "ui", "nodes": 90}])
    assert _module_page_plan(_Store(None), "t/a", node_count=50)[0] == []
    assert len(_module_page_plan(_Store(None), "t/a", node_count=6000)[0]) == 2
