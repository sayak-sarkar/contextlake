"""Built-in source: ingest the resources exposed by another MCP server.

Connects as an MCP *client* (stdio or streamable-HTTP transport), lists the server's
resources, and reads each as a document. Uses the ``mcp`` package (ships with the
``[kb]`` extra). A subprocess / network connection happens only when an ``mcp`` source
is actually configured, so the core stays offline-first.
"""

from __future__ import annotations

import asyncio

from .base import Document


def _texts(read_result) -> str:
    """Join the text parts of a ``read_resource`` result (skips binary blobs)."""
    parts = []
    for c in getattr(read_result, "contents", None) or []:
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


class McpSource:
    """Ingest the resources of an MCP server.

    Config (``[[sources]] type="mcp"``):
      - **stdio** transport: ``command`` (server executable) + ``args`` (list), ``env``
      - **http** transport:  ``url`` (a streamable-HTTP MCP endpoint)
      - ``timeout``: seconds for the whole read (default 60)
    """

    def __init__(self, command=None, args=None, url=None, env=None, timeout=60, **_):
        self.command = command
        self.args = list(args) if args else []
        self.url = url
        self.env = dict(env) if env else None
        self.timeout = int(timeout)

    def iter_documents(self):
        if not self.command and not self.url:
            return
        try:
            docs = asyncio.run(asyncio.wait_for(self._collect(), self.timeout))
        except Exception:  # noqa: BLE001 - an unreachable server yields nothing, never raises
            return
        yield from docs

    async def _collect(self):
        from mcp import ClientSession

        if self.url:
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self.url) as streams:
                return await self._read_all(ClientSession, streams[0], streams[1])

        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        async with stdio_client(params) as (read, write):
            return await self._read_all(ClientSession, read, write)

    @staticmethod
    async def _read_all(client_session_cls, read, write) -> list[Document]:
        out: list[Document] = []
        async with client_session_cls(read, write) as session:
            await session.initialize()
            resources = getattr(await session.list_resources(), "resources", None) or []
            for r in resources:
                uri = str(getattr(r, "uri", ""))
                try:
                    result = await session.read_resource(getattr(r, "uri", uri))
                except Exception:  # noqa: BLE001 - one unreadable resource must not abort
                    continue
                text = _texts(result)
                if not text:
                    continue
                out.append(Document(
                    id=uri, title=(getattr(r, "name", None) or uri), text=text, uri=uri,
                    attrs={"mimeType": getattr(r, "mimeType", None)}))
        return out
