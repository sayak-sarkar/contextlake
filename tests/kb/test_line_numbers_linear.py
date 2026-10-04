"""Line numbers in the SQL and flow extractors: right answers, and found without a rescan.

Every extractor used to compute a match's line as ``text.count("\\n", 0, pos) + 1``. That
reads ``pos`` characters per match, so a file with M matches and S characters costs M x S.
A 2.2 MB DDL file with 20,000 tables took 8.7 s in ``parse_sql``.

Two kinds of test live here:

* parity: each extractor reports the line a marker sits on, computed independently of the
  extractor, with LF and CRLF endings. These pass before and after the fix.
* method: no ``str.count`` call may run over the file text while extracting. This is a
  count of calls, not a stopwatch, so it cannot flake. One generous time bound backs it up.
"""

import sys
import time

import pytest

from contextlake.kb.flow.data import extract_data_refs
from contextlake.kb.flow.events import extract_event_flow
from contextlake.kb.flow.http import extract_http_flow
from contextlake.kb.flow.state import extract_state_flow
from contextlake.kb.flow.web import extract_web_flow
from contextlake.kb.sql import parse_sql

EOLS = ["\n", "\r\n"]


def _line_with(src: str, needle: str) -> int:
    """1-based line of the first line containing ``needle``, found by splitting on LF."""
    for i, line in enumerate(src.split("\n"), 1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in source")


def _bytes(src: str, eol: str) -> bytes:
    return src.replace("\n", eol).encode()


# --------------------------------------------------------------------------- parity

_SQL = """-- header comment
CREATE TABLE customers (
    id INT PRIMARY KEY
);

/* a block
   comment */
CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT REFERENCES customers(id)
);
GO
CREATE VIEW open_orders AS
    SELECT * FROM orders;
GO
CREATE PROCEDURE usp_close AS
    SELECT 1;
GO
CREATE TRIGGER trg_orders AFTER INSERT ON orders
BEGIN NULL; END;
/
"""


@pytest.mark.parametrize("eol", EOLS)
def test_sql_lines_match_the_marker_lines(eol):
    nodes, refs = parse_sql("r", "s.sql", _bytes(_SQL, eol))
    at = {(n.kind, n.name): n.line_start for n in nodes}
    assert at == {
        ("table", "customers"): _line_with(_SQL, "CREATE TABLE customers"),
        ("table", "orders"): _line_with(_SQL, "CREATE TABLE orders"),
        ("view", "open_orders"): _line_with(_SQL, "CREATE VIEW"),
        ("procedure", "usp_close"): _line_with(_SQL, "CREATE PROCEDURE"),
        ("trigger", "trg_orders"): _line_with(_SQL, "CREATE TRIGGER"),
    }
    assert sorted((target, line) for _src, target, _path, line in refs) == sorted([
        ("customers", _line_with(_SQL, "REFERENCES customers")),
        ("orders", _line_with(_SQL, "AFTER INSERT")),
    ])


_HTTP = """import requests
from fastapi import FastAPI
app = FastAPI()

@app.get('/api/orders/{order_id}')
def get_order(order_id):
    return requests.post('https://pay.example.com/api/payments/charge')

@app.delete('/api/customers/{cid}')
def drop(cid):
    pass
"""


@pytest.mark.parametrize("eol", EOLS)
def test_http_lines_match_the_marker_lines(eol):
    _nodes, edges = extract_http_flow("r", "a.py", _bytes(_HTTP, eol), "python")
    got = [(e.relation, e.context, e.provenance.source_line) for e in edges]
    assert got == [
        ("exposes", "GET", _line_with(_HTTP, "@app.get")),
        ("exposes", "DELETE", _line_with(_HTTP, "@app.delete")),
        ("calls_http", "POST", _line_with(_HTTP, "requests.post")),
    ]


_NEXT_ROUTE = """import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json([]);
}

export async function POST(req) {
  return NextResponse.json({});
}
"""


@pytest.mark.parametrize("eol", EOLS)
def test_nextjs_handler_lines_match_the_marker_lines(eol):
    _nodes, edges = extract_http_flow(
        "r", "app/api/orders/route.ts", _bytes(_NEXT_ROUTE, eol), "typescript")
    got = [(e.context, e.provenance.source_line) for e in edges]
    assert got == [
        ("GET", _line_with(_NEXT_ROUTE, "function GET")),
        ("POST", _line_with(_NEXT_ROUTE, "function POST")),
    ]


_EVENTS = """from kafka import KafkaProducer
producer = KafkaProducer()

def publish(order):
    producer.send('orders.created.v1', order)

consumer.subscribe(['payments.settled.v1'])
"""


@pytest.mark.parametrize("eol", EOLS)
def test_event_lines_match_the_marker_lines(eol):
    _nodes, edges = extract_event_flow("r", "a.py", _bytes(_EVENTS, eol), "python")
    got = [(e.relation, e.provenance.source_line) for e in edges]
    assert got == [
        ("publishes_event", _line_with(_EVENTS, "producer.send")),
        ("consumes_event", _line_with(_EVENTS, "consumer.subscribe")),
    ]


_STATE = """class Order:
    def pay(self, order):
        if order.status == Created:
            order.status = Paid
        return 1

    def ship(self, order):
        if order.status == Paid:
            order.status = Shipped
"""


@pytest.mark.parametrize("eol", EOLS)
def test_state_lines_match_the_marker_lines(eol):
    nodes, edges = extract_state_flow("r", "o.py", _bytes(_STATE, eol), "python")
    name_of = {n.id: n.name for n in nodes}
    first_if = _line_with(_STATE, "== Created")
    second_if = _line_with(_STATE, "== Paid")
    transitions = [(name_of[e.src], name_of[e.dst], e.provenance.source_line)
                   for e in edges if e.relation == "transitions_to"]
    assert transitions == [("Created", "Paid", first_if), ("Paid", "Shipped", second_if)]
    # a state node cites the line of the guard that first introduced it
    contained = {name_of[e.dst]: e.provenance.source_line
                 for e in edges if e.relation == "contains"}
    assert contained == {"Created": first_if, "Paid": first_if, "Shipped": second_if}


_DATA = """def load(cur):
    cur.execute("SELECT id FROM customers WHERE 1=1")
    cur.execute("INSERT INTO orders (id) VALUES (1)")
    cur.execute("UPDATE shipments SET id = 2")
    cur.execute("DELETE FROM audit_log")
"""


@pytest.mark.parametrize("eol", EOLS)
def test_data_lines_match_the_marker_lines(eol):
    reads, writes = extract_data_refs("r", "a.py", _bytes(_DATA, eol))
    assert [(t, line) for _f, t, _p, line in reads] == [
        ("customers", _line_with(_DATA, "SELECT")),
    ]
    assert [(t, line) for _f, t, _p, line in writes] == [
        ("orders", _line_with(_DATA, "INSERT")),
        ("shipments", _line_with(_DATA, "UPDATE")),
        ("audit_log", _line_with(_DATA, "DELETE")),
    ]


_WEB = """export const App = () => (
  <Routes>
    <Route path="/orders" element={<Orders />}>
      <Route path=":id" element={<OrderPage />} />
    </Route>
    {/* <Route path="/dead" element={<Dead />} /> */}
    <Route path="/settings" element={<Settings />} />
  </Routes>
);
"""


@pytest.mark.parametrize("eol", EOLS)
def test_web_route_lines_match_the_marker_lines(eol):
    _nodes, edges = extract_web_flow("r", "App.tsx", _bytes(_WEB, eol), "tsx")
    got = {e.context: e.provenance.source_line for e in edges}
    assert got == {
        "Orders": _line_with(_WEB, 'path="/orders"'),
        "OrderPage": _line_with(_WEB, 'path=":id"'),
        "Settings": _line_with(_WEB, 'path="/settings"'),
    }


# ------------------------------------------------------------------------ method

_MATCHES = 300


def _sql_big() -> bytes:
    return "".join(
        f"CREATE TABLE t{i} (\n  id INT,\n  p INT REFERENCES t{i - 1}(id)\n);\n"
        for i in range(1, _MATCHES + 1)).encode()


def _http_big() -> str:
    return "".join(f"@app.get('/api/v1/res{i}/items')\ndef h{i}():\n    pass\n"
                   for i in range(_MATCHES))


def _next_big() -> str:
    # a route file holds one handler per verb, so the matches are few and the file is long
    return "".join(f"export async function {v}() {{}}\n"
                   for v in ("GET", "POST", "PUT", "DELETE", "PATCH")) + "\n" * 2000


def _events_big() -> str:
    return "".join(f"producer.send('topic.number{i}.events', x)\n" for i in range(_MATCHES))


def _state_big() -> str:
    return "class Order:\n" + "".join(
        f"    def m{i}(self, o):\n        if o.status == S{i}:\n            o.status = T{i}\n"
        for i in range(_MATCHES))


def _data_big() -> str:
    return "".join(f'cur.execute("INSERT INTO tbl{i} VALUES (1)")\n' for i in range(_MATCHES))


def _web_big() -> str:
    return "".join(f'<Route path="/page{i}" element={{<P{i} />}} />\n' for i in range(_MATCHES))


_BIG = {
    "sql": lambda: parse_sql("r", "s.sql", _sql_big()),
    "http": lambda: extract_http_flow("r", "a.py", _http_big(), "python"),
    "nextjs": lambda: extract_http_flow("r", "app/api/x/route.ts", _next_big(), "typescript"),
    "events": lambda: extract_event_flow("r", "a.py", _events_big(), "python"),
    "state": lambda: extract_state_flow("r", "o.py", _state_big(), "python"),
    "data": lambda: extract_data_refs("r", "a.py", _data_big()),
    "web": lambda: extract_web_flow("r", "App.tsx", _web_big(), "tsx"),
}


def _str_count_calls_on_big_text(fn, min_len: int = 2000) -> int:
    """How many times ``fn`` calls ``str.count`` on a string of at least ``min_len`` chars.

    ``sys.setprofile`` sees C calls, so this needs no cooperation from the code under
    test. The size filter keeps unrelated short-string counting out of the number.
    """
    calls = 0

    def profiler(_frame, event, arg):
        nonlocal calls
        if event == "c_call" and getattr(arg, "__name__", "") == "count":
            owner = getattr(arg, "__self__", None)
            if isinstance(owner, str) and len(owner) >= min_len:
                calls += 1

    sys.setprofile(profiler)
    try:
        fn()
    finally:
        sys.setprofile(None)
    return calls


def test_the_count_probe_sees_a_count_call():
    # Positive control. If an interpreter did not report C calls to `sys.setprofile`, every
    # method test below would pass on the old code too and prove nothing.
    probe = _str_count_calls_on_big_text(lambda: ("x\n" * 2000).count("\n", 0, 3000))
    assert probe == 1


@pytest.mark.parametrize("name", sorted(_BIG))
def test_no_extractor_counts_newlines_over_the_whole_file_per_match(name):
    run = _BIG[name]
    # the input must produce output, or a count of zero would prove nothing
    assert any(len(part) for part in run())
    assert _str_count_calls_on_big_text(run) == 0


def test_ddl_with_20000_tables_is_parsed_in_linear_time():
    # The unfixed code took 8.7 s here. The fixed code takes well under 1 s. The bound is
    # loose on purpose: the count test above is the one that decides, this one shows the
    # result a user sees.
    ddl = "".join(
        f"CREATE TABLE t{i} (\n  id INT PRIMARY KEY,\n  a VARCHAR(40),\n"
        f"  b VARCHAR(40),\n  p INT REFERENCES t{max(i - 1, 0)}(id)\n);\n\n"
        for i in range(20000)).encode()
    assert len(ddl) > 2_000_000
    start = time.perf_counter()
    nodes, refs = parse_sql("r", "big.sql", ddl)
    elapsed = time.perf_counter() - start
    assert len(nodes) == 20000 and len(refs) == 19999
    assert elapsed < 3.0, f"{elapsed:.1f}s"
