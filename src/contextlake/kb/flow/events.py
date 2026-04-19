"""Event/messaging flow detection (Kafka/MSK, SNS, EventBridge).

Finds, per file, the message topics a repo **publishes** to and **consumes** from,
as edges to a shared ``topic`` node keyed by a normalised topic name — so a
producer in one repo and a consumer in another land on the same node. The
cross-repo join (``publishes_event ⨝ consumes_event``) lives in
:mod:`..arch.resolve` and yields directional ``publisher → consumer`` async flow.

Deliberately **high-precision**: only literal topic strings in a recognised
publish/subscribe context are matched. Topic names that live in config variables
are an honest *undercount* (they simply don't join), never a false link. Every
edge is ``INFERRED``.
"""

from __future__ import annotations

import re
from datetime import date

from ..ids import make_id
from ..model import Confidence, Edge, Node, Provenance

# Producer side: a function/file publishes to a named topic. Each regex captures
# the topic literal in group 1. Patterns require a messaging context (a kafka
# producer/template, .produce, EventBridge DetailType, SNS topic name) — never a
# bare .publish/.send, which would false-positive on UI events / HTTP.
_PUBLISH = [
    re.compile(r"(?:producer|kafkaTemplate|kafkaProducer|_producer|kafka)\s*\.\s*"
               r"send\(\s*['\"]([\w.\-/]{3,})['\"]"),
    re.compile(r"\.produce\(\s*['\"]([\w.\-/]{3,})['\"]"),
    re.compile(r"(?:DetailType|detail_type|detailType)\s*[=:]\s*['\"]([\w.\- /]{3,})['\"]"),
    re.compile(r"create_topic\(\s*Name\s*=\s*['\"]([\w.\-]{3,})['\"]"),
]
# Consumer side: a function/file subscribes to / listens on a named topic.
_CONSUME = [
    re.compile(r"@KafkaListener\([^)]*?['\"]([\w.\-]{3,})['\"]"),
    re.compile(r"\.[Ss]ubscribe\(\s*\[?\s*['\"]([\w.\-/]{3,})['\"]"),
    re.compile(r"@(?:EventPattern|MessagePattern)\(\s*['\"]([\w.\-]{3,})['\"]"),
]

_GENERIC = {"true", "false", "null", "none", "topic", "topics", "test", "queue",
            "string", "default", "event", "events", "message"}


def normalize_topic(raw: str) -> str:
    """Strip ARN/URL prefixes to the bare topic/queue name and lowercase it."""
    t = raw.strip().strip("'\"`")
    # arn:aws:sns:region:acct:NAME  /  https://sqs.../acct/NAME  -> NAME
    t = t.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return t.lower()


def _useful(topic: str) -> bool:
    return len(topic) >= 3 and topic not in _GENERIC


def extract_event_flow(repo_id: str, rel_path: str, source, lang: str,
                       verified_at: date | None = None) -> tuple[list[Node], list[Edge]]:
    """Topic nodes + ``publishes_event`` / ``consumes_event`` edges for one file."""
    text = source.decode("utf-8", "replace") if isinstance(source, (bytes, bytearray)) else source
    verified_at = verified_at or date.today()
    file_id = make_id(repo_id, rel_path)
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()

    def scan(patterns, relation):
        for rx in patterns:
            for m in rx.finditer(text):
                topic = normalize_topic(m.group(1))
                if not _useful(topic):
                    continue
                tid = make_id("topic", topic)
                if (relation, tid) in seen:
                    continue
                seen.add((relation, tid))
                nodes.append(Node(id=tid, repo=repo_id, kind="topic",
                                  name=topic, qualified_name=topic))
                edges.append(Edge(
                    src=file_id, dst=tid, relation=relation,
                    confidence=Confidence.INFERRED,
                    provenance=Provenance(source_file=rel_path,
                                          source_line=text.count("\n", 0, m.start()) + 1,
                                          verified_at=verified_at)))

    scan(_PUBLISH, "publishes_event")
    scan(_CONSUME, "consumes_event")
    return nodes, edges
