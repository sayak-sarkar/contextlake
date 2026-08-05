"""Tests for SQL DDL extraction (kb/sql.py)."""

from contextlake.kb.parse import index_repo_dir
from contextlake.kb.sql import parse_sql

SCHEMA_SQL = b"""
CREATE TABLE Customer (
    CustomerId INT PRIMARY KEY,
    Name NVARCHAR(100)
);
GO

CREATE TABLE [dbo].[Orders] (
    OrderId INT PRIMARY KEY,
    CustomerId INT NOT NULL REFERENCES Customer(CustomerId),
    CONSTRAINT FK_Order_Region FOREIGN KEY (RegionId) REFERENCES [dbo].[Region](RegionId)
);
GO

CREATE OR ALTER VIEW ActiveOrders AS
    SELECT * FROM Orders WHERE Status = 1;
GO

CREATE PROCEDURE usp_GetOrders AS
    SELECT * FROM Orders;
GO
"""


def _by_kind(nodes):
    out: dict[str, set] = {}
    for n in nodes:
        out.setdefault(n.kind, set()).add(n.name)
    return out


def test_parse_sql_extracts_ddl_defs():
    nodes, _refs = parse_sql("data/schema", "schema.sql", SCHEMA_SQL)
    kinds = _by_kind(nodes)
    assert kinds["table"] == {"customer", "orders"}      # names normalized + casefolded
    assert kinds["view"] == {"activeorders"}
    assert kinds["procedure"] == {"usp_getorders"}
    o = next(n for n in nodes if n.name == "orders")
    assert o.file == "schema.sql" and o.lang == "sql" and o.line_start


def _addr(nodes, nid):
    return next(n.name for n in nodes if n.id == nid)


def test_parse_sql_captures_fk_references():
    nodes, refs = parse_sql("data/schema", "schema.sql", SCHEMA_SQL)
    pairs = {(_addr(nodes, src), tgt) for src, tgt, _f, _l in refs}
    # inline column FK and table-level constraint FK both attributed to Orders
    assert ("orders", "customer") in pairs
    assert ("orders", "region") in pairs
    # bracketed + schema-qualified targets normalized to bare casefolded name
    assert all("." not in t and "[" not in t for _s, t in pairs)


def test_commented_out_references_is_not_a_foreign_key():
    # A commented-out FK is dead DDL a real script accumulates, not a schema:
    # emitting it invented an `orders -> regions` relationship that the database
    # does not have, and it resolved like a real edge whenever the named table
    # happened to exist elsewhere in the repo.
    sql = (
        b"CREATE TABLE orders (\n"
        b"    order_id INT PRIMARY KEY,\n"
        b"    -- region_id INT NULL REFERENCES regions(region_id),\n"
        b"    /* legacy_id INT NULL REFERENCES legacy(legacy_id), */\n"
        b"    /* multi-line:\n"
        b"       tenant_id INT NULL REFERENCES tenants(tenant_id), */\n"
        b"    customer_id INT NOT NULL REFERENCES customers(customer_id)\n"
        b");\n"
    )
    nodes, refs = parse_sql("r", "s.sql", sql)
    assert {n.name for n in nodes} == {"orders"}
    assert {tgt for _s, tgt, _f, _l in refs} == {"customers"}
    # the surviving FK still reports the line it was really written on: the mask
    # preserves every newline, so nothing downstream has to re-derive positions
    assert [line for _s, _t, _f, line in refs] == [7]


def test_a_comment_marker_inside_a_string_literal_does_not_blank_real_ddl():
    # The inverse failure of the fix above: treating `--` or `/*` inside a quoted
    # literal as a comment would swallow the rest of the statement and silently
    # LOSE real tables and FKs, which is worse than the false positive it fixes.
    sql = (
        b"CREATE TABLE audit (\n"
        b"    note NVARCHAR(50) DEFAULT 'n/a -- not set',\n"
        b"    tag NVARCHAR(50) DEFAULT 'a /* b',\n"
        b"    quoted NVARCHAR(50) DEFAULT 'it''s -- fine',\n"
        b"    actor_id INT NOT NULL REFERENCES actors(actor_id)\n"
        b");\n"
    )
    nodes, refs = parse_sql("r", "s.sql", sql)
    assert {n.name for n in nodes} == {"audit"}
    assert {tgt for _s, tgt, _f, _l in refs} == {"actors"}


def test_commented_out_create_table_defines_nothing():
    # Same root cause on the def side: a CREATE inside a comment used to mint a
    # node, and (being a scope boundary) to cut the live table's FK scope short.
    sql = (
        b"-- CREATE TABLE ghost (id INT);\n"
        b"/* CREATE VIEW spectre AS SELECT 1; */\n"
        b"CREATE TABLE real_table (\n"
        b"    -- CREATE TABLE decoy (id INT),\n"
        b"    owner_id INT NOT NULL REFERENCES owners(owner_id)\n"
        b");\n"
    )
    nodes, refs = parse_sql("r", "s.sql", sql)
    assert {n.name for n in nodes} == {"real_table"}
    assert {tgt for _s, tgt, _f, _l in refs} == {"owners"}


def test_alter_table_fk_not_misattributed():
    sql = (
        b"CREATE TABLE a (id INT PRIMARY KEY);\n"
        b"ALTER TABLE b ADD CONSTRAINT fk FOREIGN KEY (x) REFERENCES c(id);\n"
        b"CREATE TABLE c (id INT PRIMARY KEY);\n"
    )
    nodes, refs = parse_sql("r", "s.sql", sql)
    name = {n.id: n.name for n in nodes}
    pairs = {(name[src], tgt) for src, tgt, _f, _l in refs}
    # the ALTER-added FK belongs to table b (not defined here); it must NOT be
    # attributed to table a (the preceding CREATE TABLE). Dropped is correct.
    assert ("a", "c") not in pairs
    assert not refs  # no FK is attributable in this GO-less ALTER-only case


def test_index_repo_dir_resolves_sql_references(tmp_path):
    # tables split across files in the same repo (cross-file FK)
    (tmp_path / "customer.sql").write_text("CREATE TABLE Customer (Id INT PRIMARY KEY);\n")
    (tmp_path / "orders.sql").write_text(
        "CREATE TABLE Orders (\n"
        "  Id INT PRIMARY KEY,\n"
        "  CustomerId INT REFERENCES Customer(Id)\n"
        ");\n"
    )
    shard = index_repo_dir(str(tmp_path), "data/schema")
    name = {n.id: n.name for n in shard.nodes}
    refs = {(name[e.src], name[e.dst]) for e in shard.edges if e.relation == "references"}
    assert ("orders", "customer") in refs           # cross-file FK resolves
    assert {"table"} <= {n.kind for n in shard.nodes}


def test_index_repo_dir_languages_filter_excludes_sql(tmp_path):
    (tmp_path / "s.sql").write_text("CREATE TABLE T (Id INT);\n")
    (tmp_path / "app.py").write_text("def f():\n    pass\n")
    shard = index_repo_dir(str(tmp_path), "r", languages=["python"])
    kinds = {n.kind for n in shard.nodes}
    assert "table" not in kinds
    assert "function" in kinds
