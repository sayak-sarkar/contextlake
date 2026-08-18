"""`kb source add --type <typo>` used to be written out and confirmed with a checkmark.

`--type` is an open set because a plugin registers its own name, but "open" means
"whatever is installed", and at add time that is exactly enumerable. An unrecognised
type was written to the config, confirmed, and then told to "run `contextlake kb
ingest` to pull it in" -- an instruction that can never do anything, because
`_pipeline_for` routes every unknown type to ingest and ingest has no class to build.
"""

from __future__ import annotations

import types

import pytest

from contextlake.kb import source_cmd


def _args(tmp_path, **kw):
    base = {"type": None, "name": None, "config": str(tmp_path / "kb.toml"),
            "local": False, "set": None, "from_stdin": None, "url": None,
            "for_repo": None, "mcp": None, "token_env": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_an_unknown_type_is_refused_and_nothing_is_written(tmp_path):
    cfg = tmp_path / "kb.toml"
    rc = source_cmd.cmd_source_add(_args(tmp_path, type="totally-bogus-type", name="bad"))
    assert rc == 2
    assert not cfg.exists(), "a refused add must not leave a config entry behind"


def test_the_refusal_names_what_this_build_can_actually_run(tmp_path, caplog):
    """Captured at the logging seam, not on stdout.

    The first version of this test read `capsys` and saw an empty string, because the
    CLI writes through `logging_setup.log`. It would have passed on a build that
    printed nothing at all, which is why the emptiness assertion below stays.
    """
    with caplog.at_level("INFO", logger="contextlake"):
        source_cmd.cmd_source_add(_args(tmp_path, type="totally-bogus-type", name="bad"))
    text = caplog.text
    assert text.strip(), "the capture must see something, or this test proves nothing"
    assert "totally-bogus-type" in text
    for known in ("files", "web"):
        assert known in text, "the message must list the types that DO work"


@pytest.mark.parametrize("good", ["files", "web", "atlassian"])
def test_every_known_type_is_still_accepted(tmp_path, good):
    """The guard must reject the unrunnable, not narrow the supported set."""
    rc = source_cmd.cmd_source_add(_args(tmp_path, type=good, name=f"s-{good}"))
    assert rc == 0


def test_known_source_types_covers_both_pipelines():
    known = set(source_cmd.known_source_types())
    assert {"files", "web"} <= known, "built-in ingest sources"
    assert {"atlassian", "figma", "gitlab", "slack"} <= known, "connector sources"
