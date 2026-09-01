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
"""

from __future__ import annotations

import pytest

from contextlake.kb.dashboard.site import _OWN_FILES, _refuse_foreign_dir, build_dashboard_site


def test_a_directory_holding_someone_elses_files_is_refused(tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.html").write_text("<h1>the hand-maintained landing page</h1>")
    (out / "quickstart.html").write_text("docs")     # NOT something the export writes
    with pytest.raises(SystemExit) as e:
        _refuse_foreign_dir(out)
    msg = str(e.value)
    assert "quickstart.html" in msg, "the refusal does not name what it found"
    # index.html alone would NOT have been enough to refuse -- the export writes that one.
    # Naming the foreign file is what tells the reader which directory they picked.
    assert "refusing to build into" in msg


def test_its_own_output_is_overwritable(tmp_path):
    """Re-running an export into the same folder is the normal case and must not need a
    flag. Built from `_OWN_FILES` rather than a hand-copied list, so a new output file
    added to the exporter cannot make its own re-run start failing."""
    out = tmp_path / "site"
    out.mkdir()
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
