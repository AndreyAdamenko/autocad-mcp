"""Analyse COM records already embedded in a final BLEND; no DWG conversion."""
import base64
import json
import re
import sys
import zlib
from collections import Counter,defaultdict
from pathlib import Path

import bpy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.geometry import bounds,world_geometry,transform

text=bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string()
lines=zlib.decompress(base64.b85decode(text)).decode().splitlines()
manifest=json.loads(lines[0])['manifest']
records=[json.loads(line) for line in lines[1:] if line]
candidates=[]
titles=[]
blocks=[]
layers=Counter()
for r in records:
    if r.get('space')!='Model' or r.get('status')!='ok':
        continue
    g=r['geometry']
    if g['kind']=='block':
        blocks.append({'id':r['id'],'layer':r['effective_layer'],'geometry':g,'bbox':r.get('source_bbox'),'matrix':r['world_matrix']})
    if g['kind']=='text' and re.search('План|Кладочн|[1-6АБВ]$',g['text']):
        titles.append({'id':r['id'],'text':g['text'],'point':transform(r['world_matrix'],g['point'])})
    if g['kind']=='polyline':
        layers[r['effective_layer']]+=1
        if g.get('closed'):
            pts,_,_,_=world_geometry(r)
            box=bounds(pts)
            area=abs(sum(pts[i][0]*pts[(i+1)%len(pts)][1]-pts[(i+1)%len(pts)][0]*pts[i][1] for i in range(len(pts)))/2)
            candidates.append({'id':r['id'],'layer':r['effective_layer'],'bbox':box,'area_mm2':area,'vertices':len(pts)})
output=Path(sys.argv[sys.argv.index('--')+1])
report={'manifest':manifest,'closed_polylines':candidates,'polyline_layers':dict(layers),'titles':titles,'blocks':blocks}
output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'closed_polylines':len(candidates),'polyline_layers':dict(layers),'titles':titles},ensure_ascii=True))
