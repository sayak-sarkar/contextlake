"""Group C of the honesty batch: three silent drops, each now named.

* An unsupported language vanished with no counter. A Swift, Dart or Vue tree indexed to
  `0 nodes, 0 edges`, exit 0, and reported `skipped 0 generated, 0 oversized, 0 ignored`
  -- every counter truthfully zero and the reason invisible. It reads as "this repo is
  empty" rather than "this tool cannot read it", which is the worst first impression
  available.
* `kb forget` printed bytes it summed BEFORE an `ignore_errors=True` delete: a prediction
  presented as an outcome.
* An invalid `[[rules]]` regex disabled the rule with no message, and `doctor` then
  confirmed the rule was loaded -- a configured rule that silently matched nothing.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from contextlake.kb.parse import LANG_BY_EXT, index_repo_dir


def _repo(tmp_path, files: dict[str, str]):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, body in files.items():
        (repo / name).write_text(body, encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env,
                       capture_output=True)
    return repo


def _said(monkeypatch, fn):
    out: list[str] = []
    import contextlake.kb.parse as parse_mod
    monkeypatch.setattr(parse_mod, "log",
                        lambda msg, *a, **k: out.append(str(msg)))
    fn()
    return "\n".join(out)


def test_a_repo_in_an_unsupported_language_says_so(tmp_path, monkeypatch):
    """THE LOAD-BEARING ASSERTION: the reason must be in the output, not inferable."""
    # Haskell, because it is genuinely unparsed. This fixture used `.swift` until Swift
    # became supported, at which point the test failed by correctly reporting that the
    # files WERE parsed. Pick an extension for this fixture that nothing is likely to add
    # soon, and expect to move it again if that stops being true.
    repo = _repo(tmp_path, {
        "App.hs": "greet = putStrLn \"hi\"\n",
        "View.hs": "data V = V\n",
        "README.md": "# hi\n",
    })
    said = _said(monkeypatch, lambda: index_repo_dir(str(repo), "demo", head_commit="h"))
    assert "no file in this repository has a supported parser" in said, said
    assert ".hs" in said, "the message must name the extensions it could not read"
    assert "x2" in said, "the message must say how many"


def test_the_language_count_in_that_message_is_derived_not_written_down(tmp_path,
                                                                       monkeypatch):
    """The near-miss that matters most here: a hardcoded '14 languages' goes stale the
    moment a grammar is added, which is the same docs-versus-code drift this release is
    about. The number must come from LANG_BY_EXT.

    It has already earned its keep: adding five grammars moved the real count from 14 to
    19, and this assertion is derived so it followed rather than needing a hunt."""
    repo = _repo(tmp_path, {"App.hs": "greet = putStrLn \"hi\"\n"})
    said = _said(monkeypatch, lambda: index_repo_dir(str(repo), "demo", head_commit="h"))
    expected = len(set(LANG_BY_EXT.values()))
    assert f"{expected} languages" in said, (
        f"expected the derived count {expected}; the message may have a literal in it")


def test_a_supported_repo_says_nothing_about_unsupported_files(tmp_path, monkeypatch):
    """The other near-miss. A normal repo holds READMEs and lockfiles, so if every
    unmatched file triggered the warning it would fire on every run and be ignored."""
    repo = _repo(tmp_path, {"a.py": "def f():\n    return 1\n", "README.md": "# hi\n"})
    said = _said(monkeypatch, lambda: index_repo_dir(str(repo), "demo", head_commit="h"))
    assert "no file in this repository has a supported parser" not in said, said


@pytest.mark.parametrize("pattern", ["[unclosed", "(also bad"])
def test_an_invalid_rules_regex_is_announced(pattern, monkeypatch):
    """It stays skipped -- one bad pattern must not abort the run -- but not silent."""
    import contextlake.kb.references as refs

    said: list[str] = []
    monkeypatch.setattr(refs, "log", lambda msg, *a, **k: said.append(str(msg)))
    assert refs.extract_issue_keys("/tmp", pattern) == []
    joined = "\n".join(said)
    assert "invalid" in joined.lower() and pattern in joined, joined


def test_a_valid_rules_regex_stays_quiet(monkeypatch):
    """Pair: a warning on every correct config is noise, and noise gets filtered."""
    import contextlake.kb.references as refs

    said: list[str] = []
    monkeypatch.setattr(refs, "log", lambda msg, *a, **k: said.append(str(msg)))
    refs.extract_issue_keys("/tmp", r"[A-Z]+-\d+")
    assert not [s for s in said if "invalid" in s.lower()]


def test_forget_measures_reclaimed_bytes_after_the_delete():
    """`reclaim` was summed before an `ignore_errors=True` delete and printed as an
    outcome, so a removal that failed still reported the full figure as freed."""
    import inspect

    from contextlake.kb.cmds import forget

    src = inspect.getsource(forget.cmd_forget)
    i_delete = src.find("shutil.rmtree(p, ignore_errors=True)")
    i_measure = src.find("reclaim = freed")
    assert i_delete > 0 and i_measure > i_delete, (
        "the reclaimed figure is not measured after the delete")
    assert re.search(r"if p\.exists\(\)", src), (
        "nothing checks whether each path actually went away")
