"""Smoke test: launch the server over stdio and call READ-ONLY / dry-run tools only.

Usage (cwd ~/projects/bambu_mcp):  .venv/bin/python tests/stdio_smoke.py [tool[:json_args] ...]
Default: list tools, list_printers, printer_status a1mini, dry-run print_3mf, dry-run pause.
BAMBU_MCP_READONLY=* is forced so that no control command can ever be published from this script.
"""
import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SAMPLE = os.path.expanduser("~/solidcraft_work/a1mini_mount_v11/mount_v11_a1mini.3mf")
DEFAULT = [
    "list_printers",
    'printer_status:{"printer": "a1mini"}',
    "print_3mf:" + json.dumps(
        {"printer": "a1mini", "path": SAMPLE, "tray": 2, "expect_filament": "PLA",
         "expect_color": "FFFFFFFF", "dry_run": True}),
    'pause:{"printer": "a1mini", "dry_run": true}',
    "job_list",
]
ENV = {**os.environ, "BAMBU_MCP_READONLY": "*"}
PARAMS = StdioServerParameters(command=sys.executable, args=["-m", "bambu_mcp"], cwd=".", env=ENV)


async def main(calls):
    async with stdio_client(PARAMS) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print("TOOLS (%d):" % len(tools), ", ".join(t.name for t in tools))
            for c in calls:
                name, _, arg = c.partition(":")
                res = await s.call_tool(name, json.loads(arg) if arg else {})
                print(f"\n== {name} {arg[:100]} | isError={getattr(res, 'is_error', getattr(res, 'isError', None))}")
                for blk in res.content:
                    print(getattr(blk, "text", blk))


if __name__ == "__main__":
    anyio.run(main, sys.argv[1:] or DEFAULT)
