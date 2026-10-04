"""Counts and worked examples in the connector docs, checked against the build.

Three defects shipped in `docs/connecting-and-enriching.md` and the README:

- It taught `kb source add [--name NAME]`. The name is positional, so the flag exits 2.
- It taught `--from-stdin token` as the way to keep a secret out of shell history. `token` is a
  literal-credential key, so `add` refuses it with exit 2. A reader who followed the page failed at
  the one step that mattered. The working form is `--set token_env=MY_TOKEN`, which stores the NAME
  of an environment variable.
- It said "Four connectors ship" and "the nine source types" where the build ships five and ten.

`tests/test_docs_commands_parse.py` parses every command a page shows. It cannot see the
`--from-stdin` mistake, because `--from-stdin token` parses and is refused at run time. So this file
runs the examples the page prints.

Lives in `tests/kb/` because `kb source` needs the knowledge-layer extra (tomlkit).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from contextlake.kb.source_cmd import _CONNECT_TYPES, _reject_literal_secrets
from contextlake.kb.sources.base import _builtin_sources

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}

# A dummy value, not a secret. It is a URL because `mcp` is the option the page pipes in.
_DUMMY_MCP_URL = "https://mcp.example.test/v1/mcp"


def _as_int(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _flat(path: Path) -> str:
    """The page with each run of whitespace collapsed, so a wrapped command reads as one line."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


# --- counts ----------------------------------------------------------------------------

def test_the_connector_count_and_list_match_the_build() -> None:
    page = DOCS / "connecting-and-enriching.md"
    flat = _flat(page)
    stated = _as_int(re.search(r"(\w+) connectors ship", flat).group(1))
    assert stated == len(_CONNECT_TYPES)
    offered = _as_int(re.search(r"the (\w+) connectors \(", flat).group(1))
    assert offered == len(_CONNECT_TYPES)
    section = page.read_text(encoding="utf-8").split("## Connectors", 1)[1].split("\n## ", 1)[0]
    listed = {m.lower() for m in re.findall(r"^- \*\*(\w+)\*\*:", section, re.M)}
    assert listed == _CONNECT_TYPES, "the connector bullets are not the connector types"


def test_the_readme_source_type_count_matches_the_build() -> None:
    stated = _as_int(re.search(r"the (\w+) source types", _flat(REPO / "README.md")).group(1))
    assert stated == len(_CONNECT_TYPES | set(_builtin_sources()))


# --- the worked examples ---------------------------------------------------------------

def _doc_pages() -> list[Path]:
    return [*sorted(DOCS.rglob("*.md")), REPO / "README.md", REPO / "SECURITY.md"]


def test_no_page_feeds_a_credential_key_to_set_or_from_stdin() -> None:
    """`--from-stdin token` and `--set token=...` are refused at run time. No page may teach one."""
    bad = []
    for page in _doc_pages():
        text = page.read_text(encoding="utf-8")
        for key in re.findall(r"--from-stdin\s+(\w+)", text) + re.findall(r"--set\s+(\w+)=", text):
            if _reject_literal_secrets({key: None}):
                bad.append(f"{page.relative_to(REPO)}: {key}")
    assert not bad, f"pages teach a credential key that `kb source add` refuses: {bad}"


def _run(tmp_path: Path, argv: list[str], stdin: str = "") -> tuple[int, Path]:
    config = tmp_path / "kb.toml"
    config.unlink(missing_ok=True)
    env = {**os.environ, "HOME": str(tmp_path), "NO_COLOR": "1"}
    env.pop("CONTEXTLAKE_NO_LOCAL_CONFIG", None)
    proc = subprocess.run([sys.executable, "-m", "contextlake", *argv, "--config", str(config)],
                          input=stdin, capture_output=True, text=True, env=env, cwd=tmp_path,
                          timeout=120)
    return proc.returncode, config


def test_the_from_stdin_example_runs_and_writes_the_value(tmp_path: Path) -> None:
    """The command the page prints, run as printed with a dummy value on stdin."""
    m = re.search(r"printf '%s' \"\$\w+\" \| contextlake (kb source add [^`]+?--from-stdin \w+)",
                  _flat(DOCS / "connecting-and-enriching.md"))
    assert m, "the page no longer prints a `printf ... | contextlake kb source add` example"
    rc, config = _run(tmp_path, m.group(1).split(), stdin=_DUMMY_MCP_URL)
    assert rc == 0
    assert f'mcp = "{_DUMMY_MCP_URL}"' in config.read_text(encoding="utf-8")


def test_the_token_env_example_runs_and_stores_only_the_name(tmp_path: Path) -> None:
    m = re.search(r"--set (token_env=\w+)", _flat(DOCS / "connecting-and-enriching.md"))
    assert m, "the page no longer shows the `--set token_env=NAME` form"
    argv = ["kb", "source", "add", "jira", "--type", "atlassian", "--set", m.group(1)]
    rc, config = _run(tmp_path, argv)
    assert rc == 0
    assert re.search(r'^token_env = "\w+"$', config.read_text(encoding="utf-8"), re.M)


def test_the_positional_name_form_runs(tmp_path: Path) -> None:
    flat = _flat(DOCS / "connecting-and-enriching.md")
    m = re.search(r"\(`contextlake (kb source add \w+ --type \w+)`\)", flat)
    assert m, "the page no longer prints the positional-name example"
    rc, config = _run(tmp_path, m.group(1).split())
    assert rc == 0
    assert 'name = "jira"' in config.read_text(encoding="utf-8")


def test_a_literal_credential_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """The page promises exit 2 and no write, for both ways of passing a credential key."""
    base = ["kb", "source", "add", "jira", "--type", "atlassian"]
    rc_set, config = _run(tmp_path, [*base, "--set", "token=dummy-not-a-secret"])
    assert rc_set == 2 and not config.exists()
    rc_stdin, config = _run(tmp_path, [*base, "--from-stdin", "api_key"],
                            stdin="dummy-not-a-secret")
    assert rc_stdin == 2 and not config.exists()
