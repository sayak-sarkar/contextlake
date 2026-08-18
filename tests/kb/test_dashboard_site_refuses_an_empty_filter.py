"""`kb dashboard --site --repos <matches nothing>` printed a tick over an empty export.

The sibling surface left behind when `kb graph --site` was fixed: same flag, same store,
same matcher, and it still wrote an overview with zero repo pages, logged that zero, and
printed a success line over it with exit 0. A pair of neighbouring commands goes wrong the
same way a read/write pair does -- one side is corrected and looks complete.
"""

from __future__ import annotations

import pytest

from contextlake.cli import main


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def _indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    repo = tmp_path / "svc-billing"
    repo.mkdir()
    (repo / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    assert _run(["kb", "index", str(repo), "--config", str(cfg)]) == 0
    return cfg


def test_a_filter_matching_nothing_is_refused(tmp_path, monkeypatch, capsys):
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    out = tmp_path / "dash"
    code = _run(["kb", "dashboard", "--site", str(out), "--repos", "no-such-repo",
                 "--config", str(cfg)])
    printed = "".join(capsys.readouterr())
    assert printed.strip(), "the capture must see something or this proves nothing"
    assert code == 1
    assert "no-such-repo" in printed
    assert not (out / "index.html").exists(), "a refused export must write nothing"


def test_a_filter_that_matches_still_exports(tmp_path, monkeypatch, capsys):
    """The guard must reject the empty filter, not every filter."""
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    out = tmp_path / "dash-ok"
    assert _run(["kb", "dashboard", "--site", str(out), "--repos", "svc-billing",
                 "--config", str(cfg)]) == 0
    assert (out / "index.html").exists()


def test_an_unfiltered_export_still_works(tmp_path, monkeypatch, capsys):
    cfg = _indexed(tmp_path, monkeypatch)
    capsys.readouterr()
    out = tmp_path / "dash-all"
    assert _run(["kb", "dashboard", "--site", str(out), "--config", str(cfg)]) == 0
    assert (out / "index.html").exists()
