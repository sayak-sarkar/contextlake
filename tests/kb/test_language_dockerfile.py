"""Dockerfiles, and the first grammar contextlake does not install for you.

Two independent things are pinned here.

**Extraction.** A build stage is the referenceable name in a Dockerfile, and telling a
stage from an external image needs a decision the grammar does not make: in
`FROM builder AS test` and `FROM nginx:alpine` the base is the same node type, and only
one of them is an image anybody pulls.

**The skip.** `tree-sitter-dockerfile` publishes two wheels and no sdist, so it cannot be
a hard dependency without breaking `pip install` on every platform it has no wheel for.
That makes "the grammar is not installed" a state real users reach, and the machine that
reports it is unreachable on any machine that HAS the wheel, including this one. So it is
tested by injecting the import failure. That is the same trap a doctor test walked into
earlier in this project, where it passed only because the dev machine happened to have an
optional package installed.
"""

from __future__ import annotations

import importlib

import pytest

from contextlake.kb import parse
from contextlake.kb.parse import (
    OPTIONAL_GRAMMAR_EXTRA,
    GrammarNotInstalled,
    index_repo_dir,
    parse_source,
)

DOCKERFILE = b"""FROM node:20 AS builder
WORKDIR /app
RUN npm ci && npm run build

FROM builder AS test
RUN npm test

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
"""


def _syms(src: bytes = DOCKERFILE, fn: str = "Dockerfile") -> set[tuple[str, str]]:
    nodes, _e, _c, _i = parse_source("r", fn, src, "dockerfile")
    return {(n.kind, n.name) for n in nodes if n.kind != "file"}


# --- extraction ---------------------------------------------------------------------


def test_build_stages_become_their_own_kind():
    got = _syms()
    assert ("dockerfile_stage", "builder") in got
    assert ("dockerfile_stage", "test") in got


def test_an_external_base_image_becomes_a_module():
    """The same kind an `import` target takes everywhere else: what this file depends on
    and does not contain."""
    got = _syms()
    assert ("module", "node") in got
    assert ("module", "nginx") in got


def test_a_stage_used_as_a_base_is_not_reported_as_an_external_image():
    """`FROM builder AS test` builds on a stage declared in the same file.

    Emitting `builder` as a module would put a dependency on a container image nobody
    pulls into the graph, and it would look exactly like the real ones beside it. This is
    the assertion the two-pass walk in `_dockerfile_symbols` exists for: a single pass
    cannot know about a stage it has not reached yet.
    """
    got = _syms()
    assert ("module", "builder") not in got
    assert ("module", "test") not in got


def test_a_tag_is_not_part_of_the_name():
    """`node:20` is one image at one version. The version belongs on the edge or nowhere;
    a node named `node:20` would not match `node:22` in the next repo, and the fleet-wide
    question is which repos build on node at all."""
    assert "node:20" not in {n for _k, n in _syms()}
    assert "20" not in {n for _k, n in _syms()}


def test_a_single_stage_dockerfile_still_yields_its_base():
    got = _syms(b"FROM python:3.12-slim\nCOPY . /app\n")
    assert ("module", "python") in got
    assert not [n for k, n in got if k == "dockerfile_stage"]


# --- the optional grammar -----------------------------------------------------------


@pytest.fixture
def dockerfile_grammar_absent(monkeypatch):
    """Make `import tree_sitter_dockerfile` fail, for this test only.

    Clearing the three caches matters as much as the patch: `_language` returns early
    from `_LANGS` when another test in the same process has already loaded the grammar,
    and the patched import would never be reached. A test that silently exercises the
    cached happy path is indistinguishable from one that passes.
    """
    real = importlib.import_module

    def fake(name, *a, **kw):
        if name == "tree_sitter_dockerfile":
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **kw)

    for cache in (parse._LANGS, parse._TS_PARSERS, parse._COMPILED):
        cache.pop("dockerfile", None)
    monkeypatch.setattr(parse.importlib, "import_module", fake)
    yield
    for cache in (parse._LANGS, parse._TS_PARSERS, parse._COMPILED):
        cache.pop("dockerfile", None)


def test_the_fixture_actually_breaks_the_import(dockerfile_grammar_absent):
    """Proves the injection works before anything is concluded from its silence."""
    with pytest.raises(GrammarNotInstalled) as exc:
        parse._language("dockerfile")
    assert exc.value.lang == "dockerfile"
    assert exc.value.extra == "kb-dockerfile"


def test_a_hard_dependency_still_raises_the_partial_install_error(monkeypatch):
    """The other branch. A missing REQUIRED grammar is a broken install, and telling that
    user to install an optional extra would send them somewhere with no fix in it."""
    real = importlib.import_module

    def fake(name, *a, **kw):
        if name == "tree_sitter_python":
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **kw)

    for cache in (parse._LANGS, parse._TS_PARSERS, parse._COMPILED):
        cache.pop("python", None)
    monkeypatch.setattr(parse.importlib, "import_module", fake)
    try:
        with pytest.raises(ImportError) as exc:
            parse._language("python")
        assert not isinstance(exc.value, GrammarNotInstalled)
        assert "contextlake[kb]" in str(exc.value)
    finally:
        for cache in (parse._LANGS, parse._TS_PARSERS, parse._COMPILED):
            cache.pop("python", None)


def test_the_skip_is_counted_and_names_the_extra(tmp_path, monkeypatch,
                                                 dockerfile_grammar_absent):
    """The whole point of the machinery. Before it, a missing optional grammar fell into
    the blanket per-file handler, which logged `parse error` once per file and
    incremented no counter, so the run reported the user's file as broken and the summary
    reported nothing at all.
    """
    (tmp_path / "Dockerfile").write_bytes(DOCKERFILE)
    (tmp_path / "Dockerfile.dev").write_bytes(DOCKERFILE)
    (tmp_path / "app.py").write_bytes(b"def go():\n    return 1\n")

    said: list[str] = []
    monkeypatch.setattr(parse, "log", lambda msg, **kw: said.append(str(msg)))
    shard = index_repo_dir(str(tmp_path), "r")
    out = "\n".join(said)

    assert "2 dockerfile file(s) skipped" in out, out
    assert "contextlake[kb-dockerfile]" in out, out
    # NOT the other sentence: the language is supported, so telling the user it has no
    # parser would point them at docs/contributing-languages.md, which cannot help.
    assert "no parser for their type" not in out
    assert "parse error" not in out
    # The rest of the repo still indexes. A missing optional grammar is not a failed run.
    assert "app.py" in {n.name for n in shard.nodes if n.kind == "file"}


def test_the_message_is_silent_when_every_grammar_is_present(tmp_path, monkeypatch):
    """Guards against the skip line becoming boilerplate on runs where nothing was
    skipped, which is what would make people stop reading it."""
    (tmp_path / "Dockerfile").write_bytes(DOCKERFILE)
    said: list[str] = []
    monkeypatch.setattr(parse, "log", lambda msg, **kw: said.append(str(msg)))
    index_repo_dir(str(tmp_path), "r")
    assert "kb-dockerfile" not in "\n".join(said)


def test_every_optional_grammar_has_an_extra_that_exists(tmp_path):
    """A named extra that pyproject does not define sends the user to a command that
    fails, which is worse than saying nothing."""
    import tomllib

    from contextlake.kb.parse import _GRAMMARS

    repo = __import__("pathlib").Path(__file__).resolve().parents[2]
    data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(data["project"]["optional-dependencies"])
    for lang, extra in OPTIONAL_GRAMMAR_EXTRA.items():
        assert lang in _GRAMMARS, f"{lang} is marked optional but has no grammar row"
        assert extra in declared, (
            f"the {lang} skip message tells users to install contextlake[{extra}], and "
            f"pyproject.toml defines no such extra")
