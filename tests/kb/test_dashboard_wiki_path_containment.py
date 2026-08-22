"""The dashboard's wiki and generated-document routes must not read outside their
directory under the store.

``repo_id`` (URL path) and ``module`` (``?module=``) both become part of a filename.
The name builder replaced only ``/``, so a ``\\``-separated value walked out of the
wiki directory on Windows -- contextlake ships a Windows binary, and CI is Linux-only,
so no existing test could have caught it.

Two layers are asserted here, deliberately:

* the filename builder folds every path separator, and
* the read site verifies the resolved path is *inside* the wiki directory.

The second is what closes the class rather than this spelling of it, so it is tested
independently of the first -- including with the sanitizer bypassed.
"""

from pathlib import Path

import pytest

from contextlake.kb.cmds.wiki import _module_wiki_filename
from contextlake.kb.dashboard.data import _docs_out, _wiki_out, _within


class _Store:
    """The one method ``_wiki_out`` calls."""

    def get_repo(self, repo_id):
        return None


# --- layer 1: the filename builder ----------------------------------------


def test_legitimate_module_prefix_keeps_its_exact_filename():
    """Golden: the shape existing pages are named with must not drift.

    ``_module_wiki_filename`` is used for *writes* during wiki generation too, so a
    change here orphans every page already on disk. This test exists to make that
    breakage loud rather than silent.
    """
    assert _module_wiki_filename("team/app", "src/foo") == "team__app__src__foo.md"
    assert _module_wiki_filename("team/app", "src") == "team__app__src.md"
    assert _module_wiki_filename("app", "pkg.sub-mod_1") == "app__pkg.sub-mod_1.md"


def test_non_ascii_prefixes_are_preserved():
    """The allowlist is \\w-based, so unicode directory names still round-trip."""
    assert _module_wiki_filename("team/app", "café") == "team__app__café.md"


@pytest.mark.parametrize("hostile", [
    "..\\..\\..\\..\\Users\\victim\\secret",
    "../../../../etc/passwd",
    "a/../../b",
    "x\x00y",
])
def test_no_path_separator_survives_the_filename_builder(hostile):
    name = _module_wiki_filename("team/app", hostile)
    assert "/" not in name
    assert "\\" not in name
    assert "\x00" not in name
    # and it stays a single component whichever OS reads it
    assert Path(name).name == name


# --- layer 2: containment at the read site --------------------------------


def test_within_rejects_escapes_and_unresolvable_paths(tmp_path):
    base = tmp_path / "wiki"
    base.mkdir()
    assert _within(base, base / "page.md")
    assert not _within(base, tmp_path / "outside.md")
    assert not _within(base, base / ".." / "outside.md")
    # a NUL byte makes resolve() raise ValueError, not OSError -- must fail closed
    assert not _within(base, Path(str(base) + "/\x00x.md"))


def test_wiki_read_refuses_a_file_outside_the_wiki_dir(tmp_path, monkeypatch):
    """Bypass the sanitizer to prove the read site refuses on its own.

    If containment depended on the filename builder, this would read the secret.
    """
    store_dir = tmp_path / "store"
    (store_dir / "wiki" / "_modules").mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("CANARY at commit `abc`\n", encoding="utf-8")

    monkeypatch.setattr("contextlake.kb.cmds.wiki._module_wiki_filename",
                        lambda repo_id, module: "../../secret.md")

    out = _wiki_out(_Store(), store_dir, "team/app", module="anything")
    assert out == {"found": False, "stale": True, "html": None}
    assert out["html"] is None


def test_a_legitimate_module_page_still_reads(tmp_path):
    """The containment check must not break the ordinary path."""
    store_dir = tmp_path / "store"
    mods = store_dir / "wiki" / "_modules"
    mods.mkdir(parents=True)
    (mods / _module_wiki_filename("team/app", "src/foo")).write_text(
        "# Module\n\nat commit `abc1234`\n", encoding="utf-8")

    out = _wiki_out(_Store(), store_dir, "team/app", module="src/foo")
    assert out["found"] is True
    assert "Module" in out["html"]


def test_traversal_via_repo_id_is_also_refused(tmp_path):
    """The whole-repo branch builds its name from `repo_id`, which is equally
    request-supplied -- the audit only named `?module=`, but `repo_slug` replaces
    `/` and nothing else, so the branch has the same shape."""
    store_dir = tmp_path / "store"
    (store_dir / "wiki").mkdir(parents=True)
    (tmp_path / "secret.md").write_text("CANARY\n", encoding="utf-8")

    out = _wiki_out(_Store(), store_dir, "..\\..\\secret")
    assert out["found"] is False


# --- the same class, on the generated-document route ----------------------

def test_docs_read_refuses_a_file_outside_the_docs_dir(tmp_path, monkeypatch):
    """`/api/repo/<id>/docs` builds a filename from `repo_id` the same way, so it
    inherits the same exposure and needs the check proved independently of the
    name builder."""
    from contextlake.kb.cmds.docs import API_DIR

    store_dir = tmp_path / "store"
    store_dir.joinpath(*API_DIR).mkdir(parents=True)
    # The canary sits where the traversal actually LANDS. `docs/api` is two levels
    # under the store, so "../../secret" resolves to <store>/secret.md -- outside the
    # docs directory, which is what containment is for. An earlier version of this
    # test put the canary in tmp_path, one level further out, so the escaped path
    # pointed at a file that did not exist and the test passed with the guard
    # deleted. A traversal test whose target is absent proves nothing.
    secret = store_dir / "secret.md"
    secret.write_text("CANARY\n", encoding="utf-8")

    monkeypatch.setattr("contextlake.kb.visualize.repo_slug",
                        lambda repo_id: "../../secret")

    out = _docs_out(_Store(), store_dir, "team/app", "api")
    assert out["found"] is False, "the read site followed a path out of the docs dir"
    assert out["html"] is None


def test_docs_traversal_via_repo_id_is_also_refused(tmp_path):
    """`repo_slug` replaces `/` and nothing else, so a backslash-separated id walks
    out of the directory on Windows, where contextlake ships a binary and CI does
    not run."""
    from contextlake.kb.cmds.docs import API_DIR

    store_dir = tmp_path / "store"
    store_dir.joinpath(*API_DIR).mkdir(parents=True)
    (tmp_path / "secret.md").write_text("CANARY\n", encoding="utf-8")

    out = _docs_out(_Store(), store_dir, "..\\..\\secret", "api")
    assert out["found"] is False


def test_a_legitimate_docs_page_still_reads(tmp_path):
    """The containment check must not break the ordinary path."""
    from contextlake.kb.cmds.docs import API_DIR
    from contextlake.kb.visualize import repo_slug

    store_dir = tmp_path / "store"
    d = store_dir.joinpath(*API_DIR)
    d.mkdir(parents=True)
    (d / (repo_slug("team/app") + ".md")).write_text(
        "# Reference\n\nA real page.\n", encoding="utf-8")

    out = _docs_out(_Store(), store_dir, "team/app", "api")
    assert out["found"] is True
    assert "Reference" in out["html"]
