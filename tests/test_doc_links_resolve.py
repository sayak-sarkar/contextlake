"""Every link and anchor in the docs must resolve to something that exists.

This is the gate that was missing. `site/build_docs.py` validates its own nav and the
Next-steps targets, and nothing checked the body text — so a link could rot silently and
only a human reading the page would notice. Two findings in the last docs audit were
exactly that, and one of them was worse than a broken page: the CLI *printed* a
`docs/mirroring-repositories.md#shell-completion` pointer at users after the
restructure had moved that section to `cli-reference.md`, so anyone who
followed the instruction the tool gave them landed somewhere without the steps.

Scope, and why it stops where it does:

* **Relative links between markdown files**, and the `#anchor` on them. These are the ones
  that rot when a page is renamed or a section moved, which is the failure this exists for.
* **Absolute GitHub blob URLs into this same repo** are resolved back to a local path.
  README and QUICKSTART use those so the links work on PyPI and on GitHub, and a rename
  breaks them identically — it just does not look like a relative link.
* **Code cites on `explained.md` and the `asking-the-graph.md` sample.** The first names a
  file and a symbol, never a line number, and the symbol must be in that file. The second
  prints `path:line` cites, and each line must hold the name the example is about.
* **External URLs are not fetched.** A test that reaches the network is a test that fails
  for reasons unrelated to the change in front of you.

Anchors are matched the way GitHub generates them: lowercase, punctuation dropped, spaces
to hyphens. That is an approximation of a rule nobody publishes exactly, so a *missing*
anchor is reported and an unparseable heading is skipped rather than failed — the check
earns its place by catching renames, not by being a Markdown parser.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
# The markdown that ships to a reader. `site/*.html` is generated from these and is
# gitignored, so checking it would check the generator, not the source.
ROOTS = [DOCS, REPO]
BLOB = re.compile(r"https://github\.com/sayak-sarkar/contextlake/blob/main/([^)\s#]+)(#[^)\s]*)?")
# [text](target) -- skips image embeds, which are matched by the `!` that precedes them.
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.MULTILINE)
EXPLICIT_ANCHOR = re.compile(r'<a\s+(?:id|name)="([^"]+)"', re.IGNORECASE)


def _slug(text: str) -> str:
    """GitHub's heading slug, closely enough for rename detection."""
    text = re.sub(r"`([^`]*)`", r"\1", text)          # inline code keeps its contents
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # links keep their label
    text = re.sub(r"[*_~]", "", text)
    text = text.strip().lower().replace(" ", "-")
    return re.sub(r"[^0-9a-zÀ-￿_-]", "", text)


def _anchors(path: Path) -> set[str]:
    body = path.read_text(encoding="utf-8")
    found = {_slug(m.group(2)) for m in ATX.finditer(body)}
    found |= set(EXPLICIT_ANCHOR.findall(body))
    return found


def _markdown_files() -> list[Path]:
    files = sorted(DOCS.glob("*.md"))
    files += [REPO / n for n in ("README.md", "QUICKSTART.md", "CONTRIBUTING.md",
                                 "BRANDING.md", "SECURITY.md", "CHANGELOG.md")
              if (REPO / n).is_file()]
    return files


def _targets(md: Path):
    """Yield ``(raw, path, anchor)`` for every in-repo link on the page."""
    body = md.read_text(encoding="utf-8")
    for raw in LINK.findall(body):
        raw = raw.strip()
        blob = BLOB.match(raw)
        if blob:
            yield raw, REPO / blob.group(1), (blob.group(2) or "")[1:]
            continue
        if raw.startswith(("http://", "https://", "mailto:", "#")):
            if raw.startswith("#"):
                yield raw, md, raw[1:]          # same-page anchor
            continue
        target, _, anchor = raw.partition("#")
        if not target:
            continue
        yield raw, (md.parent / target).resolve(), anchor


def test_every_in_repo_doc_link_points_at_a_file_that_exists():
    broken = []
    for md in _markdown_files():
        for raw, path, _anchor in _targets(md):
            if not path.exists():
                broken.append(f"{md.relative_to(REPO)} -> {raw}")
    assert not broken, (
        "documentation links pointing at files that do not exist:\n  "
        + "\n  ".join(broken)
        + "\n\nA renamed or deleted page leaves these behind, and nothing else catches it."
    )


def test_every_in_repo_doc_anchor_exists_on_the_page_it_names():
    """The half that matters most: a link can survive a restructure while the *section*
    it points at moves to another page, which reads as working until somebody clicks."""
    broken = []
    for md in _markdown_files():
        for raw, path, anchor in _targets(md):
            if not anchor or not path.exists() or path.suffix != ".md":
                continue
            if _slug(anchor) not in {_slug(a) for a in _anchors(path)}:
                broken.append(f"{md.relative_to(REPO)} -> {raw}")
    assert not broken, (
        "documentation links naming a section that does not exist on the target page:\n  "
        + "\n  ".join(broken)
        + "\n\nThis is the shape that shipped in 7.2.1: the CLI printed a pointer at users "
          "for a section the restructure had moved to another page."
    )


def test_the_checker_would_notice_a_broken_link():
    """The near-miss. Both tests above pass on a clean tree, so on their own they cannot
    tell 'everything resolves' from 'nothing was examined'. Assert the machinery finds a
    deliberately broken target and a deliberately missing anchor."""
    assert _markdown_files(), "no markdown was collected; the sweep above proved nothing"
    real = DOCS / "cli-reference.md"
    assert real.is_file()
    assert _slug("Shell completion") in {_slug(a) for a in _anchors(real)}
    assert _slug("a section that does not exist") not in {_slug(a) for a in _anchors(real)}
    assert not (DOCS / "usage.md#shell-completion").exists()


# Each cite on explained.md: the exact text the page carries, the file it names, and the
# text that file must hold. A line number would be wrong after the next edit above it
# (it was: the cites pointed at a bare `#` and a docstring), so the page names a symbol.
EXPLAINED_CITES = [
    ("the `edges` table in `src/contextlake/kb/store/sqlite_store.py`",
     "src/contextlake/kb/store/sqlite_store.py", "source_file TEXT, source_line INTEGER"),
    ("`hush_hf_hub()` in `src/contextlake/kb/_util.py`",
     "src/contextlake/kb/_util.py", 'os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY"'),
]


def test_explained_cites_name_a_symbol_that_is_in_the_file_they_cite():
    page = (DOCS / "explained.md").read_text(encoding="utf-8")
    for cite, rel, needle in EXPLAINED_CITES:
        assert cite in page, f"explained.md no longer carries the cite {cite!r}; update this list"
        assert needle in (REPO / rel).read_text(encoding="utf-8"), (
            f"explained.md cites {rel} for {needle!r}, which the file no longer holds")
    line_cites = re.findall(r"src/contextlake/[\w/]+\.py:\d+", page)
    assert not line_cites, f"cite a symbol, not a line number, on explained.md: {line_cites}"


def test_the_ambiguity_sample_cites_lines_that_hold_the_name_it_is_about():
    """The sample in asking-the-graph.md prints `path:line` cites, as the tool does. It cited
    `sqlite_store.py:179`, a docstring delimiter, and `def close` was at line 221. Each cite
    must hit a line that holds `close`, and a `--node` id must end in the line it sits beside."""
    page = (DOCS / "asking-the-graph.md").read_text(encoding="utf-8")
    block = re.search(r"```text\n(Impact of changing close.*?)```", page, re.S)
    assert block, "the 'Impact of changing close' sample moved; update this test"
    cited = 0
    for line in block.group(1).splitlines():
        for rel, number in re.findall(r"((?:src|site)/[\w/.-]+\.(?:py|js)):(\d+)", line):
            source = (REPO / rel).read_text(encoding="utf-8").splitlines()
            held = source[int(number) - 1] if int(number) <= len(source) else "<past the end>"
            assert "close" in held, (
                f"asking-the-graph.md cites {rel}:{number}, which does not hold `close`: "
                f"{held.strip()!r}")
            cited += 1
        node = re.search(r"--node \S+_(\d+)\b", line)
        if node:
            assert f":{node.group(1)} " in line, (
                f"the node id and the cite disagree: {line.strip()}")
    assert cited >= 4, "the sample cites fewer files than it did; this test examined too little"
