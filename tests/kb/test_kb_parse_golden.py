"""Golden-shard regression test for :func:`index_repo_dir`.

Guards the whole walk -> classify -> parse -> resolve pipeline as one unit: a
synthetic repo covering every extraction kind (code in several languages, HCL,
SQL, manifests, ADRs) plus every skip rule (pruned dirs, ignore file, generated
code by name and by header, oversize) is indexed, and the resulting shard is
compared against a recorded snapshot.

**Why the comparison is order-canonical, and why a byte assertion sits beside
it.** This test was originally written order-canonically because it had to be:
tree-sitter's ``QueryCursor.captures()`` (0.26.0) returns each capture list in an
order that varies run to run *and within a single process*, so ``parse_source``
emitted a file's definitions in a different sequence each time and six
consecutive indexes of this unchanged fixture produced six distinct shard
byte-strings. Node/edge CONTENT was stable under that entropy (a canonicalised
snapshot was identical across those runs); only the sequence moved -- which is
exactly why the canonical form could not catch it.

``parse._sorted_captures`` removed that entropy, so
``test_shard_bytes_are_reproducible`` now asserts the property that was
previously untestable: repeated indexes of one unchanged tree produce identical
shard bytes. Both assertions are kept deliberately -- the canonical one pins
content and would survive a future deliberate reordering, the byte one pins
reproducibility and is the only thing that would catch capture-order entropy
coming back. The ordering ``index_repo_dir`` itself owns -- file visit order --
is asserted separately by ``test_file_nodes_follow_walk_order``.

Regenerate the snapshot with ``CONTEXTLAKE_UPDATE_GOLDEN=1 pytest
tests/kb/test_kb_parse_golden.py`` after an intentional extraction change, and
review the diff: a change here means every already-indexed repo's shard changed
too, which is what ``PARSER_VERSION`` exists to signal.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from contextlake.kb.parse import _SKIP_DIRS, index_repo_dir

GOLDEN = Path(__file__).parent / "golden" / "parse_shard.json"

# `verified_at` is stamped with today's date, which is not a property of the
# extraction; normalise it so the snapshot does not expire overnight.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# rel path -> contents. Deliberately generic, synthetic sources: this is a public
# repository, so no fixture may resemble a real internal codebase.
FIXTURE: dict[str, str] = {
    "svc/core/base.py": (
        "import json\n"
        "import logging\n"
        "\n"
        "\n"
        "class BaseHandler:\n"
        '    """Root of the handler hierarchy."""\n'
        "\n"
        "    def handle(self, payload):\n"
        "        return self.render(payload)\n"
        "\n"
        "    def render(self, payload):\n"
        "        return json.dumps(payload)\n"
        "\n"
        "\n"
        "def make_logger(name):\n"
        "    return logging.getLogger(name)\n"
    ),
    "svc/core/readings.py": (
        "from .base import BaseHandler\n"
        "\n"
        "\n"
        "class ReadingHandler(BaseHandler):\n"
        "    def handle(self, payload):\n"
        "        rec = self.load(payload)\n"
        "        return self.render(rec)\n"
        "\n"
        "    def load(self, payload):\n"
        "        return payload\n"
        "\n"
        "\n"
        "def bootstrap():\n"
        "    h = ReadingHandler()\n"
        "    return h.handle({})\n"
    ),
    "svc/api/routes.py": (
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        # A constant plus a BARE read of it, so the golden shard covers
        # `attrs["declaration"]` and the `uses` relation. Without the read, this
        # fixture's only constant is `app`, which appears solely as `@app.route`
        # -- attribute access, which is correctly not a use -- so the whole
        # pipeline gate recorded zero `uses` edges and could not have noticed the
        # relation breaking entirely.
        "PAGE_SIZE = 50\n"
        "\n"
        "\n"
        '@app.route("/v1/readings", methods=["GET"])\n'
        "def list_readings():\n"
        '    return query("SELECT id FROM readings", PAGE_SIZE)\n'
        "\n"
        "\n"
        "def query(sql):\n"
        '    return execute("INSERT INTO readings (id) VALUES (1)")\n'
        "\n"
        "\n"
        "def execute(sql):\n"
        "    return sql\n"
    ),
    "web/src/models.ts": (
        "export interface Reading { id: number; value: number; }\n"
        "\n"
        "export enum ReadingState { Pending, Accepted, Rejected }\n"
        "\n"
        "export class ReadingStore {\n"
        "  private items: Reading[] = [];\n"
        "  add(o: Reading): void { this.items.push(o); }\n"
        "}\n"
    ),
    "app/Domain/StationUnit.cs": (
        "using System;\n"
        "\n"
        "namespace App.Domain\n"
        "{\n"
        "    public interface ICalibratable { void Calibrate(); }\n"
        "\n"
        "    public class StationUnit : ICalibratable\n"
        "    {\n"
        "        public void Calibrate() { Recalculate(); }\n"
        "        public void Recalculate() { }\n"
        "    }\n"
        "}\n"
    ),
    # Out-of-line method definitions split across a header and its .cpp: the
    # case _resolve_pending_methods exists for.
    "native/include/widget.h": (
        "#pragma once\n"
        "\n"
        "class Widget {\n"
        "public:\n"
        "    void Draw();\n"
        "    int Area() const;\n"
        "};\n"
        "\n"
        "class Sprite;\n"
    ),
    "native/src/widget.cpp": (
        '#include "widget.h"\n'
        "\n"
        "void Widget::Draw() {\n"
        "    Area();\n"
        "}\n"
        "\n"
        "int Widget::Area() const {\n"
        "    return 1;\n"
        "}\n"
    ),
    "infra/main.tf": (
        'variable "region" {\n'
        '  default = "eu-west-1"\n'
        "}\n"
        "\n"
        'resource "aws_s3_bucket" "assets" {\n'
        "  region = var.region\n"
        "}\n"
    ),
    "db/schema.sql": (
        "CREATE TABLE stations (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  name TEXT NOT NULL\n"
        ");\n"
        "\n"
        "CREATE TABLE readings (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  station_id INTEGER REFERENCES stations(id)\n"
        ");\n"
    ),
    "package.json": (
        "{\n"
        '  "name": "demo-web",\n'
        '  "version": "1.0.0",\n'
        '  "dependencies": { "axios": "^1.6.0" }\n'
        "}\n"
    ),
    "docs/adr/0001-use-a-single-store.md": (
        "# Use a single store\n"
        "\n"
        "## Status\n"
        "Accepted\n"
    ),
    "docs/guide.md": "# Just a guide\n\nNot a decision record.\n",
    # --- everything below must be skipped, each by a different rule ---------
    ".contextlakeignore": "vendorcode/\n*.gen.py\n",
    "vendorcode/bundled.py": "def vendored():\n    pass\n",   # pruned directory
    "svc/core/table.gen.py": "def generated_thing():\n    pass\n",  # ignore pattern
    "app/Domain/StationUnit.Designer.cs": "namespace App.Domain { class D { } }\n",  # by name
    "generated/header.cs": "// <auto-generated />\nnamespace G { class H { } }\n",  # by header
    ".git/hooks/hook.py": "def never_indexed():\n    pass\n",  # pruned directory
}

# Large enough to trip the oversize guard below without writing a 5 MiB file.
_OVERSIZE_LIMIT = 2048


def _build(root: Path) -> None:
    for rel, text in FIXTURE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "svc" / "core" / "blob.py").write_text(
        "# pad\n" * 500 + "def huge():\n    pass\n", encoding="utf-8")


def _index(root: Path):
    return index_repo_dir(str(root), "demo/fixture", head_commit="0" * 40,
                          max_file_bytes=_OVERSIZE_LIMIT)


def _canonical(shard) -> str:
    d = json.loads(shard.model_dump_json())
    d["nodes"] = sorted(d["nodes"], key=lambda n: json.dumps(n, sort_keys=True))
    d["edges"] = sorted(d["edges"], key=lambda e: json.dumps(e, sort_keys=True))
    return _DATE.sub("DATE", json.dumps(d, indent=2, sort_keys=True)) + "\n"


def test_shard_matches_golden(tmp_path):
    _build(tmp_path)
    actual = _canonical(_index(tmp_path))
    if os.environ.get("CONTEXTLAKE_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(actual, encoding="utf-8")
    assert actual == GOLDEN.read_text(encoding="utf-8")


def test_shard_bytes_are_reproducible(tmp_path):
    """Re-indexing one unchanged tree produces byte-identical shards.

    The invariant ``store.shards.archive_shard`` documents ("a repo re-indexed at
    the same commit overwrites identically"), which was silently false while
    ``QueryCursor.captures()`` ordering leaked into the shard -- see the module
    docstring. Asserted on raw ``model_dump_json`` output, NOT the canonical form,
    because canonicalising is precisely what hid the bug.

    Deliberately more than two runs: the entropy was per-call, so two runs could
    coincide by luck. ``verified_at`` is normalised for the same reason as in
    ``_canonical`` -- a midnight rollover mid-test is not a determinism failure.
    """
    _build(tmp_path)
    raws = [_DATE.sub("DATE", _index(tmp_path).model_dump_json(indent=2))
            for _ in range(5)]
    assert len(set(raws)) == 1, (
        f"{len(set(raws))} distinct shard byte-strings across {len(raws)} indexes "
        "of an unchanged tree; extraction has become order-nondeterministic again"
    )


def test_file_nodes_follow_walk_order(tmp_path):
    """File nodes land in the shard in os.walk order.

    One of the two ordering guarantees now in force: this one is the indexer's own
    (definition order within a file is tree-sitter's, made deterministic by
    ``parse._sorted_captures``). Expected order is recomputed by walking the same
    tree rather than hard-coded, so this does not depend on a particular
    filesystem's directory hash order.
    """
    _build(tmp_path)
    shard = _index(tmp_path)
    actual = [n.name for n in shard.nodes if n.kind == "file"]

    wanted = set(actual)
    expected = []
    for dirpath, dirnames, filenames in os.walk(tmp_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            rel = str((Path(dirpath) / fn).relative_to(tmp_path))
            if rel in wanted:
                expected.append(rel)
    assert actual == expected
    assert len(actual) > 1  # an empty/one-element list would prove nothing


def test_skip_counters_are_reported(tmp_path, gls_logs):
    """Every skip rule is tallied and surfaced in the one summary line."""
    # caplog's set_level restores the logger's level on teardown; setting it
    # directly would leave the package logger at DEBUG for the rest of the
    # session (conftest's reset_logging clears handlers, not levels).
    gls_logs.set_level(logging.DEBUG, logger="contextlake")
    _build(tmp_path)
    _index(tmp_path)
    summary = [m for m in gls_logs.messages if m.startswith("  parsed ")]
    assert summary == [
        "  parsed 11 file(s); skipped 2 generated, 1 oversized, 1 ignored"]
