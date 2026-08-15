"""Every citation the INDEXER writes must resolve against the tree it just indexed.

``test_citation_verification.py`` covers the checker thoroughly: each status, the
window's edges, huge files, off-by-default, dedup. Every node in it is hand-built. So
the checker is pinned and the producer is not, and the claim contextlake actually makes
to an agent is about the producer:

    "Every cited node carries citation_status, checked against the file on disk."

If the parser started recording a line one off, or a path relative to the wrong root,
every test in that file would still pass. This one indexes a real tree with the real
`kb index` and checks what came out.

**What counts as a failure here.** Only the reasons that mean a citation was made and
does not resolve: ``file_missing``, ``line_out_of_range``, ``name_absent``.
``no_citation`` is deliberately excluded. It fires on nodes with no file at all, which
on any real repo means its external dependencies (an imported stdlib module, an
``#include`` of a system header) -- things that are supposed to have no file in this
tree. Failing on those would manufacture a defect per dependency, which is the same
reasoning ``verify_citations`` already uses to keep ``checkout_missing`` separate from
``file_missing``. The serving path agrees: ``store/drift.py`` maps ``no_citation`` to
*unverifiable*, never to stale. What this test does assert about them is narrower and
checkable: ``no_citation`` may only appear on a node that genuinely has no file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# A citation was written and it does not resolve. These are the failures.
CITATION_IS_WRONG = frozenset({"file_missing", "line_out_of_range", "name_absent"})

SOURCES = {
    # The out-of-line definition is the point of the C++ file, not decoration. Measured:
    # the parser records `Draw` at line 11, the `Widget::Draw(int n)` line itself, NOT at
    # the `void` return type on line 10 -- so this row verifies on an exact hit and the
    # +/-2 window plays no part in it. Worth stating, because a first draft of this file
    # asserted the opposite in a comment and a window-only break-test then passed while
    # changing nothing. What the row does pin is that a qualified out-of-line definition
    # gets a citation at all, and one that lands on the name.
    "src/widget.cpp": """\
#include <string>

class Widget {
public:
    void Draw(int n);
    int count_;
};

// out-of-line definition, the dominant C++ shape
void
Widget::Draw(int n)
{
    count_ = n;
}

int helper(int a) { return a + 1; }
""",
    "src/app.py": """\
import os


class Engine:
    \"\"\"Does the thing.\"\"\"

    def start(self, n):
        return helper(n)


def helper(n):
    return n + 1
""",
    "src/lib.ts": """\
export interface Shape { kind: string }

export class Box implements Shape {
  kind = "box";
  area(w: number, h: number): number {
    return w * h;
  }
}

export function makeBox(): Box {
  return new Box();
}
""",
}


def _indexed(tmp_path):
    """Index a real three-language repo with the shipped command; return (store, repo_id)."""
    home = tmp_path / "home"
    (home / ".contextlake").mkdir(parents=True)
    store_dir = tmp_path / "store"
    repo = tmp_path / "ws" / "demo"
    for rel, text in SOURCES.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", *ident, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *ident, "commit", "-qm", "init"], cwd=repo, check=True)

    # Embeddings off: this measures citations, and a model download would make the
    # outcome depend on the network.
    (home / ".contextlake" / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store_dir}"\n[embeddings]\nenabled = false\n', encoding="utf-8")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1"}
    r = subprocess.run([sys.executable, "-m", "contextlake", "kb", "index", str(repo)],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"indexing failed:\n{(r.stdout + r.stderr)[-2000:]}"

    from contextlake.kb.store.sqlite_store import SqliteStore

    store = SqliteStore(store_dir / "index.sqlite")
    repos = store.list_repos()
    assert len(repos) == 1, f"expected one indexed repo, got {[x.id for x in repos]}"
    return store, repos[0].id


def _nodes(store, repo_id):
    """Every node of the repo, via the same call the product's own graph export uses."""
    from contextlake.kb.visualize import repo_subgraph

    nodes, _edges = repo_subgraph(store, repo_id, max_nodes=5000)
    return nodes


def _checks(store, nodes):
    from contextlake.kb.eval import verify_citations

    return verify_citations(store, [n.id for n in nodes])


def test_every_citation_the_indexer_wrote_resolves_on_disk(tmp_path):
    store, repo_id = _indexed(tmp_path)
    try:
        nodes = _nodes(store, repo_id)
        by_id = {n.id: n for n in nodes}
        checks = _checks(store, nodes)

        wrong = [c for c in checks if c.reason in CITATION_IS_WRONG]
        assert not wrong, "the indexer wrote citations that do not resolve:\n" + "\n".join(
            f"  {c.reason}: {by_id[c.node_id].kind} {by_id[c.node_id].name} at {c.cite}"
            for c in wrong)

        # Without this the test passes on a store the indexer wrote nothing into, which
        # is the failure it is least able to notice.
        verified = [c for c in checks if c.status == "verified"]
        assert len(verified) >= 10, (
            f"only {len(verified)} verified citations from {len(nodes)} nodes; the "
            f"indexer is not producing enough to make this a real check")
        langs = {by_id[c.node_id].lang for c in verified}
        assert {"python", "cpp", "typescript"} <= langs, (
            f"verified citations only cover {sorted(x for x in langs if x)}; a "
            f"per-language regression would hide behind the other two")

        # The one thing worth asserting about the excluded reason: it may only appear
        # where there is genuinely nothing to cite.
        for c in checks:
            if c.reason == "no_citation":
                n = by_id[c.node_id]
                assert n.file is None, (
                    f"{n.kind} {n.name} reports no_citation but records file {n.file!r}")
    finally:
        store.close()


def test_the_check_catches_a_citation_that_stopped_resolving(tmp_path):
    """Proof the gate above is not vacuous, kept as a test rather than done by hand once.

    The C++ method is moved to a line that exists and does not carry its name, which is
    ``name_absent`` -- the failure a file-exists or line-count check cannot see, and the
    one a drifting parser would actually produce.
    """
    store, repo_id = _indexed(tmp_path)
    try:
        nodes = _nodes(store, repo_id)
        draw = next(n for n in nodes
                    if n.name == "Draw" and (n.file or "").endswith("widget.cpp"))
        assert [c.status for c in _checks(store, [draw])] == ["verified"], (
            "the fixture's own starting point is wrong: Draw does not verify")

        # 16 is the last line of widget.cpp; with a +/-2 window nothing in view says Draw.
        # pydantic model, not a dataclass: model_copy is its replace().
        store.upsert_nodes(repo_id, [draw.model_copy(update={"line_start": 16,
                                                            "line_end": 16})])
        moved = store.get_node(draw.id)
        assert moved.line_start == 16, "the fixture edit did not take"

        check = _checks(store, [moved])[0]
        assert check.status == "broken" and check.reason == "name_absent", (
            f"a citation pointing at the wrong line reported {check.status}/{check.reason}")
    finally:
        store.close()
