from datetime import date

import pytest

from contextlake.kb.arch.resolve import repo_event_flow_edges
from contextlake.kb.flow.events import _useful, extract_event_flow, normalize_topic
from contextlake.kb.ids import make_id
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    yield s
    s.close()


def _rels(edges):
    return {(e.relation, e.dst) for e in edges}


def test_normalize_topic_strips_arn_url_and_lowercases():
    assert normalize_topic("arn:aws:sns:us-east-1:123:OrderCreated") == "ordercreated"
    assert normalize_topic(
        "https://sqs.us-east-1.amazonaws.com/123/Baggage-Queue") == "baggage-queue"
    assert normalize_topic("'order.events'") == "order.events"


def test_useful_guard_rejects_generic():
    assert _useful("order.events") and _useful("baggage-queue")
    assert not _useful("topic") and not _useful("ev") and not _useful("test")


def test_extract_kafka_publish_and_consume():
    src = b'''
producer.send("order.created", payload);
@KafkaListener(topics = "order.created")
public void onOrder(String m) {}
consumer.subscribe(["baggage.events"]);
'''
    nodes, edges = extract_event_flow("r", "svc.java", src, "java")
    topics = {n.name for n in nodes if n.kind == "topic"}
    assert "order.created" in topics and "baggage.events" in topics
    assert ("publishes_event", make_id("topic", "order.created")) in _rels(edges)
    assert ("consumes_event", make_id("topic", "order.created")) in _rels(edges)
    assert ("consumes_event", make_id("topic", "baggage.events")) in _rels(edges)
    assert all(e.confidence == Confidence.INFERRED for e in edges)


def test_extract_eventbridge_detailtype():
    src = b'eventbridge.put_events(DetailType="OrderShipped", Source="oms")'
    nodes, edges = extract_event_flow("r", "h.py", src, "python")
    assert ("publishes_event", make_id("topic", "ordershipped")) in _rels(edges)


def test_no_false_positive_on_bare_send():
    # a bare .send() with no messaging context must NOT be read as a publish
    nodes, edges = extract_event_flow("r", "x.js", b'res.send("hello world page");', "javascript")
    assert edges == [] and nodes == []


def test_event_flow_two_hop_resolves_publisher_to_consumer(store):
    topic = make_id("topic", "order.created")
    pub_file = make_id("repoP", "Producer.cs")
    con_file = make_id("repoC", "Consumer.cs")
    prov = Provenance(source_file="f", verified_at=date(2026, 6, 25))
    store.upsert_nodes("repoP", [
        Node(id=pub_file, repo="repoP", kind="file", name="Producer.cs"),
        Node(id=topic, repo="repoP", kind="topic", name="order.created")])
    store.upsert_nodes("repoC", [Node(id=con_file, repo="repoC", kind="file", name="Consumer.cs")])
    store.upsert_edges("repoP", [Edge(src=pub_file, dst=topic, relation="publishes_event",
                                      confidence=Confidence.INFERRED, provenance=prov)])
    store.upsert_edges("repoC", [Edge(src=con_file, dst=topic, relation="consumes_event",
                                      confidence=Confidence.INFERRED, provenance=prov)])
    flow = repo_event_flow_edges(store)
    assert len(flow) == 1
    e = flow[0]
    # the event flows from the publisher (repoP) to the consumer (repoC)
    assert e["src"] == "repoP" and e["dst"] == "repoC"
    assert e["relation"] == "flow" and e["context"] == "event" and e["weight"] == 1


def test_event_flow_ignores_same_repo(store):
    topic = make_id("topic", "internal.ping")
    f1, f2 = make_id("repoP", "P.cs"), make_id("repoP", "C.cs")
    prov = Provenance(source_file="f", verified_at=date(2026, 6, 25))
    store.upsert_nodes("repoP", [
        Node(id=f1, repo="repoP", kind="file", name="P.cs"),
        Node(id=f2, repo="repoP", kind="file", name="C.cs"),
        Node(id=topic, repo="repoP", kind="topic", name="internal.ping")])
    store.upsert_edges("repoP", [
        Edge(src=f1, dst=topic, relation="publishes_event",
             confidence=Confidence.INFERRED, provenance=prov),
        Edge(src=f2, dst=topic, relation="consumes_event",
             confidence=Confidence.INFERRED, provenance=prov)])
    assert repo_event_flow_edges(store) == []
