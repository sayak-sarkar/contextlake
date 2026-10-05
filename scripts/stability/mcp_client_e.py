"""Tier E MCP client: start `contextlake kb serve` over stdio, record the contract, call tools.

Run with the target venv's python, through the harness's run function
(`run(target, [this, ...], exe="python")`, `ci_runner.run` on CI), so the server inherits
the runner's environment. Portable: no symlinks, no shell.

    python mcp_client_e.py CONFIG PLAN_JSON OUT_JSON

PLAN_JSON is a list of calls::

    {"label": "find_callers.compute_tax", "tool": "find_callers",
     "args": {"name": "compute_tax"},
     "resolve": {"node_id": {"tool": "find_definition", "args": {"name": "x"},
                             "path": ["nodes", 0, "id"]}}}

`resolve` fills an argument from an earlier call's structured output before the call runs.
OUT_JSON gets the server instructions, every tool's description and schemas, and one record
per call: the arguments actually sent, `is_error`, the structured result (or the text parsed
as JSON), and the raw text. Nothing here judges a result; tier_e.py does.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

CALL_TIMEOUT = 60.0


def _server_exe() -> str:
    """The `contextlake` entry point next to this python, so the server is the target's.

    A venv keeps it in `Scripts\\contextlake.exe` on Windows and `bin/contextlake`
    elsewhere. `sys.executable` is not resolved: on Linux the venv python can be a symlink
    into a system directory that has no `contextlake`. No fallback to PATH, so the server
    can never be some other install.
    """
    here = Path(sys.executable).parent
    exe = here / ("contextlake.exe" if os.name == "nt" else "contextlake")
    if not exe.is_file():
        raise SystemExit(f"no contextlake entry point next to {sys.executable}: {exe}")
    return str(exe)


def _dig(obj, path):
    for key in path:
        obj = obj[key]
    return obj


def _structured(result):
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc
    text = "".join(getattr(c, "text", "") or "" for c in result.content)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _call(session, tool, args):
    r = await asyncio.wait_for(session.call_tool(tool, args), timeout=CALL_TIMEOUT)
    text = "".join(getattr(c, "text", "") or "" for c in r.content)
    return {"is_error": bool(r.is_error), "structured": _structured(r), "text": text[:20000]}


async def main(cfg: str, plan_path: str, out_path: str) -> int:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    server = _server_exe()
    params = StdioServerParameters(command=server, args=["kb", "serve", "--config", cfg],
                                   env=dict(os.environ), cwd=os.getcwd())
    record: dict = {"server_exe": server, "calls": {}}
    errlog_path = out_path + ".server-stderr.txt"
    with open(errlog_path, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as s:
                init = await asyncio.wait_for(s.initialize(), timeout=CALL_TIMEOUT)
                record["instructions"] = getattr(init, "instructions", None)
                info = getattr(init, "server_info", None)
                record["server"] = {"name": getattr(info, "name", None),
                                    "version": getattr(info, "version", None)}
                tools = (await asyncio.wait_for(s.list_tools(), timeout=CALL_TIMEOUT)).tools
                record["tools"] = [{"name": t.name, "description": t.description,
                                    "input_schema": getattr(t, "input_schema", None),
                                    "output_schema": getattr(t, "output_schema", None)}
                                   for t in tools]
                names = {t.name for t in tools}
                for step in plan:
                    label, tool = step["label"], step["tool"]
                    args = dict(step.get("args") or {})
                    entry: dict = {"tool": tool}
                    try:
                        for arg, how in (step.get("resolve") or {}).items():
                            got = await _call(s, how["tool"], how["args"])
                            args[arg] = _dig(got["structured"], how["path"])
                        entry["args"] = args
                        if tool not in names:
                            entry["skipped"] = f"tool {tool} is not registered on this server"
                        else:
                            entry.update(await _call(s, tool, args))
                    except Exception as e:  # noqa: BLE001 - record and keep going
                        entry["args"] = args
                        entry["client_error"] = f"{type(e).__name__}: {e}"[:500]
                    record["calls"][label] = entry
    Path(out_path).write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    print(f"tools={len(record['tools'])} calls={len(record['calls'])}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    raise SystemExit(asyncio.run(main(*sys.argv[1:])))
