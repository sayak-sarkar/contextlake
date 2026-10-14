"""Author pseudonyms are keyed per export, per server start and per `kb wiki` run.

The pseudonym was 4 hex of an unkeyed SHA-256 of the email: 65,536 values, so anyone with a
list of candidate emails could match `Contributor a1b2` to a person, in every export. Now
it is an HMAC under the key of one run. The same person reads the same within the run and
differently across two. The keys here are fixed, so every comparison is deterministic.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import types
from argparse import Namespace

from contextlake.kb import anonymize
from contextlake.kb.anonymize import Anonymizer, using
from contextlake.kb.ownership import anon_author

K1, K2, K3 = b"\x01" * 16, b"\x02" * 16, b"\x03" * 16
EMAIL = "ada@example.invalid"


def _unkeyed(email: str) -> str:
    """The label every version before this one gave, for any reader holding the email."""
    return f"Contributor {hashlib.sha256(email.encode('utf-8')).hexdigest()[:4]}"


def test_the_label_is_keyed_and_follows_the_email():
    a, b = Anonymizer(key=K1), Anonymizer(key=K2)
    one, two = a.author_label("Ada", EMAIL), b.author_label("Ada", EMAIL)
    assert one.startswith("Contributor ") and len(one) == len("Contributor ") + 4
    assert one != two, "two runs gave one person the same label"
    assert a.author_label("Ada L.", EMAIL) == one, "a renamed author became a second person"
    assert _unkeyed(EMAIL) not in {one, two}


def test_anon_author_takes_the_given_key_then_the_active_one():
    assert anon_author("Ada", EMAIL, Anonymizer(key=K1)) == Anonymizer(key=K1).author_label(
        "Ada", EMAIL)
    with using(Anonymizer(key=K2)):
        assert anon_author("Ada", EMAIL) == Anonymizer(key=K2).author_label("Ada", EMAIL)


def test_with_no_key_in_force_the_process_key_is_random_and_kept(monkeypatch):
    """A direct library call has no export and no server. It must still never get the
    unkeyed hash, and two calls in one process must agree."""
    monkeypatch.setattr(anonymize, "_PROCESS", None)
    monkeypatch.setattr(anonymize, "os", types.SimpleNamespace(urandom=lambda n: K3))
    assert anonymize.active() is None
    label = anon_author("Ada", EMAIL)
    assert label == Anonymizer(key=K3).author_label("Ada", EMAIL)
    assert label != _unkeyed(EMAIL)
    assert anon_author("Ada", EMAIL) == label


# --- one `kb wiki` run ------------------------------------------------------------------

def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ada Lovelace", "GIT_AUTHOR_EMAIL": EMAIL,
           "GIT_COMMITTER_NAME": "Ada Lovelace", "GIT_COMMITTER_EMAIL": EMAIL,
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env=env)


def _setup(tmp_path):
    from datetime import date

    from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
    from contextlake.kb.state import check_schema
    from contextlake.kb.store.shards import GraphShard, write_shard
    from contextlake.kb.store.sqlite_store import SqliteStore

    clone = tmp_path / "r"
    (clone / "api").mkdir(parents=True)
    (clone / "api" / "svc.py").write_text("class ForecastService: ...\n")
    (clone / "main.py").write_text("def main(): ...\n")
    _git(clone, "init", "-q")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "first")

    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    (tmp_path / "kb.toml").write_text(
        f'[kb]\nstore_dir = "{store_dir.as_posix()}"\nanonymize = "always"\n',
        encoding="utf-8")
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="r", path=str(clone)))
    store.close()
    nodes = [Node(id="svc", repo="r", kind="class", name="ForecastService",
                  file="api/svc.py"),
             Node(id="main", repo="r", kind="entry_point", name="main", file="main.py")]
    edges = [Edge(src="main", dst="svc", relation="calls", confidence=Confidence.EXTRACTED,
                  provenance=Provenance(source_file="main.py", source_line=1,
                                        verified_at=date(2026, 10, 14)))]
    write_shard(store_dir, GraphShard(repo="r", head_commit="abc123", nodes=nodes,
                                      edges=edges))
    return store_dir


def _labels(text: str) -> set[str]:
    import re

    return set(re.findall(r"Contributor [0-9a-f]{4}", text))


def test_one_wiki_run_uses_one_key_and_the_next_run_a_new_one(tmp_path, monkeypatch):
    import contextlake.kb.cmds.wiki as wiki
    from contextlake.kb.cmds.wiki import _module_page_file, cmd_wiki

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store_dir = _setup(tmp_path)
    # The module plan is not under test: name the one module directly.
    monkeypatch.setattr(wiki, "_module_page_plan",
                        lambda store, repo_id, n, override=None: (
                            [{"prefix": "api", "nodes": 1}], False))
    keys = iter([K1, K2])
    monkeypatch.setattr(anonymize, "os", types.SimpleNamespace(urandom=lambda n: next(keys)))

    def run():
        assert cmd_wiki(Namespace(config=str(tmp_path / "kb.toml"))) == 0
        repo_page = (store_dir / "wiki" / "r.md").read_text(encoding="utf-8")
        module_page = _module_page_file(store_dir / "wiki", "r", "api").read_text(
            encoding="utf-8")
        return repo_page, module_page

    repo_page, module_page = run()
    first = _labels(repo_page)
    assert first, "control: the ownership section holds a pseudonym"
    assert _labels(module_page) == first, "the repo page and its module page disagree"
    for page in (repo_page, module_page):
        assert "Ada" not in page and EMAIL not in page
        assert _unkeyed(EMAIL) not in page
    assert first == {Anonymizer(key=K1).author_label("Ada Lovelace", EMAIL)}

    second = _labels(run()[0])
    assert second == {Anonymizer(key=K2).author_label("Ada Lovelace", EMAIL)}
    assert second != first, "the next run reused the previous run's key"
