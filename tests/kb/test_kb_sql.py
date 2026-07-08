"""Tests for SQL DDL extraction (kb/sql.py)."""

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
