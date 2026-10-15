"""Inspect existing COM plan geometry at the three window marks lacking root blocks."""
import base64,json,sys,zlib
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from autocad_mcp.architecture import plan_frame,source_world_bounds
from autocad_mcp.geometry import world_geometry
bpy.ops.wm.open_mainfile(filepath=str(ROOT/'artifacts/erdaut_upper_plan.blend'),load_ui=False,use_scripts=False)
lines=zlib.decompress(base64.b85decode(bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string())).decode().splitlines()
records=[json.loads(line) for line in lines[1:] if line]
frame=plan_frame(records,'Model/E11F');origin=frame['origin_mm']
for x,y in [(4495,0),(10505,12900),(25050,12900)]:
    print('REGION',x,y)
    for r in records:
        if r['id'].count('/')!=1 or r['geometry']['kind'] not in ('line','polyline','block'):continue
        b=source_world_bounds(r)
        if not b:continue
        b=[[p[i]-origin[i] for i in (0,1)] for p in b]
        if b[0][0]<=x+900 and b[1][0]>=x-900 and b[0][1]<=y+600 and b[1][1]>=y-600:
            pts=world_geometry(r)[0] if r['geometry']['kind']!='block' else []
            print(json.dumps({'id':r['id'],'kind':r['geometry']['kind'],'layer':r['effective_layer'],'bbox':b,
                              'near_vertices':[[round(p[i]-origin[i],2) for i in (0,1)] for p in pts if abs(p[0]-origin[0]-x)<1500 and abs(p[1]-origin[1]-y)<600]},ensure_ascii=True))
