import pytest

from contextlake.kb.flow.data import extract_data_refs
from contextlake.kb.ids import file_node_id

DAO = file_node_id("r", "dao.py")
DAO_A = file_node_id("r", "a.py")


def test_select_from_is_a_read():
    src = b'cursor.execute("SELECT id, status FROM orders WHERE id = ?", (id,))'
    reads, writes = extract_data_refs("r", "dao.py", src)
    assert reads == [(DAO, "orders", "dao.py", 1)]
    assert writes == []


def test_insert_update_delete_are_writes():
    src = b'''
db.exec("INSERT INTO orders (id) VALUES (1)")
db.exec("UPDATE orders SET status = 'shipped' WHERE id = 1")
db.exec("DELETE FROM orders WHERE id = 1")
'''
    reads, writes = extract_data_refs("r", "dao.py", src)
    assert reads == []
    # same table hit three ways in one file -> one write ref, not three
    assert writes == [(DAO, "orders", "dao.py", 2)]


def test_bracket_and_schema_qualified_names_normalize_like_sql_py():
    """Must match kb/sql.py's CREATE TABLE [dbo].[Orders] -> name 'orders' exactly,
    or a real read/write against a real table would never resolve."""
    src = b'cursor.execute("SELECT * FROM [dbo].[Orders]")'
    reads, _ = extract_data_refs("r", "q.py", src)
    assert reads[0][1] == "orders"


def test_a_select_with_no_from_within_range_is_not_matched():
    """The DOTALL '.*?' gap between SELECT and FROM is bounded so it can't leap
    over unrelated code to a later, unrelated FROM clause."""
    filler = "x" * 400
    src = f'run("SELECT name") \n{filler}\nother("FROM users")'.encode()
    reads, _ = extract_data_refs("r", "far.py", src)
    assert reads == []


def test_a_stray_select_does_not_hide_a_later_real_query():
    """A word "select" with no FROM near it (a `SELECT 1` health check, a comment) used to
    run its lazy `.*?` to the first FROM anywhere later in the file. When that was past the
    300-character bound the match was dropped, but the scan resumed AFTER it, so the real
    query inside the consumed span was never examined and its `reads` edge was missing."""
    src = ('ping = "SELECT 1"\n' + 'logging.info("doing the thing now")\n' * 20
           + 'rows = db.query("SELECT id FROM users")\n').encode()
    reads, _ = extract_data_refs("r", "a.py", src)
    assert reads == [(DAO_A, "users", "a.py", 22)]


def test_many_stray_selects_do_not_hide_a_later_real_query():
    src = ('x = "SELECT 1"\n' * 50 + 'rows = db.query("SELECT id FROM users")\n').encode()
    reads, _ = extract_data_refs("r", "a.py", src)
    assert [name for _f, name, _p, _l in reads] == ["users"]


@pytest.mark.parametrize("gap", [100, 290, 300, 301, 450])
def test_a_stray_select_never_hides_a_query_whatever_the_distance(gap):
    """The old code lost the query for any gap from 290 up: at 290 a stray's match spans 306
    characters (SELECT, the gap, FROM and the name), which was dropped after it had consumed
    the real query. The fixture checks its own distance, so the numbers cannot drift."""
    real = 'db.query("SELECT id FROM users")'
    # characters between the end of the stray `SELECT` and the real `FROM`
    fixed = len(' 1"\n') + len("z = \n") + len('db.query("SELECT id ')
    src = 'a = "SELECT 1"\n' + "z = " + "x" * (gap - fixed) + "\n" + real
    assert src.index("FROM") - (src.index("SELECT") + len("SELECT")) == gap
    reads, _ = extract_data_refs("r", "a.py", src.encode())
    assert [name for _f, name, _p, _l in reads] == ["users"]


def test_a_long_column_list_is_still_one_statement():
    """The bound must not be so tight that an ordinary wide query stops matching."""
    cols = ", ".join(f"col_{i:02d}" for i in range(35))        # 278 characters, under the bound
    src = f'cur.execute("SELECT {cols} FROM orders")'.encode()
    assert 270 < len(cols) < 300
    reads, _ = extract_data_refs("r", "q.py", src)
    assert [name for _f, name, _p, _l in reads] == ["orders"]


def test_different_tables_produce_separate_refs():
    src = b'''
a.execute("SELECT * FROM orders")
b.execute("SELECT * FROM invoices")
'''
    reads, _ = extract_data_refs("r", "dao.py", src)
    assert {name for _, name, _, _ in reads} == {"orders", "invoices"}


def test_no_sql_in_file_is_a_noop():
    assert extract_data_refs("r", "plain.py", b"def f():\n    return 1\n") == ([], [])


def test_commented_out_sql_is_not_a_write():
    """A dead `# DELETE FROM orders` in a data-access file must not assert a
    write that doesn't happen -- an honest miss, never a false write."""
    src = b'''
def f():
    # TODO: we used to do this
    # DELETE FROM orders WHERE id = 1
    pass
'''
    reads, writes = extract_data_refs("r", "dao.py", src)
    assert reads == [] and writes == []


def test_sql_inside_a_docstring_is_not_a_write():
    src = b'''
def f():
    """Legacy: INSERT INTO audit_log VALUES (1)"""
    pass
'''
    reads, writes = extract_data_refs("r", "dao.py", src)
    assert reads == [] and writes == []


def test_sql_inside_a_block_comment_is_not_a_read():
    src = b'''
def f():
    /* SELECT * FROM widgets */
    pass
'''
    reads, writes = extract_data_refs("r", "dao.py", src)
    assert reads == [] and writes == []


def test_a_real_query_after_a_stripped_comment_still_reports_the_right_line():
    """Comment-blanking must preserve newlines so line numbers on real matches
    downstream of a stripped comment don't shift."""
    src = b'''
def f():
    # DELETE FROM orders WHERE id = 1
    cursor.execute("SELECT * FROM widgets")
'''
    reads, writes = extract_data_refs("r", "dao.py", src)
    assert writes == []
    assert reads == [(DAO, "widgets", "dao.py", 4)]


def test_source_bytes_and_str_are_equivalent():
    src_bytes = b'x.execute("SELECT * FROM orders")'
    src_str = src_bytes.decode()
    assert extract_data_refs("r", "a.py", src_bytes) == extract_data_refs("r", "a.py", src_str)
