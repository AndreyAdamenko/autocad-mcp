"""Read source linework around the main entry from embedded native COM records."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.architectural_worker import decode
from autocad_mcp.architecture import plan_frame
from autocad_mcp.geometry import world_geometry,bounds
import json
lines=decode('AutoCAD source provenance.jsonl').splitlines();records=[json.loads(s) for s in lines[1:] if s]
origin=plan_frame(records,'Model/2F228')['origin_mm']
for r in records:
    if r.get('space')!='Model' or r['geometry']['kind'] not in ('line','polyline'):continue
    p=world_geometry(r)[0]
    if not p:continue
    p=[[v[0]-origin[0],v[1]-origin[1]] for v in p];b=bounds([[*v,0] for v in p])
    if b[0][0]>12000 and b[1][0]<17000 and b[0][1]>-5000 and b[1][1]<-420:
        print(r['id'],r['effective_layer'],p,flush=True)
