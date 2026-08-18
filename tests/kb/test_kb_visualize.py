

def test_a_wiki_partition_is_not_counted_as_a_repository():
    """Running `kb wiki` once doubled the fleet count on the graph overview.

    A three-repository store rendered "6 repos with a parsed graph", with `@wiki:*` entries
    listed beside the real ones and each linked to its own page. The predicate excluded the
    `(shared)` / `(packages)` sentinels and stopped, so the partitions written beside a repo
    -- `@wiki:`, `@connect:`, `@enrich:`, `@ingest:` -- passed straight through.

    The correct count existed a few lines away the whole time: `kb lint` and the dashboard's
    own `data.json` both said 3.
    """
    from contextlake.kb.visualize.payload import _is_not_a_real_repo

    for real in ("github.com/pallets/click", "click", "team/api"):
        assert not _is_not_a_real_repo(real), real
    for pseudo in ("(shared)", "(packages)", "(external)",
                   "@wiki:click", "@wiki:click::mod", "@connect:team/api",
                   "@enrich:team/api", "@ingest:handbook"):
        assert _is_not_a_real_repo(pseudo), pseudo


def test_repo_node_sizes_excludes_every_pseudo_repo(tmp_path):
    """The docstring said "real repos only" and the filter did not deliver it.

    Exercised through the real query rather than the predicate alone, because the defect was
    that this function's caller trusted a claim the function did not keep.
    """
    from contextlake.kb.store.sqlite_store import SqliteStore
    from contextlake.kb.visualize.payload import repo_node_sizes

    store = SqliteStore(tmp_path / "k.sqlite")
    try:
        store.conn.executemany(
            "INSERT INTO nodes (node_id, repo_id, kind, name) VALUES (?,?,?,?)",
            [("n1", "team/api", "function", "a"),
             ("n2", "@wiki:team/api", "doc", "page"),
             ("n3", "(packages)", "package", "requests"),
             ("n4", "@ingest:handbook", "doc", "h")])
        store.conn.commit()
        assert set(repo_node_sizes(store)) == {"team/api"}
    finally:
        store.close()
