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
