"""`--site` must not overwrite a directory it did not create.

`build_dashboard_site` writes `index.html` unconditionally. Pointed at a directory that
already holds something else, it overwrote it and said nothing: the command succeeded and
the damage sat in the working tree until someone read a diff.

That is not hypothetical. On 2026-08-25 this repository's own hand-maintained landing page,
`site/index.html`, was replaced by the dashboard SPA. It was found on 2026-09-02, a week
later, and only because an unrelated `git status` listed it. The published site was never
affected, because the clobbered file was never committed -- which is luck, not a safeguard.

The rule is ownership, not emptiness. An export may overwrite its own output and nothing
else, so re-running into the same directory stays idempotent without a flag, and that
idempotence is asserted below rather than assumed.

File names do not show ownership. A hand-kept docs site has an `index.html`, and anyone can
name a file `wiki-notes.html`, so a folder is the export's only when it holds the marker
file the export writes (`EXPORT_MARKER`). The first version of the guard trusted the names
and was walked around with a lone `index.html`.
"""

from __future__ import annotations

import pytest

from contextlake.kb import visualize as viz
from contextlake.kb.dashboard.site import _OWN_FILES, _refuse_foreign_dir, build_dashboard_site
from contextlake.kb.model import Node, Repo
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize.html_render import EXPORT_MARKER


def test_a_directory_holding_someone_elses_files_is_refused(tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.html").write_text("<h1>the hand-maintained landing page</h1>")
    (out / "quickstart.html").write_text("docs")     # NOT something the export writes
    with pytest.raises(SystemExit) as e:
        _refuse_foreign_dir(out)
    msg = str(e.value)
    assert "quickstart.html" in msg, "the refusal does not name what it found"
    # Naming the foreign file is what tells the reader which directory they picked.
    assert "refusing to build into" in msg


def test_a_lone_index_html_without_the_marker_is_refused(tmp_path):
    """The case the first guard let through: the export writes `index.html`, so a folder
    holding only that looked like its own. Nothing shows the export wrote it, and the
    refusal has to say what is missing."""
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.html").write_text("<h1>MY LANDING</h1>")
    with pytest.raises(SystemExit) as e:
        _refuse_foreign_dir(out)
    assert EXPORT_MARKER in str(e.value) and "index.html" in str(e.value)


def test_the_marker_does_not_excuse_a_foreign_file(tmp_path):
    """The marker shows an earlier export made the folder. A file the export does not
    write, added since, is still someone's and still refuses the run."""
    out = tmp_path / "site"
    out.mkdir()
    (out / EXPORT_MARKER).write_text("")
    (out / "index.html").write_text("x")
    (out / "notes.txt").write_text("mine")
    with pytest.raises(SystemExit) as e:
        _refuse_foreign_dir(out)
    assert "notes.txt" in str(e.value)


def test_its_own_output_is_overwritable(tmp_path):
    """Re-running an export into the same folder is the normal case and must not need a
    flag. Built from `_OWN_FILES` rather than a hand-copied list, so a new output file
    added to the exporter cannot make its own re-run start failing."""
    out = tmp_path / "site"
    out.mkdir()
    (out / EXPORT_MARKER).write_text("")     # what an earlier export left
    for name in _OWN_FILES:
        if name == "graph":
            (out / name).mkdir()
        else:
            (out / name).write_text("x")
    _refuse_foreign_dir(out)          # must not raise


def test_a_new_or_empty_directory_is_fine(tmp_path):
    _refuse_foreign_dir(tmp_path / "does-not-exist-yet")   # must not raise
    empty = tmp_path / "empty"
    empty.mkdir()
    _refuse_foreign_dir(empty)                              # must not raise


def test_dotfiles_do_not_block_an_export(tmp_path):
    """A `.gitignore` or `.nojekyll` beside an export is routine and is not someone's
    content. Blocking on those would make the guard fire on the ordinary case, which is
    how a guard gets disabled."""
    out = tmp_path / "site"
    out.mkdir()
    (out / ".nojekyll").write_text("")
    (out / ".gitignore").write_text("*.tmp")
    _refuse_foreign_dir(out)          # must not raise


def test_the_real_landing_page_would_have_been_caught(tmp_path):
    """The 2026-08-25 case, reconstructed: a docs-site folder with the landing page and
    its generated siblings."""
    out = tmp_path / "site"
    out.mkdir()
    for name in ("index.html", "quickstart.html", "docs.css", "graph-embed.html",
                 "llms-full.txt", "build_docs.py"):
        (out / name).write_text("x")
    with pytest.raises(SystemExit):
        _refuse_foreign_dir(out)


def test_the_real_export_actually_calls_the_guard(tmp_path):
    """The tests above call `_refuse_foreign_dir` directly, so they pass whether or not
    anything invokes it. Deleting the call from `_emit` left all five of them green -- a
    correct function with no consumer, which is the shape this codebase has shipped
    before. This one drives `build_dashboard_site` and is the only test here that fails
    when the call goes missing.
    """
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.html").write_text("<h1>a hand-maintained landing page</h1>")
    (out / "quickstart.html").write_text("docs")

    with pytest.raises(SystemExit) as e:
        build_dashboard_site(tmp_path / "store", out, sample=True)
    assert "refusing to build into" in str(e.value)
    # and it refused BEFORE writing: the page it would have overwritten is untouched
    assert out.joinpath("index.html").read_text() == "<h1>a hand-maintained landing page</h1>"


# --- `kb graph --site` and the dashboard's `graph/` folder -------------------------------
#
# The guard first lived in the dashboard export only. `kb graph --site DIR` calls
# `visualize.build_site` directly, which wrote `index.html` and the shared assets over
# whatever DIR held, and the dashboard let its `graph` folder through by name without
# looking inside. The check now lives in `build_site`, so every caller has it.

@pytest.fixture
def graph_store(tmp_path):
    kb = tmp_path / "kb"
    (kb / "wiki").mkdir(parents=True)
    store = SqliteStore(kb / "index.sqlite")
    store.upsert_repo(Repo(id="team/app", path="/app"))
    store.upsert_nodes("team/app", [Node(id="a1", repo="team/app", kind="function", name="a1")])
    (kb / "wiki" / "team__app.md").write_text("# team/app\n\nA page.\n")
    yield store
    store.close()


def _docs_site(tmp_path):
    out = tmp_path / "docs-site"
    out.mkdir()
    (out / "index.html").write_text("<h1>the hand-maintained landing page</h1>")
    (out / "quickstart.html").write_text("docs")      # NOT something build_site writes
    return out


def test_build_site_refuses_a_directory_holding_someone_elses_files(graph_store, tmp_path):
    out = _docs_site(tmp_path)
    with pytest.raises(SystemExit) as e:
        viz.build_site(graph_store, out)
    assert "quickstart.html" in str(e.value), "the refusal does not name what it found"
    # refused BEFORE writing: the landing page is intact and no asset was added
    assert out.joinpath("index.html").read_text() == "<h1>the hand-maintained landing page</h1>"
    assert sorted(p.name for p in out.iterdir()) == ["index.html", "quickstart.html"]


def test_an_anonymised_build_keeps_a_foreign_wiki_page(graph_store, tmp_path):
    """`anonymize=True` deletes every `wiki-*.html` in the folder, so it must not run in a
    folder that is not an earlier export."""
    out = tmp_path / "notes"
    out.mkdir()
    (out / "wiki-keepme.html").write_text("my notes")
    (out / "todo.txt").write_text("x")
    with pytest.raises(SystemExit):
        viz.build_site(graph_store, out, anonymize=True)
    assert out.joinpath("wiki-keepme.html").read_text() == "my notes"


def test_file_names_alone_do_not_make_a_folder_the_exports(graph_store, tmp_path):
    """The walk-around the first guard allowed: a hand-kept `index.html` plus a
    `wiki-*.html` note holds only names the export writes, so it passed, was overwritten,
    and with `anonymize=True` lost the note. Without the marker there is no sign the
    export wrote either file."""
    out = tmp_path / "notes"
    out.mkdir()
    (out / "index.html").write_text("MY LANDING")
    (out / "wiki-keep.html").write_text("NOTES")
    for anonymize in (False, True):
        with pytest.raises(SystemExit) as e:
            viz.build_site(graph_store, out, anonymize=anonymize)
        assert EXPORT_MARKER in str(e.value)
        assert out.joinpath("index.html").read_text() == "MY LANDING"
        assert out.joinpath("wiki-keep.html").read_text() == "NOTES"
        assert sorted(p.name for p in out.iterdir()) == ["index.html", "wiki-keep.html"]


def test_the_refusal_says_how_to_adopt_an_earlier_export(graph_store, tmp_path):
    """A folder an older version made has no marker. The message names the command that
    adds it, and running that command is enough to rebuild into the folder."""
    out = tmp_path / "old-export"
    out.mkdir()
    (out / "index.html").write_text("an older export's index")
    with pytest.raises(SystemExit) as e:
        viz.build_site(graph_store, out)
    assert f"touch {out / EXPORT_MARKER}" in str(e.value)
    (out / EXPORT_MARKER).touch()
    viz.build_site(graph_store, out)                    # must not raise
    assert "an older export's index" not in out.joinpath("index.html").read_text()


def test_build_site_marks_the_folder_it_writes(graph_store, tmp_path):
    out = tmp_path / "fresh"
    viz.build_site(graph_store, out)
    assert (out / EXPORT_MARKER).is_file()


def test_build_site_can_rebuild_into_its_own_output(graph_store, tmp_path):
    """Built from the real function, so an output name left out of the guard's list makes
    the second run refuse its own first run."""
    out = tmp_path / "site"
    (out).mkdir()
    (out / ".nojekyll").write_text("")                 # a dotfile is not content
    viz.build_site(graph_store, out)
    assert (out / "wiki-team__app.html").is_file(), "the fixture wrote no wiki page"
    assert (out / "repo-team__app.html").is_file()
    viz.build_site(graph_store, out)                    # must not raise
    viz.build_site(graph_store, out, anonymize=True)    # deletes the wiki page, must not raise
    assert not (out / "wiki-team__app.html").exists()


def test_the_dashboard_looks_inside_its_graph_folder(tmp_path):
    """`graph` is one of the export's own names, so the top-level check passes it. A
    `graph` folder holding other files is still someone's, and the refusal has to come
    before the top-level files are written."""
    out = tmp_path / "site"
    (out / "graph").mkdir(parents=True)
    (out / EXPORT_MARKER).write_text("")       # the top level is proven; `graph` is not
    (out / "graph" / "notes.txt").write_text("mine")
    with pytest.raises(SystemExit) as e:
        build_dashboard_site(tmp_path / "store", out, sample=True)
    assert "notes.txt" in str(e.value)
    assert sorted(p.name for p in out.iterdir()) == [EXPORT_MARKER, "graph"], \
        "a refused export wrote files before it refused"
    assert out.joinpath("graph", "notes.txt").read_text() == "mine"


def test_a_graph_folder_alone_does_not_make_a_dashboard_folder(tmp_path):
    """`graph` is one of the export's own names, so a folder holding only a `graph`
    folder of hand-kept pages named like the export's own passed the first guard."""
    out = tmp_path / "foreign"
    (out / "graph").mkdir(parents=True)
    (out / "graph" / "index.html").write_text("MY LANDING")
    (out / "graph" / "wiki-keepme.html").write_text("NOTES")
    with pytest.raises(SystemExit) as e:
        build_dashboard_site(tmp_path / "store", out, sample=True, anonymize=True)
    assert EXPORT_MARKER in str(e.value)
    assert sorted(p.name for p in out.iterdir()) == ["graph"]
    assert sorted(p.name for p in (out / "graph").iterdir()) == ["index.html", "wiki-keepme.html"]
    assert (out / "graph" / "index.html").read_text() == "MY LANDING"
    assert (out / "graph" / "wiki-keepme.html").read_text() == "NOTES"


def test_the_dashboard_marks_both_folders_and_rebuilds_into_them(tmp_path):
    out = tmp_path / "site"
    build_dashboard_site(tmp_path / "store", out, sample=True)
    assert (out / EXPORT_MARKER).is_file() and (out / "graph" / EXPORT_MARKER).is_file()
    build_dashboard_site(tmp_path / "store", out, sample=True)        # must not raise
    build_dashboard_site(tmp_path / "store", out, sample=True, anonymize=True)


def test_a_marked_dashboard_folder_does_not_vouch_for_a_hand_placed_graph_folder(tmp_path):
    """The marker on the top level shows the export made the top level. A `graph` folder
    inside it that holds pages named like the export's own, but no marker of its own, is
    not proven: the export must not write the proof and then rely on it, or an anonymised
    run deletes the `wiki-*.html` note."""
    out = tmp_path / "site"
    (out / "graph").mkdir(parents=True)
    (out / EXPORT_MARKER).write_text("")
    (out / "graph" / "index.html").write_text("MY LANDING")
    (out / "graph" / "wiki-keepme.html").write_text("NOTES")
    with pytest.raises(SystemExit) as e:
        build_dashboard_site(tmp_path / "store", out, sample=True, anonymize=True)
    assert f"touch {out / 'graph' / EXPORT_MARKER}" in str(e.value)
    assert sorted(p.name for p in out.iterdir()) == [EXPORT_MARKER, "graph"], \
        "a refused export wrote files before it refused"
    assert sorted(p.name for p in (out / "graph").iterdir()) == ["index.html", "wiki-keepme.html"]
    assert (out / "graph" / "index.html").read_text() == "MY LANDING"
    assert (out / "graph" / "wiki-keepme.html").read_text() == "NOTES"


def test_an_earlier_dashboard_export_needs_a_marker_in_each_folder(tmp_path):
    """An export made before the marker existed has none in either folder. Each refusal
    names the file to add, and both come before anything is written."""
    out = tmp_path / "site"
    build_dashboard_site(tmp_path / "store", out, sample=True)
    (out / EXPORT_MARKER).unlink()
    (out / "graph" / EXPORT_MARKER).unlink()
    (out / "dashboard.css").write_text("OLD")      # a file the export would rewrite
    with pytest.raises(SystemExit) as e:
        build_dashboard_site(tmp_path / "store", out, sample=True)
    assert f"touch {out / EXPORT_MARKER}" in str(e.value)
    (out / EXPORT_MARKER).touch()
    with pytest.raises(SystemExit) as e:
        build_dashboard_site(tmp_path / "store", out, sample=True)
    assert f"touch {out / 'graph' / EXPORT_MARKER}" in str(e.value)
    assert (out / "dashboard.css").read_text() == "OLD", \
        "a refused export wrote files before it refused"
    (out / "graph" / EXPORT_MARKER).touch()
    build_dashboard_site(tmp_path / "store", out, sample=True)        # must not raise
    assert (out / "dashboard.css").read_text() != "OLD"
