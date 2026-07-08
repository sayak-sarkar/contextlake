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
