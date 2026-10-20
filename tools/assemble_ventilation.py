"""Call the MCP ventilation assembly against already captured COM sources."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]


async def run(args):
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'server.py')],cwd=str(ROOT),
        env=dict(os.environ,ACAD_MCP_READ_ONLY='1',ACAD_MCP_OUTPUT_ROOT=str(ROOT/'artifacts'),PYTHONUTF8='1'))
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=1000)) as client:
            await client.initialize()
            reply=await client.call_tool('assemble_ventilation_model',{'project_path':str(args.project.resolve()),'output':args.output})
            payload=json.loads(reply.content[0].text)
            args.report.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(payload,ensure_ascii=True))
            if not payload.get('ok'):raise ValueError('MCP assembly failed')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--report',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
