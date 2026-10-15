"""End-to-end MCP stdio acceptance against live AutoCAD and actual bpy.

Uses the selected DWG read-only. Refuses to overwrite the final BLEND.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocad_mcp.acad_core import MUTATING_TOOLS
from autocad_mcp.extraction import RecordAssembler


async def run(args):
    env = dict(os.environ, ACAD_MCP_READ_ONLY="1", ACAD_MCP_EXPECTED_VERSION="19.1",
               ACAD_MCP_OUTPUT_ROOT=str(ROOT/"artifacts"), PYTHONUTF8="1")
    params = StdioServerParameters(command=sys.executable, args=["-X","utf8",str(ROOT/"server.py")], cwd=str(ROOT), env=env)
    evidence = {"transport":"MCP stdio", "source":str(args.source), "checks":[]}
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=1800)) as client:
            init = await client.initialize()
            evidence["server"] = init.serverInfo.model_dump()
            async def call(name, arguments=None, expected=True):
                reply = await client.call_tool(name, arguments or {})
                payload = json.loads(reply.content[0].text)
                if payload["ok"] is not expected:
                    raise RuntimeError(f"{name}: {payload}")
                return payload.get("data", {})
            tools = await client.list_tools()
            names = [t.name for t in tools.tools]
            assert not set(names) & MUTATING_TOOLS
            evidence["tools"] = names
            await call("draw_line", {"start":[0,0],"end":[1,1]}, expected=False)
            evidence["checks"].append("write tool hidden and rejected by MCP")
            evidence["connection"] = await call("diagnose_compatibility")
            evidence["source_open"] = await call("open_source_drawing", {"path":str(args.source)})
            scan = await call("start_geometry_scan", {"path":str(args.source), "scope":"model"})
            assembler = RecordAssembler()
            batch = await call("read_geometry_batch", {"scan_id":scan["scan_id"],"page":0,"limit":5,"max_bytes":8192})
            records = assembler.accept(batch["fragments"])
            assert records
            evidence["sample_records"] = len(records)
            evidence["interpretation_sample"] = await call("interpret_geometry", {"records":records})
            await call("read_geometry_batch", {"scan_id":scan["scan_id"],"page":0}, expected=False)
            await call("close_geometry_scan", {"scan_id":scan["scan_id"]})
            evidence["checks"].append("bounded batch, interpretation and replay rejection")
            print("MCP protocol, read-only guards, live connection and geometry batch passed", flush=True)
            if args.build:
                print("Streaming DWG through COM -> MCP handler -> Blender stdin; waiting for final model", flush=True)
                evidence["blender"] = await call("build_blender_model", {"path":str(args.source),"output":args.build,"scope":"all"})
            if args.assemble:
                if not args.project:
                    raise ValueError('--assemble requires --project')
                evidence['blender']=await call('assemble_architectural_model',{'project_path':str(args.project.resolve()),'output':args.assemble})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence.get("blender", {"mcp_acceptance":True}), ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--build", help="New .blend relative to artifacts/")
    p.add_argument("--report", type=Path, default=ROOT/"evidence"/"mcp_acceptance.json")
    p.add_argument('--assemble',help='Architectural .blend output relative to artifacts/')
    p.add_argument('--project',type=Path)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
