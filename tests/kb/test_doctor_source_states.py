"""doctor's per-source line: three answers, three marks, one exit code.

`True if reachable else None` gave a broken source, a source of a type nothing
can dial, and a source switched off in the config the same ⚠ mark. That is an
absent check rendering as an answer, which is this workspace's own recorded
failure shape. These tests pin the marks apart, and pin the exit code together.

Precondition for every test here: the autouse `_isolated_home` fixture
(tests/conftest.py) redirects HOME, and `GLOBAL_CONFIG` is the literal
"~/.contextlake/kb.toml" expanded at call time inside `load_kb_config`
(kb/config.py), so the developer's real user config is never merged in. Without
that the source rows below would be a function of the machine running them.
"""

from __future__ import annotations

import pytest

from contextlake import style
from contextlake.cli import main


def _run(argv) -> int:
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def _mark(out: str, label: str) -> str:
    """The first non-space character of the line carrying ``label``."""
    for line in style.strip_ansi(out).splitlines():
        if label in line:
            return line.strip()[0]
    raise AssertionError(f"no line mentions {label!r}; the fixture never produced it")


def _three_state_config(tmp_path):
    """A config with one source of each answer, all names and paths synthetic.

    - broken-notes: a `files` source pointed at a path that does not exist, so
      it is probed and fails.
    - fleet-mirror: a `gitlab` source, in `known_source_types()` and absent from
      `_PROBED_TYPES`, so nothing dials it.
    - good-notes: a `files` source pointed at a directory holding one file that
      matches the default globs, so it is probed and answers.
    """
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.md").write_text("# a\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '[[sources]]\ntype = "files"\nname = "broken-notes"\n'
        f'path = "{tmp_path / "absent"}"\n'
        '[[sources]]\ntype = "gitlab"\nname = "fleet-mirror"\n'
        f'[[sources]]\ntype = "files"\nname = "good-notes"\npath = "{good}"\n'
    )
    return cfg


def test_doctor_marks_a_probed_failure_apart_from_an_unprobed_type(tmp_path, capsys):
    cfg = _three_state_config(tmp_path)
    _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert out.strip(), "the capture must see something or this proves nothing"

    # The positive row first: a source that IS reachable still reads as one. A
    # change that swept every source into ⚠/⊘ would satisfy both rows below.
    assert _mark(out, "good-notes (files)") == "✓"

    assert _mark(out, "broken-notes (files)") == "⚠", (
        "doctor dialled this source and got no answer; that is a warning")
    assert _mark(out, "fleet-mirror (gitlab)") == "⊘", (
        "nothing dialled this source at all; it must not read as a probe that failed")
    assert _mark(out, "broken-notes (files)") != _mark(out, "fleet-mirror (gitlab)"), (
        "a broken source and a source with no probe collapsed into one mark, so "
        "'I tried and it failed' and 'I never tried' read the same")


def test_doctor_does_not_probe_a_disabled_source(tmp_path, capsys, monkeypatch):
    """`kb connect` and `kb ingest` both skip disabled sources; doctor dialled them."""
    from contextlake.kb import source_cmd

    live = tmp_path / "live"
    live.mkdir()
    (live / "a.md").write_text("# a\n")
    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        f'[[sources]]\ntype = "files"\nname = "live-notes"\npath = "{live}"\n'
        '[[sources]]\ntype = "files"\nname = "parked-notes"\nenabled = false\n'
        f'path = "{tmp_path / "absent"}"\n'
    )
    probed: list[str] = []
    real = source_cmd.verify_source

    def spy(src, timeout=None):
        probed.append(src.name)
        return real(src, timeout=timeout)

    # Patched on source_cmd, not on commands: doctor imports the name lazily at
    # call time (see the comment in test_kb_commands.py's reachability test).
    monkeypatch.setattr(source_cmd, "verify_source", spy)
    _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out

    # One assertion, both directions: the disabled source was not dialled AND the
    # enabled one was. A change that skipped every source would fail this row.
    assert probed == ["live-notes"]
    assert _mark(out, "live-notes (files)") == "✓"
    assert _mark(out, "parked-notes (files)") == "⊘"
    assert "disabled" in style.strip_ansi(out).split("parked-notes (files)")[1].splitlines()[0]


class _FakeHeaders:
    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    """The three things `sources/web.py` asks of a urlopen result."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.headers = _FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def test_doctor_does_not_tick_a_web_source_that_read_nothing(tmp_path, capsys,
                                                             monkeypatch):
    """The same defect one branch over, at the surface an operator reads.

    `verify_source` returned True for a `web`/`api`/`graphql` probe that matched
    nothing and dialled nothing, so doctor drew the green ✓ it draws for a source
    that answered. The transport is stubbed rather than dialled: both rows go
    through `sources/web.py` and neither touches the network.
    """
    pages = {"http://docs.example.net/ok":
             "<html><head><title>H</title></head><body><p>hello</p></body></html>",
             "http://docs.example.net/empty":
             "<html><head><title>H</title></head><body></body></html>"}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResponse(pages[req.full_url]))

    cfg = tmp_path / "kb.toml"
    cfg.write_text(
        f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
        '[[sources]]\ntype = "web"\nname = "answering-docs"\n'
        'url = "http://docs.example.net/ok"\n'
        '[[sources]]\ntype = "web"\nname = "silent-docs"\n'
        'url = "http://docs.example.net/empty"\n'
    )
    code = _run(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out

    # The positive row first: a source that answered still reads as one.
    assert _mark(out, "answering-docs (web)") == "✓"
    assert _mark(out, "silent-docs (web)") != "✓", (
        "a source that read no document drew the mark of one that answered")
    assert _mark(out, "silent-docs (web)") == "⚠"
    # AC4 for the state this change creates, not only for the files fixture:
    # the mark moved and the verdict did not.
    assert code == 0, "a source that read nothing must not fail doctor's verdict"
    assert "Problems found" not in style.strip_ansi(out)


def test_doctor_source_states_do_not_change_the_exit_code(tmp_path, capsys):
    """AC4: this story changes the mark, not the verdict."""
    cfg = _three_state_config(tmp_path)
    code = _run(["doctor", "--config", str(cfg)])
    out = style.strip_ansi(capsys.readouterr().out)
    assert code == 0, "no source state may fail doctor's verdict"
    assert "Problems found" not in out

    # The control that makes the run above mean something: doctor CAN exit 1 in
    # this fixture shape, so exit 0 is a fact about the source states rather than
    # about a doctor that can never fail.
    broken = tmp_path / "broken.toml"
    broken.write_text("[kb]\nstore_dir\n")
    code = _run(["doctor", "--config", str(broken)])
    out = style.strip_ansi(capsys.readouterr().out)
    assert code == 1
    assert "Problems found" in out
