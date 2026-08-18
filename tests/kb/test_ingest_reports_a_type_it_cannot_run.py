"""An enabled source whose type this build cannot construct must not vanish silently.

`kb source add` refuses an unknown type at write time now, and that does nothing for a
config written before this release, edited by hand, or authored ahead of installing the
plugin it names. The ingest read path filtered those rows out with `if s.type in
registry`, so a run configured to read three sources could read one and still print a
checkmark. This is the same defect on the other side of a read/write pair: fixing the
writer looks complete and matches nothing already on disk.
"""

from __future__ import annotations

import pytest

from contextlake.cli import main


def _printed(capsys) -> str:
    """The CLI logs through a stdout handler, so `capsys` is the seam, not `caplog`.

    An earlier version of this file read `caplog` and saw an empty string while the
    command printed three correct lines. It would have passed against a build that
    printed nothing at all, which is why every assertion below is preceded by a check
    that the capture saw anything. `readouterr()` also CONSUMES, so it is called once
    per test and the result reused.
    """
    out = capsys.readouterr()
    return out.out + out.err


def _config(tmp_path, body: str):
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n' + body)
    return cfg


def test_a_config_of_only_unrunnable_sources_fails_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _config(tmp_path, '\n[[sources]]\nname = "wiki"\ntype = "not-a-real-type"\n')
    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])
    printed = _printed(capsys)
    assert e.value.code == 1
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert "not-a-real-type" in printed
    assert "wiki" in printed, "the source's NAME is what the user has to go edit"


def test_a_runnable_source_alongside_an_unrunnable_one_is_still_incomplete(
        tmp_path, monkeypatch, capsys):
    """The dangerous shape: something worked, so the run used to look clean."""
    monkeypatch.setenv("HOME", str(tmp_path))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n\ncharge_card runs first.\n")
    cfg = _config(tmp_path, (
        f'\n[[sources]]\nname = "notes"\ntype = "files"\npath = "{docs}"\n'
        '\n[[sources]]\nname = "wiki"\ntype = "not-a-real-type"\n'))
    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])
    printed = _printed(capsys)
    assert e.value.code != 0, "one source could not run, so the run is not complete"
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert "incomplete" in printed.lower()
    assert "not-a-real-type" in printed


def test_a_disabled_unrunnable_source_is_not_reported(tmp_path, monkeypatch, capsys):
    """Turning a source off is how a user parks it. That must stay quiet."""
    monkeypatch.setenv("HOME", str(tmp_path))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n\nbody\n")
    cfg = _config(tmp_path, (
        f'\n[[sources]]\nname = "notes"\ntype = "files"\npath = "{docs}"\n'
        '\n[[sources]]\nname = "wiki"\ntype = "not-a-real-type"\nenabled = false\n'))
    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])
    printed = _printed(capsys)
    assert e.value.code == 0
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert "not-a-real-type" not in printed


def test_no_sources_at_all_still_says_so_rather_than_failing(tmp_path, monkeypatch, capsys):
    """"Nothing configured" and "nothing could run" are different events."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _config(tmp_path, "")
    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--config", str(cfg)])
    printed = _printed(capsys)
    assert e.value.code == 0
    assert "No document sources" in printed
