"""Audit building blocks from final BLEND COM provenance; no geometry transfer file."""
import base64
import json
import re
import sys
import zlib
from pathlib import Path
import bpy

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from autocad_mcp.architecture import in_frame,plan_frame,source_world_bounds

results=[]
for file,title in [('erdaut_checked.blend','Model/2F228'),('erdaut_upper_plan.blend','Model/E11F')]:
    bpy.ops.wm.open_mainfile(filepath=str(ROOT/'artifacts'/file),load_ui=False,use_scripts=False)
    lines=zlib.decompress(base64.b85decode(bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string())).decode().splitlines()
    manifest=json.loads(lines[0])['manifest']
    records=[json.loads(line) for line in lines[1:] if line]
    frame=plan_frame(records,title)
    marks=[]
    for r in records:
        g=r['geometry']
        if g['kind']=='text' and in_frame(r,frame) and re.search(r'[ОO][КK]\s*-\s*1',g.get('text',''),re.I):
            from autocad_mcp.geometry import transform
            p=transform(r['world_matrix'],g['point'])
            marks.append({'id':r['id'],'text':g['text'],'local_xy_mm':[round(p[i]-frame['origin_mm'][i],2) for i in (0,1)],
                          'geometry':g,'bbox':source_world_bounds(r)})
    blocks=[]
    for r in records:
        g=r['geometry']
        if r.get('space')!='Model' or g['kind']!='block' or r['id'].count('/')!=1 or not in_frame(r,frame):
            continue
        bbox=r.get('source_bbox')
        attrs=[{'id':a['id'],'text':a['geometry'].get('text'),'tag':a['geometry'].get('tag')} for a in records if a['id'].startswith(r['id']+'/') and a['geometry']['kind']=='text']
        blocks.append({'id':r['id'],'layer':r.get('effective_layer'),'name':g.get('name'),'effective_name':g.get('effective_name'),
                       'local_bbox_mm':[[round(p[i]-(frame['origin_mm'][i] if i<2 else 0),4) for i in range(3)] for p in bbox] if bbox else None,
                       'geometry_properties':g,'attributes':attrs})
    variants=[]
    for r in records:
        if r['geometry']['kind']=='text' and 'Кладочн' in r['geometry'].get('text',''):
            try:
                other=plan_frame(records,r['id'])
                windows=[x['id'] for x in records if x['geometry']['kind']=='block' and 'VS_022' in x['geometry'].get('effective_name','') and in_frame(x,other)]
                variants.append({'title_id':r['id'],'window_count':len(windows),'window_ids':windows,'origin_mm':other['origin_mm']})
            except ValueError as exc:
                from autocad_mcp.geometry import transform
                title_record=next(x for x in records if x['id']==title)
                a=transform(r['world_matrix'],r['geometry']['point'])
                b=transform(title_record['world_matrix'],title_record['geometry']['point'])
                other={**frame,'origin_mm':[frame['origin_mm'][i]+a[i]-b[i] for i in (0,1)]}
                windows=[{'id':x['id'],'name':x['geometry'].get('effective_name'),'bbox':source_world_bounds(x)} for x in records if x['geometry']['kind']=='block' and 'VS_022' in x['geometry'].get('effective_name','') and in_frame(x,other)]
                variants.append({'title_id':r['id'],'error':str(exc),'translated_candidate_origin':other['origin_mm'],'candidate_windows':windows})
    results.append({'source':manifest['source_path'],'frame':{k:v for k,v in frame.items() if k!='columns'},'blocks':blocks,'window_marks':marks,'masonry_variants':variants})
(ROOT/'evidence/building_blocks_audit.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
for result in results:
    from collections import Counter
    print(json.dumps({'source':result['source'],'count':len(result['blocks']),
                      'types':dict(Counter(b['effective_name'] for b in result['blocks']))},ensure_ascii=True))
