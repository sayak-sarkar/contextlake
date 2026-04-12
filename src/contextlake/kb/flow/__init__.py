"""Flow-edge extraction: cross-repo runtime flow (HTTP, later events) on top of
the structural graph. Per-repo detectors emit edges to shared flow-surface nodes
(endpoints, topics); a two-hop join in :mod:`..arch.resolve` turns producer ⨝
consumer into directional repo→repo ``flow`` edges. All INFERRED — a deliberate,
honest undercount, never presented as ground truth.
"""
