"""Read-only analysis of open COM contours embedded in the checked slab BLEND."""
import base64
import json
import sys
import zlib
from pathlib import Path
import bpy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.geometry import bounds,world_geometry

lines=zlib.decompress(base64.b85decode(bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string())).decode().splitlines()
origin=[291764.8963467055,26159.949071492534,0]
for line in lines[1:]:
    r=json.loads(line)
    g=r['geometry']
    if g['kind'] not in ('line','polyline') or g.get('closed'):
        continue
    pts,_,_,_=world_geometry(r)
    if not pts:
        continue
    box=bounds(pts)
    if box[0][1]>25000 and box[1][1]<40000 and box[0][0]>291000 and box[1][0]<322500:
        print(json.dumps({'id':r['id'],'layer':r['effective_layer'],'kind':g['kind'],
                          'points_local_mm':[[round(p[i]-origin[i],3) for i in range(3)] for p in pts]},ensure_ascii=True))
