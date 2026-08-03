"""Golden-shard regression test for :func:`index_repo_dir`.

Guards the whole walk -> classify -> parse -> resolve pipeline as one unit: a
synthetic repo covering every extraction kind (code in several languages, HCL,
SQL, manifests, ADRs) plus every skip rule (pruned dirs, ignore file, generated
code by name and by header, oversize) is indexed, and the resulting shard is
compared against a recorded snapshot.

**Why the comparison is order-canonical rather than byte-for-byte.** The obvious
form of this test -- assert the shard's JSON bytes -- cannot work: tree-sitter's
``QueryCursor.captures()`` (0.26.0) returns each capture list in an order that
varies run to run *and within a single process*, so ``parse_source`` emits a
file's definitions in a different sequence each time and six consecutive runs of
the *same* code produce six distinct shard byte-strings. Node/edge CONTENT is
stable under that entropy (a canonicalised snapshot was identical across those
same runs); only the sequence moves. So content is asserted canonically here, and the ordering that
``index_repo_dir`` itself does control -- file visit order -- is asserted
separately by ``test_file_nodes_follow_walk_order``.

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
    "svc/core/orders.py": (
        "from .base import BaseHandler\n"
        "\n"
        "\n"
        "class OrderHandler(BaseHandler):\n"
        "    def handle(self, payload):\n"
        "        rec = self.load(payload)\n"
        "        return self.render(rec)\n"
        "\n"
        "    def load(self, payload):\n"
        "        return payload\n"
        "\n"
        "\n"
        "def bootstrap():\n"
        "    h = OrderHandler()\n"
        "    return h.handle({})\n"
    ),
    "svc/api/routes.py": (
        "from flask import Flask\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        "\n"
        '@app.route("/v1/orders", methods=["GET"])\n'
        "def list_orders():\n"
        '    return query("SELECT id FROM orders")\n'
        "\n"
        "\n"
        "def query(sql):\n"
        '    return execute("INSERT INTO orders (id) VALUES (1)")\n'
        "\n"
        "\n"
        "def execute(sql):\n"
        "    return sql\n"
    ),
    "web/src/models.ts": (
        "export interface Order { id: number; total: number; }\n"
        "\n"
        "export enum OrderState { Draft, Placed, Shipped }\n"
        "\n"
        "export class OrderStore {\n"
        "  private items: Order[] = [];\n"
        "  add(o: Order): void { this.items.push(o); }\n"
        "}\n"
    ),
    "app/Domain/Ledger.cs": (
        "using System;\n"
        "\n"
        "namespace App.Domain\n"
        "{\n"
        "    public interface IPostable { void Post(); }\n"
        "\n"
        "    public class Ledger : IPostable\n"
        "    {\n"
        "        public void Post() { Recalculate(); }\n"
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
        "CREATE TABLE customers (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  name TEXT NOT NULL\n"
        ");\n"
        "\n"
        "CREATE TABLE orders (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  customer_id INTEGER REFERENCES customers(id)\n"
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
    "app/Domain/Ledger.Designer.cs": "namespace App.Domain { class D { } }\n",  # by name
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


def test_file_nodes_follow_walk_order(tmp_path):
    """File nodes land in the shard in os.walk order.

    The one ordering guarantee the indexer itself owns (definition order within a
    file is tree-sitter's, and is not deterministic). Expected order is recomputed
    by walking the same tree rather than hard-coded, so this does not depend on a
    particular filesystem's directory hash order.
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
