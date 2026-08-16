"""`[kb] anonymize`, the explicit setting that decides whether identities are shown.

The standing intent was "default it on when the store holds repos the operator does not
own", and nothing in the data model can answer that: a `Repo` records id, path, host,
branch and commit, and no ownership at all. The most obvious substitute signal, whether a
repo id sits inside the configured mirror group, INVERTS on the case that motivated the
rule, since mirroring an organisation you contribute to but do not own puts every repo
inside the group. So the operator states their own answer once and no inference can be
wrong.

Three properties are pinned here, and the third is a trust boundary rather than a
preference.
"""

from __future__ import annotations

import logging

import pytest

from contextlake.kb.config import load_kb_config


def _write(path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _cfg(tmp_path, monkeypatch, body: str | None, *, named: bool = True):
    """Load a config, either as the file the user NAMED (`--config`) or as one found by
    walking up from the current directory. The difference is the whole point of the
    third test: same bytes, different provenance, different answer."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    if body is None:
        return load_kb_config(None)
    if named:
        p = tmp_path / "named.toml"
        _write(p, body)
        return load_kb_config(str(p))
    _write(tmp_path / ".contextlake.kb.toml", body)
    return load_kb_config(None)


def test_the_default_is_never(tmp_path, monkeypatch):
    """Nothing changes for anybody who does not opt in. Stated as a test because the
    alternative reading of the same decision was a default that flips ON by inference,
    and a silent change of that kind is exactly what was rejected."""
    assert _cfg(tmp_path, monkeypatch, None).anonymize == "never"


@pytest.mark.parametrize("written,expected", [
    ('[kb]\nanonymize = "always"\n', "always"),
    ('[kb]\nanonymize = "never"\n', "never"),
    # Case and surrounding space are the operator's typing, not their intent.
    ('[kb]\nanonymize = "ALWAYS"\n', "always"),
    ('[kb]\nanonymize = " never "\n', "never"),
])
def test_the_setting_is_read(tmp_path, monkeypatch, written, expected):
    assert _cfg(tmp_path, monkeypatch, written).anonymize == expected


def test_an_unreadable_value_fails_SAFE(tmp_path, monkeypatch, caplog):
    """A typo resolves to "always", NOT to the "never" default.

    The inverse of how every other unknown value in this file is treated, and deliberate:
    this one guards identities, so a misspelling that quietly showed them would be found
    by the person whose name was on the screen rather than by a test.
    """
    with caplog.at_level(logging.WARNING, logger="contextlake"):
        cfg = _cfg(tmp_path, monkeypatch, '[kb]\nanonymize = "alway"\n')
    assert cfg.anonymize == "always"
    assert caplog.text.strip(), "the capture saw nothing, so the assertion below is vacuous"
    assert "alway" in caplog.text, (
        "the warning must quote the value's own spelling, or the reader cannot see the typo")


def test_a_directory_discovered_config_may_turn_it_ON(tmp_path, monkeypatch):
    """Directory-scoped config is a feature, and STRENGTHENING is always allowed."""
    cfg = _cfg(tmp_path, monkeypatch, '[kb]\nanonymize = "always"\n', named=False)
    assert cfg.anonymize == "always"


def test_a_directory_discovered_config_may_NOT_turn_it_OFF(tmp_path, monkeypatch, caplog):
    """The trust boundary. Same bytes as a named config, different provenance.

    `load_kb_config` finds `.contextlake.kb.toml` by walking cwd upward, and contextlake
    clones repositories into the workspace itself, so a checkout can plant this file. A
    checkout that ships `anonymize = "never"` would turn off the operator's own privacy
    setting for any dashboard they serve while sitting in that directory. Only the
    weakening direction is refused, so honest directory-scoped config keeps working.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write(tmp_path / "home" / ".contextlake" / "kb.toml",
           '[kb]\nanonymize = "always"\n')
    with caplog.at_level(logging.WARNING, logger="contextlake"):
        cfg = _cfg(tmp_path, monkeypatch, '[kb]\nanonymize = "never"\n', named=False)
    assert cfg.anonymize == "always", (
        "a discovered config overrode the operator's own setting and turned "
        "anonymising off")
    assert "anonymize" in caplog.text, "the refusal must be reported, never silent"
    assert "never, but" not in caplog.text.lower()


def test_the_same_bytes_are_honoured_from_a_named_config(tmp_path, monkeypatch):
    """The other half of the pair. Without this, the test above would also pass on a
    build that ignored `anonymize = "never"` from EVERY file, including the user's own,
    which would be a different bug wearing the same green tick."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write(tmp_path / "home" / ".contextlake" / "kb.toml",
           '[kb]\nanonymize = "always"\n')
    cfg = _cfg(tmp_path, monkeypatch, '[kb]\nanonymize = "never"\n', named=True)
    assert cfg.anonymize == "never"
