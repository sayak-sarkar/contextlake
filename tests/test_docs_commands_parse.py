"""Every `contextlake ...` command a doc page shows must parse with the real argparse parser.

Why this exists: `docs/connecting-and-enriching.md` taught `kb source add --name NAME` and
`--from-stdin token`, and `docs/code-graph-model.md` taught `--languages c`. All three exit 2.
The `--from-stdin token` form is the documented way to keep a secret out of shell history, so a
reader who followed it failed at the step that matters. `tests/test_cli_examples_parse.py`
covers the examples in `--help` text. Nothing covered the docs.

How it works: pull the command lines out of the doc pages and parse each through
`build_parser().parse_args`. Nothing runs. A flag or a command that does not exist fails.

Two places hold commands:

1. Fenced blocks (bash, sh, shell, zsh, text, unlabeled). A `console` block counts only its
   `$ ` lines, because the rest is captured output.
2. Inline code spans. The two W04 mistakes were in inline spans, not in fenced blocks, so a
   fenced-only scan would not have caught them. A span may wrap over a line break, as long as no
   blank line separates the parts.

What the extractor does with text that is not a literal command:

- `[--flag VALUE]` brackets in an inline span mark an optional part. The brackets are removed and
  the flag is checked, because `[--name NAME]` is where a wrong flag hides.
- `<name>` becomes a dummy value. `<a|b|c>` becomes its first choice, so a `choices=` list is
  checked. A placeholder where the command word belongs (`contextlake <command>`) cannot be
  checked and is skipped.
- A bare upper-case word (`N`, `NAME`, `SOURCE`) is a placeholder too and becomes `1`.
- `...` is dropped.
- An inline span that names a command without its required arguments (`contextlake schedule`)
  is accepted: argparse reports a missing required argument, and a prose mention is not a
  complete invocation. A fenced block gets no such leniency.

A line that is wrong on purpose (a doc that shows the refusal) goes in `_DELIBERATELY_WRONG`
with a reason. An allowlist entry that matches nothing fails the test, so the list cannot rot.
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

from contextlake.cli import _ALIASES, _dry_run_refusal, _resolve_command, build_parser

REPO = Path(__file__).resolve().parents[1]

_FENCE = re.compile(r"^(\s*)(```+|~~~+)\s*([A-Za-z0-9_+-]*)")
_INLINE = re.compile(r"(?<!`)`([^`\n](?:[^`]|\n(?!\s*\n))*?)`(?!`)")
_PLACEHOLDER = re.compile(r"<([A-Za-z_][\w .:/|-]*)>")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_UPPER_WORD = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REQUIRED = re.compile(r"the following arguments are required")
_PLACEHOLDER_CHOICE = re.compile(r"argument -[-\w]+: invalid choice: '1'")
_SHELL_LANGS = {"", "bash", "sh", "shell", "console", "zsh", "text", "txt"}
_NAMESPACES = {"kb", "mirror"}

# (page, command words after `contextlake`, placeholders as written below) -> why it is wrong
# on purpose. `PLACEHOLDER` stands for a `<...>` in the doc.
_DELIBERATELY_WRONG: dict[tuple[str, str], str] = {
    ("docs/cli-reference.md", "--help-advanced"):
        "the page says there is no top-level --help-advanced",
    ("docs/cli-reference.md", "--dry-run kb index"):
        "the page shows that a command without --dry-run refuses it",
    ("docs/cli-reference.md", "kb index --dry-run"):
        "the page shows that a command without --dry-run refuses it",
    ("README.md", "--dry-run kb index"):
        "the README shows that a command without --dry-run refuses it",
    ("README.md", "kb --dry-run index"):
        "the README shows that a command without --dry-run refuses it",
    ("README.md", "kb index --dry-run"):
        "the README shows that a command without --dry-run refuses it",
    ("docs/console-output.md", "fetc"):
        "a typo, to show the did-you-mean suggestion",
    ("docs/console-output.md", "fetch"):
        "the flat spelling removed in v3.0.0, to show the did-you-mean suggestion",
    ("docs/console-output.md", "kb index --work-d /tmp"):
        "a mistyped flag, to show how the error reads",
    ("docs/console-output.md", "kb index --worksapce ."):
        "a misspelt flag, to show the did-you-mean suggestion",
    ("docs/console-output.md", "bootstrap --local"):
        "a flag that belongs to other commands, to show the 'it's used by' message",
    ("docs/console-output.md", "kb dashboard --serve --workspace --open"):
        "a flag with no value, to show the missing-value message",
    ("docs/scheduling.md", "schedule interval 6h run kb wiki --force"):
        "the page shows the failure you get without the `--` separator",
    ("docs/explained.md", "trust PLACEHOLDER"):
        "a command the design notes record as rejected, not as a feature",
}


def _doc_pages() -> list[Path]:
    pages = sorted((REPO / "docs").rglob("*.md"))
    return [*pages, REPO / "README.md", REPO / "SECURITY.md"]


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _fenced_blocks(text: str) -> Iterator[tuple[str, int, list[str]]]:
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, lang = m.group(2), m.group(3).lower()
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith(fence[:3]):
            j += 1
        yield lang, i + 2, lines[i + 1:j]
        i = j + 1


def _prose_only(text: str) -> str:
    """The page with fenced blocks blanked out. Line numbers stay the same."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        fence = m.group(2)
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith(fence[:3]):
            j += 1
        out.extend([""] * (j - i + 1))
        i = j + 1
    return "\n".join(out)


def _logical_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Join `\\` continuations. Yields (offset of the first physical line, joined text)."""
    buf, first = "", None
    for n, raw in enumerate(lines):
        if first is None:
            first = n
        s = raw.rstrip()
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        yield first, buf + s
        buf, first = "", None
    if buf and first is not None:
        yield first, buf


def _substitute_placeholders(line: str) -> str:
    def one(m: re.Match) -> str:
        body = m.group(1)
        if "|" in body:
            return body.split("|")[0].strip()
        return "PLACEHOLDER"
    return _PLACEHOLDER.sub(one, line)


def _shell_segments(line: str) -> list[list[str]] | None:
    """Split a shell line on pipes, `&&`, `;`. A redirect ends its segment. None if unparseable."""
    lex = shlex.shlex(_substitute_placeholders(line), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= set("();<>|&"):
            segments.append(current)
            current = []
            continue
        current.append(tok)
    segments.append(current)
    return segments


def _after_the_program_word(segment: list[str]) -> list[str] | None:
    """The words after `contextlake` when this segment starts the program, else None.

    Handles a leading `NAME=value`, `uvx [--from X] contextlake`, `uv run contextlake`,
    `pipx run contextlake` and `python -m contextlake`.
    """
    k = 0
    while k < len(segment) and _ENV_ASSIGN.match(segment[k]):
        k += 1
    seg = segment[k:]
    if not seg:
        return None
    if seg[0] == "contextlake":
        return seg[1:]
    for prefix in (["uv", "run"], ["pipx", "run"], ["python", "-m"], ["python3", "-m"]):
        if seg[:2] == prefix and seg[2:3] == ["contextlake"]:
            return seg[3:]
    if seg[0] == "uvx" and "contextlake" in seg[1:]:
        return seg[seg.index("contextlake", 1) + 1:]
    return None


def _from_line(line: str) -> Iterator[list[str]]:
    if "contextlake" not in line:
        return
    segments = _shell_segments(line)
    if segments is None:
        # An unbalanced quote means this line was never checked. Report it instead of skipping it.
        # Only a line that opens with the program is a command. Captured output such as
        # "Run 'contextlake x --help' to see x's own flags." is prose with an apostrophe.
        if re.match(r"\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*contextlake\b", line):
            yield ["__UNPARSEABLE__", line.strip()]
        return
    for seg in segments:
        words = _after_the_program_word(seg)
        if words is not None:
            yield words


def extract(path: Path) -> list[tuple[int, str, list[str]]]:
    """Every command in one page: (line number, 'fenced' or 'inline', words after `contextlake`)."""
    text = path.read_text(encoding="utf-8")
    found: list[tuple[int, str, list[str]]] = []
    for lang, start, body in _fenced_blocks(text):
        if lang not in _SHELL_LANGS:
            continue
        for offset, joined in _logical_lines(body):
            stripped = joined.strip()
            if lang == "console":
                if not stripped.startswith("$ "):
                    continue
                stripped = stripped[2:]
            elif stripped.startswith("$ "):
                stripped = stripped[2:]
            for words in _from_line(stripped):
                found.append((start + offset, "fenced", words))
    prose = _prose_only(text)
    for m in _INLINE.finditer(prose):
        span = " ".join(m.group(1).replace("\\|", "|").split())
        if not re.search(r"(^|[\s|&;(])contextlake(\s|$)", span):
            continue
        span = span.replace("[", " ").replace("]", " ")
        line_no = prose.count("\n", 0, m.start()) + 1
        for words in _from_line(span):
            found.append((line_no, "inline", words))
    return found


def _parseable(words: list[str]) -> list[str] | None:
    """The words with placeholders made literal, or None if the command word is a placeholder."""
    words = [w for w in words if w != "..."]
    positions = [i for i, w in enumerate(words) if not w.startswith("-")
                 and not (i and words[i - 1].startswith("-") and w == "PLACEHOLDER")]
    # Where the command word and the verb sit, ignoring flags and the values of flags.
    command_slots = positions[:1]
    if command_slots and words[command_slots[0]] in _NAMESPACES and len(positions) > 1:
        command_slots = positions[:2]
    if any(words[i] == "PLACEHOLDER" for i in command_slots):
        return None
    return ["1" if (w == "PLACEHOLDER" or _UPPER_WORD.match(w)) else w for w in words]


def check(words: list[str], kind: str) -> str | None:
    """None when argparse accepts the words, else the first line of its complaint."""
    argv = _parseable(words)
    if argv is None:
        return None
    parser = build_parser()
    err, out = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return None
        message = (err.getvalue() or out.getvalue()).strip()
        if kind == "inline" and _REQUIRED.search(message):
            return None
        # `--format <fmt>`: the placeholder stands for one of the choices, and the flag exists.
        if _PLACEHOLDER_CHOICE.search(message):
            return None
        lines = [ln for ln in message.splitlines() if ln.strip()]
        return next((ln for ln in lines if "error" in ln or "Unknown" in ln or "✗" in ln),
                    lines[-1] if lines else "exit 2")
    # A flag that parses can still be refused afterwards (`--dry-run` on a command without it).
    # `_run` does this in the same order: resolve the namespace, then ask for a refusal.
    if args.command is None:
        return None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _resolve_command(args, parser)
            args.command = _ALIASES.get(args.command, args.command)
            refusal = _dry_run_refusal(parser, args)
    except SystemExit:
        return None
    return refusal.strip().splitlines()[0] if refusal else None


def _key(words: list[str]) -> str:
    return " ".join(w for w in words if w != "...")


def _problems() -> tuple[list[str], set[tuple[str, str]], int]:
    bad: list[str] = []
    allowlist_hits: set[tuple[str, str]] = set()
    total = 0
    for page in _doc_pages():
        rel = _relative(page)
        for line_no, kind, words in extract(page):
            total += 1
            if words and words[0] == "__UNPARSEABLE__":
                bad.append(f"{rel}:{line_no}: could not tokenise: {words[1]!r}")
                continue
            complaint = check(words, kind)
            if complaint is None:
                continue
            key = (rel, _key(words))
            if key in _DELIBERATELY_WRONG:
                allowlist_hits.add(key)
                continue
            bad.append(f"{rel}:{line_no} [{kind}] contextlake {_key(words)}\n      {complaint}")
    return bad, allowlist_hits, total


def test_every_command_in_the_docs_parses() -> None:
    bad, _hits, total = _problems()
    # Pinned near the measured surface so a broken extractor cannot pass by finding nothing.
    # Measured 2026-10-04: 367 commands, 147 in fenced blocks and 220 in inline spans.
    assert total >= 350, f"only {total} commands extracted from the docs, the scanner went blind"
    assert not bad, "the docs show commands the parser refuses:\n  " + "\n  ".join(bad)


def test_the_allowlist_has_no_stale_entries() -> None:
    _bad, hits, _total = _problems()
    stale = sorted(set(_DELIBERATELY_WRONG) - hits)
    assert not stale, (
        "these allowlist entries no longer match a refused doc command, so delete them:\n  "
        + "\n  ".join(f"{p}: {c}" for p, c in stale))


# --- a flag named on its own ---------------------------------------------------------------
#
# `--languages c` sat in a sentence with no `contextlake` word next to it, so the command scan
# above could not see it. Any `--flag` in an inline code span must exist on at least one
# contextlake command, unless it belongs to another tool or the page says it does not exist.

_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
_OTHER_PROGRAMS = {"git", "pip", "pip3", "python", "python3", "pytest", "gh", "docker", "systemctl",
                   "wsl", "uv", "uvx", "pipx", "glab", "cosign", "curl", "npm", "claude", "ruff",
                   "COPY", "sudo", "make", "pypi-attestations"}
# flag -> why it is not a contextlake flag
_NOT_A_CONTEXTLAKE_FLAG: dict[str, str] = {
    "--break-system-packages": "pip",
    "--collector": "a node_exporter flag (--collector.textfile.directory)",
    "--discussed": "the edge label `--discussed_in-->`, not a flag",
    "--documented": "the edge label `--documented_by-->`, not a flag",
    "--extra-index-url": "pip",
    "--insecure": "named as something not to pass to a TLS client",
    "--no-": "the prefix of the `--no-` counterpart flags, a pattern and not a flag",
    "--no-anonymize": "the page says it does not exist",
    "--only-binary": "pip",
    "--repos-exact": "the page says it was removed",
    "--stdio": "a flag of an MCP server the config launches",
    "--tag": "a flag of the release-verification script, not of contextlake",
    "--target": "docker build --target",
    "--upgrade": "pip",
    "--work-d": "a mistyped flag, to show how the error reads",
}


def _known_flags() -> set[str]:
    parser = build_parser()
    known = set(parser._option_string_actions)
    for sub in parser._all_parsers.values():
        known |= set(sub._option_string_actions)
    return known


def _bare_flag_problems() -> tuple[list[str], set[str]]:
    known = _known_flags()
    bad: list[str] = []
    used: set[str] = set()
    for page in _doc_pages():
        prose = _prose_only(page.read_text(encoding="utf-8"))
        for m in _INLINE.finditer(prose):
            span = " ".join(m.group(1).split())
            names_contextlake = re.search(r"(^|[\s|&;(])contextlake(\s|$)", span)
            if names_contextlake or span.split()[0] in _OTHER_PROGRAMS:
                continue
            for flag in _FLAG.findall(span):
                if flag in known:
                    continue
                if flag in _NOT_A_CONTEXTLAKE_FLAG:
                    used.add(flag)
                    continue
                line_no = prose.count("\n", 0, m.start()) + 1
                bad.append(
                    f"{_relative(page)}:{line_no} `{span}`: {flag} is not a contextlake flag")
    return bad, used


def test_every_flag_in_an_inline_span_exists_on_some_command() -> None:
    bad, _used = _bare_flag_problems()
    assert not bad, "the docs name flags no contextlake command has:\n  " + "\n  ".join(bad)


def test_the_foreign_flag_list_has_no_stale_entries() -> None:
    _bad, used = _bare_flag_problems()
    stale = sorted(set(_NOT_A_CONTEXTLAKE_FLAG) - used)
    assert not stale, f"these entries no longer match any doc span, so delete them: {stale}"


# --- the extractor itself must see a bad flag, in both places a command can hide -------------

def _write(tmp_path: Path, body: str) -> Path:
    page = tmp_path / "page.md"
    page.write_text(body, encoding="utf-8")
    return page


@pytest.mark.parametrize("body, kind", [
    ("```bash\ncontextlake kb source add jira --name jira\n```\n", "fenced"),
    ("Run `contextlake kb source add [--name NAME]` to add one.\n", "inline"),
    ("Pipe it: `printf '%s'\n \"$X\" | contextlake kb index --languages c`.\n", "inline"),
    ("```bash\ncontextlake kb index \\\n  --languages c\n```\n", "fenced"),
    ("```console\n$ contextlake kb index --languages c\n```\n", "fenced"),
])
def test_the_scanner_catches_a_flag_that_does_not_exist(
        tmp_path: Path, body: str, kind: str) -> None:
    found = extract(_write(tmp_path, body))
    assert found, "the scanner found no command in a page that holds one"
    assert all(k == kind for _l, k, _w in found)
    assert any(check(words, k) for _l, k, words in found), "a flag that does not exist passed"


@pytest.mark.parametrize("body", [
    "```bash\ncontextlake kb source add jira --type atlassian --set token_env=MY_TOKEN\n```\n",
    "Run `contextlake kb source add NAME --type atlassian`.\n",
    "```bash\nprintf '%s' \"$URL\" | contextlake kb source add jira --type atlassian "
    "--from-stdin mcp\n```\n",
    "The `contextlake schedule` command.\n",
    "Use `contextlake kb serve --transport <stdio|http|sse>`.\n",
    "See `contextlake <command> --help`.\n",
])
def test_the_scanner_accepts_a_valid_form(tmp_path: Path, body: str) -> None:
    found = extract(_write(tmp_path, body))
    assert found, "the scanner found no command in a page that holds one"
    assert all(check(words, k) is None for _l, k, words in found)
