"""Object-name shapes in the SQL extractor (kb/sql.py).

`CREATE TABLE IF NOT EXISTS orders (...)` used to index a table named `if`: the name token
was the first bare identifier after TABLE, so the real table and its foreign keys were
lost. Quoted names (`"Order"`, `` `order` ``, `[dbo].[Orders]`) and names with more than
one qualifier were read as the wrong table or not at all.

Each case below is one dialect's real spelling of a definition.
"""

import time

import pytest

from contextlake.kb.flow.data import extract_data_refs
from contextlake.kb.ids import make_id
from contextlake.kb.sql import _norm_name, parse_sql


def _parse(ddl: str):
    nodes, refs = parse_sql("r", "s.sql", ddl.encode())
    by_id = {n.id: n for n in nodes}
    return nodes, [(by_id[src].name, target, line) for src, target, _path, line in refs]


def _names(ddl: str, kind: str) -> list[str]:
    nodes, _refs = _parse(ddl)
    return [n.name for n in nodes if n.kind == kind]


# ---------------------------------------------------------------------------- tables

_TABLE_CASES = [
    # PostgreSQL
    ("CREATE TABLE IF NOT EXISTS orders (id int);", ["orders"]),
    ("CREATE TABLE IF NOT EXISTS public.orders (id int);", ["orders"]),
    ('CREATE TABLE "Order" (id int);', ["order"]),
    ('CREATE TABLE "public"."Order Items" (id int);', ["order items"]),
    ('CREATE TABLE IF NOT EXISTS "public"."Order" (id int);', ["order"]),
    ("CREATE TEMP TABLE scratch (id int);", ["scratch"]),
    ("CREATE TEMPORARY TABLE scratch (id int);", ["scratch"]),
    ("CREATE TEMP TABLE IF NOT EXISTS scratch (id int);", ["scratch"]),
    ("CREATE GLOBAL TEMP TABLE scratch (id int);", ["scratch"]),
    ("CREATE LOCAL TEMPORARY TABLE scratch (id int);", ["scratch"]),
    ("CREATE UNLOGGED TABLE logged_not (id int);", ["logged_not"]),
    # Oracle (the spelling the extractor already accepted)
    ("CREATE GLOBAL TEMPORARY TABLE scratch (id int);", ["scratch"]),
    # MariaDB, Snowflake, BigQuery
    ("CREATE OR REPLACE TABLE orders (id int);", ["orders"]),
    # MySQL
    ("CREATE TABLE `order` (id int);", ["order"]),
    ("CREATE TABLE IF NOT EXISTS `shop`.`items` (id int);", ["items"]),
    ("CREATE TABLE IF NOT EXISTS `order-items` (id int);", ["order-items"]),
    # SQLite
    ("CREATE TABLE IF NOT EXISTS main.users (id integer primary key);", ["users"]),
    # SQL Server
    ("CREATE TABLE [dbo].[Orders] (id int);", ["orders"]),
    ("CREATE TABLE [Order Details] (id int);", ["order details"]),
    ("CREATE TABLE [Sales].[Line Items] (id int);", ["line items"]),
    ("CREATE TABLE dbo.[Order Details] (id int);", ["order details"]),
    ("CREATE TABLE [db].[dbo].[Orders] (id int);", ["orders"]),
    ("CREATE TABLE db.dbo.orders (id int);", ["orders"]),
    ("IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'x') CREATE TABLE dbo.x (id int);",
     ["x"]),
    # unchanged
    ("CREATE TABLE orders (id int);", ["orders"]),
    ("CREATE TABLE Orders (id int);", ["orders"]),
    ("create table orders (id int);", ["orders"]),
    # a quoted name may be a word that is reserved unquoted
    ('CREATE TABLE "if" (id int);', ["if"]),
    # names that begin with the letters `if` are names, not the start of `IF NOT EXISTS`
    ("CREATE TABLE ifrs_ledger (id int);", ["ifrs_ledger"]),
    ("CREATE TABLE if_orders (id int);", ["if_orders"]),
    ("CREATE TABLE IF NOT EXISTS ifrs_ledger (id int);", ["ifrs_ledger"]),
    ("CREATE TABLE IF NOT EXISTS if_not_exists_log (id int);", ["if_not_exists_log"]),
    ("CREATE TABLE IF_X (id int);", ["if_x"]),
]


@pytest.mark.parametrize("ddl,expected", _TABLE_CASES)
def test_table_names(ddl, expected):
    assert _names(ddl, "table") == expected


def test_if_not_exists_is_never_a_table_name():
    for ddl in ("CREATE TABLE IF NOT EXISTS orders (id int);",
                "CREATE TABLE IF NOT EXISTS `a`.`b` (id int);",
                "CREATE TABLE IF NOT EXISTS\n    orders (id int);",
                "create table if not exists orders (id int);"):
        assert "if" not in _names(ddl, "table"), ddl


def test_a_file_that_ends_after_if_not_exists_has_no_table():
    assert _names("CREATE TABLE IF NOT EXISTS", "table") == []
    assert _names("CREATE TABLE IF NOT EXISTS   \n", "table") == []


def test_a_comment_between_if_not_exists_and_the_name_is_skipped():
    ddl = "CREATE TABLE IF NOT EXISTS -- the orders\n   orders (id int);"
    assert _names(ddl, "table") == ["orders"]


def test_the_line_is_the_create_line_when_the_name_is_on_a_later_line():
    nodes, _ = _parse("\n\nCREATE TABLE IF NOT EXISTS\n    orders (id int);\n")
    assert [(n.name, n.line_start) for n in nodes] == [("orders", 3)]


# ----------------------------------------------------------------- foreign keys

def test_foreign_keys_of_an_if_not_exists_table_belong_to_that_table():
    ddl = ("CREATE TABLE IF NOT EXISTS orders (\n"
           "  id int,\n"
           "  customer_id int REFERENCES customers(id)\n"
           ");\n")
    nodes, refs = _parse(ddl)
    assert [n.name for n in nodes] == ["orders"]
    assert refs == [("orders", "customers", 3)]


@pytest.mark.parametrize("target", [
    "customers", "Customers", '"Customers"', "public.customers", '"public"."Customers"',
    "[dbo].[Customers]", "[Customers]", "db.dbo.customers", "[db].[dbo].[Customers]",
])
def test_every_spelling_of_a_reference_lands_on_one_target_name(target):
    _, refs = _parse(f"CREATE TABLE orders (c int REFERENCES {target}(id));")
    assert refs == [("orders", "customers", 1)]


def test_a_quoted_reference_with_a_space_resolves_to_the_quoted_table():
    ddl = ('CREATE TABLE "Order Items" (id int);\n'
           'CREATE TABLE shipments (i int REFERENCES "Order Items"(id));\n')
    nodes, refs = _parse(ddl)
    assert refs == [("shipments", "order items", 2)]
    assert "order items" in {n.name for n in nodes}


def test_backticked_foreign_key_target():
    ddl = ("CREATE TABLE `order` (id int);\n"
           "CREATE TABLE IF NOT EXISTS `shop`.`items` (id int, o int REFERENCES `order`(id));\n")
    nodes, refs = _parse(ddl)
    assert [n.name for n in nodes] == ["order", "items"]
    assert refs == [("items", "order", 2)]


def test_a_temp_table_ends_the_scope_of_the_table_before_it():
    # Without TEMP in the scope-end pattern, `a` runs on through `b` and is credited
    # with b's reference to `c`.
    for kind in ("TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL TEMPORARY", "LOCAL TEMP"):
        ddl = ("CREATE TABLE a (id int);\n"
               f"CREATE {kind} TABLE b (x int REFERENCES c(id));\n")
        _, refs = _parse(ddl)
        assert refs == [("b", "c", 2)], kind


def test_or_replace_table_ends_the_scope_of_the_table_before_it():
    ddl = ("CREATE TABLE a (id int);\n"
           "CREATE OR REPLACE TABLE b (x int REFERENCES c(id));\n")
    _, refs = _parse(ddl)
    assert refs == [("b", "c", 2)]


def test_a_qualified_name_is_not_cut_back_to_its_qualifier():
    # Once the name part is strict, a failed match must not back off to the first part.
    _, refs = _parse("CREATE TABLE t (x int REFERENCES [Sales].[Line Items](id));")
    assert refs == [("t", "line items", 1)]
    _, refs = _parse("CREATE TABLE t (x int REFERENCES a.b.c(id));")
    assert refs == [("t", "c", 1)]


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE [Sales].[Line Items (id int);",       # bracket never closes
    "CREATE TABLE [Sales].[Line/Items] (id int);",      # a character a name body may not hold
    'CREATE TABLE "public"."odd/name" (id int);',
    "CREATE TABLE [Line/Items] (id int);",
])
def test_a_name_the_extractor_cannot_read_gives_no_table_rather_than_a_wrong_one(ddl):
    # A failed match must not back off to the qualifier and return `sales` or `public`,
    # and must not cut a name short at the first character it dislikes.
    assert _names(ddl, "table") == []


@pytest.mark.parametrize("target", ["[Sales].[Line/Items]", 'a.b."broken', "[Sales].[Line Items"])
def test_an_unreadable_reference_target_gives_no_reference(target):
    _, refs = _parse(f"CREATE TABLE t (x int REFERENCES {target}(id));")
    assert refs == []


# --------------------------------------------------------------------------- others

@pytest.mark.parametrize("ddl,expected", [
    ("CREATE VIEW IF NOT EXISTS v1 AS SELECT 1;", ["v1"]),
    ("CREATE OR REPLACE TEMP VIEW v1 AS SELECT 1;", ["v1"]),
    ("CREATE TEMPORARY VIEW v1 AS SELECT 1;", ["v1"]),
    ('CREATE OR REPLACE VIEW "V Quoted" AS SELECT 1;', ["v quoted"]),
    ("CREATE MATERIALIZED VIEW IF NOT EXISTS mv1 AS SELECT 1;", ["mv1"]),
    ("CREATE RECURSIVE VIEW rv1 (n) AS SELECT 1;", ["rv1"]),
    ("CREATE OR ALTER VIEW [dbo].[Active Orders] AS SELECT 1;", ["active orders"]),
    ("CREATE VIEW ActiveOrders AS SELECT 1;", ["activeorders"]),
])
def test_view_names(ddl, expected):
    assert _names(ddl, "view") == expected


def test_routine_and_trigger_names_skip_if_not_exists():
    assert _names("CREATE PROCEDURE IF NOT EXISTS p1() BEGIN END;", "procedure") == ["p1"]
    assert _names("CREATE FUNCTION IF NOT EXISTS f1() RETURNS int RETURN 1;",
                  "function") == ["f1"]
    ddl = "CREATE TRIGGER IF NOT EXISTS tr1 AFTER INSERT ON `orders` FOR EACH ROW BEGIN END;"
    nodes, refs = _parse(ddl)
    assert [(n.kind, n.name) for n in nodes] == [("trigger", "tr1")]
    assert refs == [("tr1", "orders", 1)]


# ------------------------------------------------------------------ id stability

def test_ids_of_already_correct_tables_do_not_change():
    expected = make_id("r", "s.sql", "table", "orders")
    for ddl in ("CREATE TABLE orders (id int);",
                "CREATE TABLE Orders (id int);",
                "CREATE TABLE [dbo].[Orders] (id int);",
                "CREATE TABLE dbo.orders (id int);"):
        nodes, _ = _parse(ddl)
        assert [n.id for n in nodes] == [expected], ddl


def test_a_quoted_and_a_bare_spelling_share_one_id():
    ids = set()
    for ddl in ('CREATE TABLE "Orders" (id int);', "CREATE TABLE `orders` (id int);",
                "CREATE TABLE IF NOT EXISTS orders (id int);"):
        nodes, _ = _parse(ddl)
        ids |= {n.id for n in nodes}
    assert ids == {make_id("r", "s.sql", "table", "orders")}


@pytest.mark.parametrize("raw,expected", [
    ("orders", "orders"), ("Orders", "orders"), ("[Orders]", "orders"),
    ('"Orders"', "orders"), ("`Orders`", "orders"), ("[Order Details]", "order details"),
    (" [x] ", "x"),
])
def test_norm_name(raw, expected):
    assert _norm_name(raw) == expected


# ---------------------------------------------------------------- hostile input

@pytest.mark.timeout(5)
def test_an_unbalanced_quote_does_not_scan_to_the_end_of_the_file():
    for opener in ('"', "`", "["):
        ddl = f"CREATE TABLE {opener}" + "a " * 100_000
        start = time.perf_counter()
        nodes, _ = _parse(ddl)
        assert time.perf_counter() - start < 2.0, opener
        assert nodes == [], opener


@pytest.mark.timeout(5)
def test_many_definitions_with_quoted_names_stay_linear():
    ddl = "".join(f'CREATE TABLE IF NOT EXISTS "s"."Table {i}" (id int REFERENCES "s"."T"(id));\n'
                  for i in range(5000))
    start = time.perf_counter()
    nodes, refs = _parse(ddl)
    assert time.perf_counter() - start < 2.0
    assert len(nodes) == 5000 and len(refs) == 5000


# ------------------------------------------------------- embedded SQL (flow/data.py)

def _data(code: str):
    reads, writes = extract_data_refs("r", "a.py", code)
    return [t for _f, t, _p, _l in reads], [t for _f, t, _p, _l in writes]


def test_embedded_sql_with_quoted_and_qualified_names_reads_the_same_name_ddl_uses():
    code = ('cur.execute(\'SELECT * FROM "Orders" WHERE 1\')\n'
            'cur.execute("INSERT INTO [dbo].[Customers] (id) VALUES (1)")\n'
            'cur.execute("UPDATE `shipments` SET a = 1")\n'
            'cur.execute("DELETE FROM db.dbo.audit_log")\n')
    reads, writes = _data(code)
    assert reads == ["orders"]
    assert writes == ["customers", "shipments", "audit_log"]


def test_string_concatenation_is_not_read_as_a_quoted_table():
    code = ('q = "SELECT a FROM " + table + " WHERE 1"\n'
            'r = "UPDATE " + other + " SET a = 1"\n'
            "s = 'DELETE FROM ' + more + ' WHERE 1'\n")
    assert _data(code) == ([], [])
