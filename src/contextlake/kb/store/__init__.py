"""Storage backends for the knowledge layer.

A per-repo graph shard (node-link JSON) is the durable source of truth; the
SQLite store is a rebuildable cross-repo index over those shards.
"""
