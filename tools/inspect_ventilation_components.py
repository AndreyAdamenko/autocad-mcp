"""Print representative source blocks from embedded COM data in Blender."""
import base64
from collections import Counter
import json
from pathlib import Path
import sys
import zlib
import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocad_mcp.geometry import world_geometry
from autocad_mcp.ventilation_project import column_frames, inside_plan, local_point, visible_descendants

lines = zlib.decompress(base64.b85decode(bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string())).decode().splitlines()
records = [json.loads(line) for line in lines[1:] if line]
regions = json.loads((Path(__file__).resolve().parents[1]/'evidence/ventilation/project.json').read_text(encoding='utf-8'))['regions']
frames = column_frames(records, regions)
seen = Counter()
for r in records:
    g = r['geometry']
    if r['id'].count('/') != 1 or g['kind'] != 'block' or not inside_plan(r, frames['ground'], margin=200):
        continue
    name = g['effective_name']
    seen[name] += 1
    if seen[name] > 1 and name != 'A$C2D247BA6' or name.startswith(('ADSK', 'КТР1_')):
        continue
    children = visible_descendants(records, r)
    data = {'id':r['id'],'name':name,'properties':g.get('dynamic_properties'), 'insertion':local_point(g['insertion'],frames['ground']),
            'children':[]}
    for child in children:
        kind = child['geometry']['kind']
        info = {'id':child['id'],'kind':kind}
        if kind in ('line','arc','circle','polyline'):
            pts,edges,faces,closed=world_geometry(child,1)
            info.update(points=[local_point(p,frames['ground']) for p in pts],closed=closed)
            if len(pts)>20:
                info['points']=info['points'][::max(1,len(pts)//8)]
            if kind in ('arc','circle'):info['geometry']=child['geometry']
        data['children'].append(info)
    print(json.dumps(data,ensure_ascii=True))
print('ROOT COUNTS',json.dumps(seen,ensure_ascii=True))
