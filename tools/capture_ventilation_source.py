"""Capture selected original COM entities through a fresh MCP stdio process.

The selection JSON contains handles/evidence only, never mesh coordinates.
Original geometry is streamed to Blender and embedded in the resulting BLEND.
"""
import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    selected = json.loads(args.selection.read_text(encoding='utf-8'))
    if hashlib.sha256(Path(selected['source']).read_bytes()).hexdigest() != selected['sha256']:
        raise ValueError('Selected drawing hash changed before capture')
    env = dict(os.environ, ACAD_MCP_READ_ONLY='1', ACAD_MCP_EXPECTED_VERSION='19.1',
               ACAD_MCP_OUTPUT_ROOT=str(ROOT / 'artifacts'), PYTHONUTF8='1')
    bootstrap = '''import sys,runpy
from pathlib import Path
import pythoncom,win32com,win32com.client.gencache,win32com.gen_py
root=Path(sys.argv[1]).resolve().parent
cache=root/'evidence/ventilation/gen_py';cache.mkdir(parents=True,exist_ok=True)
win32com.__gen_path__=str(cache);win32com.gen_py.__path__=[str(cache)]
lib=pythoncom.LoadTypeLib(r'C:\\Program Files\\Common Files\\Autodesk Shared\\acax19enu.tlb')
a=lib.GetLibAttr();win32com.client.gencache.EnsureModule(a[0],a[1],a[3],a[4])
sys.path.insert(0,str(root));runpy.run_path(sys.argv[1],run_name='__main__')
'''
    params = StdioServerParameters(command=sys.executable, args=['-X', 'utf8', '-c', bootstrap, str(ROOT / 'server.py')],
                                  cwd=str(ROOT), env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=7200)) as client:
            await client.initialize()
            available = await client.list_tools()
            schema = next(t.inputSchema for t in available.tools if t.name == 'build_blender_model')
            if 'root_handles' not in schema['properties']:
                raise ValueError('MCP process does not expose proved root selections')
            print(f'MCP capture: {len(selected["root_handles"])} ModelSpace roots', flush=True)
            reply = await client.call_tool('build_blender_model', {
                'path': selected['source'], 'output': args.output, 'scope': 'model',
                'root_handles': selected['root_handles'], 'selection_evidence': selected['evidence'],
                'visible_only': selected.get('visible_only', False),
                'tolerance_mm': 0.25})
            payload = json.loads(reply.content[0].text)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(payload, ensure_ascii=True), flush=True)
            if not payload.get('ok'):
                raise ValueError('MCP capture failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection', required=True, type=Path)
    parser.add_argument('--output', required=True, help='New BLEND relative to artifacts')
    parser.add_argument('--report', required=True, type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
