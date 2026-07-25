from contextlake.kb.flow.data import extract_data_refs


def test_select_from_is_a_read():
    src = b'cursor.execute("SELECT id, status FROM orders WHERE id = ?", (id,))'
    reads, writes = extract_data_refs("r", "dao.py", src)
    assert reads == [("r_dao_py", "orders", "dao.py", 1)]
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
    assert writes == [("r_dao_py", "orders", "dao.py", 2)]


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


def test_different_tables_produce_separate_refs():
    src = b'''
a.execute("SELECT * FROM orders")
b.execute("SELECT * FROM invoices")
'''
    reads, _ = extract_data_refs("r", "dao.py", src)
    assert {name for _, name, _, _ in reads} == {"orders", "invoices"}


def test_no_sql_in_file_is_a_noop():
    assert extract_data_refs("r", "plain.py", b"def f():\n    return 1\n") == ([], [])


def test_source_bytes_and_str_are_equivalent():
    src_bytes = b'x.execute("SELECT * FROM orders")'
    src_str = src_bytes.decode()
    assert extract_data_refs("r", "a.py", src_bytes) == extract_data_refs("r", "a.py", src_str)
