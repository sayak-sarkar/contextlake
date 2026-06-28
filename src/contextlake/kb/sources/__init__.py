"""Pluggable document sources for RAG aggregation.

Common sources ship built-in and are configured with no code; anything else is a
loosely-coupled plugin. See :mod:`contextlake.kb.sources.base`.
"""

from .base import Document, Source, build_source, discover_sources

__all__ = ["Document", "Source", "build_source", "discover_sources"]
