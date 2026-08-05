"""Tests for per-symbol issue-key candidates: docstring matches (pure) and
git-blame commit-message matches (against a real throwaway git repo)."""

import subprocess

from contextlake.kb.connectors.symbol_refs import keys_from_blame, keys_from_docstrings
from contextlake.kb.model import Node

_PATTERN = r"[A-Z]+-\d+"


def _node(id_, *, kind="function", file=None, line_start=None, doc=None):
    return Node(id=id_, repo="g/app", kind=kind, name=id_, file=file,
                line_start=line_start, attrs={"doc": doc} if doc else {})


# --- docstrings (pure, no subprocess) ---------------------------------------

def test_keys_from_docstrings_matches():
    symbols = [
        _node("s1", doc="See PROJ-42 for context."),
        _node("s2", doc="No ticket here."),
        _node("s3", kind="file", doc="PROJ-99"),  # not an embeddable kind -> skipped
    ]
    assert keys_from_docstrings(symbols, _PATTERN) == {"s1": "PROJ-42"}


def test_keys_from_docstrings_empty_without_doc():
    assert keys_from_docstrings([_node("s1")], _PATTERN) == {}


# --- git blame (real throwaway repo) -----------------------------------------

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    return repo


def test_keys_from_blame_matches_commit_subject(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "mod.py"
    f.write_text("def a():\n    pass\n\n\ndef b():\n    pass\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "PROJ-7: add a and b")

    symbols = [
        _node("sa", file="mod.py", line_start=1),
        _node("sb", file="mod.py", line_start=5),
    ]
    assert keys_from_blame(str(repo), symbols, _PATTERN) == {
        "sa": "PROJ-7", "sb": "PROJ-7",
    }


def test_keys_from_blame_no_match_leaves_symbol_out(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "mod.py"
    f.write_text("def a():\n    pass\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "just a refactor, no ticket")

    assert keys_from_blame(str(repo), [_node("sa", file="mod.py", line_start=1)],
                           _PATTERN) == {}


def test_keys_from_blame_missing_repo_is_empty_not_raising(tmp_path):
    symbols = [_node("sa", file="mod.py", line_start=1)]
    assert keys_from_blame(str(tmp_path / "nope"), symbols, _PATTERN) == {}


def test_keys_from_blame_ignores_non_embeddable_and_lineless_symbols(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "mod.py"
    f.write_text("x = 1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "PROJ-1: init")

    symbols = [
        _node("no_line", file="mod.py", line_start=None),
        _node("no_file", file=None, line_start=1),
        _node("wrong_kind", kind="module", file="mod.py", line_start=1),
    ]
    assert keys_from_blame(str(repo), symbols, _PATTERN) == {}


def test_keys_from_blame_survives_non_utf8_commit_subject(tmp_path, commit_raw_bytes):
    """`git blame --line-porcelain` replays the commit subject in its `summary`
    line, so a cp1252 byte in the message reaches the decoder here too. Strict
    decoding turned that into a UnicodeDecodeError, which is a ValueError and so
    slipped past this module's OSError/SubprocessError guard entirely."""
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("def a():\n    pass\n")
    _git(repo, "add", "mod.py")
    # commit_raw_bytes, never `git commit -F` -- see the fixture's docstring: git
    # transcodes, so the ordinary spelling proves nothing.
    commit_raw_bytes(repo, message=b"PROJ-8: widen the retry window \x96 was too tight\n")

    assert keys_from_blame(str(repo), [_node("sa", file="mod.py", line_start=1)],
                           _PATTERN) == {"sa": "PROJ-8"}
