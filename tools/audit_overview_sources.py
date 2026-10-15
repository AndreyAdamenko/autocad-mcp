"""Audit original COM provenance inside final BLEND artifacts for overview assembly."""
import base64,json,sys,zlib
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from autocad_mcp.architecture import closed_footprint,plan_frame
from autocad_mcp.geometry import bounds,world_geometry
from autocad_mcp.overview import footprints,window_gaps,gap_bands,exterior_gaps

def source(file):
    bpy.ops.wm.open_mainfile(filepath=str(ROOT/'artifacts'/file),load_ui=False,use_scripts=False)
    lines=zlib.decompress(base64.b85decode(bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string())).decode().splitlines()
    return [json.loads(line) for line in lines[1:] if line]

result={}
for file,title in [('erdaut_checked.blend','Model/2F228'),('erdaut_upper_plan.blend','Model/E11F')]:
    records=source(file);frame=plan_frame(records,title);profiles=footprints(records,frame)
    from collections import Counter
    from autocad_mcp.architecture import in_frame
    external=[r for r in records if r.get('space')=='Model' and r.get('effective_layer','').startswith('KG_Стены_Наруж') and in_frame(r,frame)]
    print('external kinds',dict(Counter((r['geometry']['kind'],r['status']) for r in external)),flush=True)
    for r in external:
        if r['geometry']['kind']!='polyline' or not r['geometry'].get('closed'):
            print('NONCLOSED',r['id'],r['geometry']['kind'],r.get('error'),flush=True)
    gaps=window_gaps(profiles,frame['axis_spans_mm'])
    print('ALL GAPS',[(g['side'],round(g['lo'],2),round(g['hi'],2)) for g in exterior_gaps(profiles,frame['axis_spans_mm'])],flush=True)
    for gap in gaps:gap['bands']=gap_bands(profiles,gap)
    result[file]={'windows':gaps,'perimeter_profiles':[{'id':p['id'],'category':p['category'],'bbox':p['bbox']} for p in profiles if p['category']=='Unclassified construction']}
    if file in ('erdaut_checked.blend','erdaut_upper_plan.blend'):
        result[file]['entrance_candidates']=[p for p in footprints(records,frame,6000) if p['layer']=='0' and (p['bbox'][0][0]<-460 or p['bbox'][1][0]>30460 or p['bbox'][0][1]<-460 or p['bbox'][1][1]>13360)]
        paths=[]
        for r in records:
            if r['id'].count('/')!=1 or r['geometry']['kind']!='polyline' or not in_frame(r,frame,6000):continue
            points=world_geometry(r)[0]
            points=[[p[i]-frame['origin_mm'][i] for i in (0,1)] for p in points]
            b=[[min(p[i] for p in points) for i in (0,1)],[max(p[i] for p in points) for i in (0,1)]]
            center=[(b[0][i]+b[1][i])/2 for i in (0,1)]
            if center[0]<-460 or center[0]>30460 or center[1]<-460 or center[1]>13360:
                if 1000<max(b[1][i]-b[0][i] for i in (0,1))<6000:paths.append({'id':r['id'],'layer':r['effective_layer'],'bbox':b,'points':points,'closed':r['geometry'].get('closed')})
        result[file]['entrance_paths']=paths
    print(file,'windows',len(gaps),'with wall bands',sum(any(b['category']=='Walls' for b in g['bands']) for g in gaps),flush=True)
for file in ['spz_roof_source.blend','spz_frame_source.blend','spz_stairs_source.blend']:
    records=source(file);profiles=[];dimensions=[];axis_lines=[]
    for r in records:
        g=r['geometry']
        if g['kind']=='dimension':dimensions.append({'id':r['id'],'measurement':g.get('measurement'),'text':g.get('text_override'),'bbox':r.get('source_bbox')})
        if g['kind']=='line' and 'Оси' in r.get('effective_layer',''):axis_lines.append({'id':r['id'],'start':g['start'],'end':g['end']})
        if g['kind']!='polyline':continue
        try:points=closed_footprint(r)
        except ValueError:continue
        b=bounds(points)
        profiles.append({'id':r['id'],'layer':r['effective_layer'],'bbox':b,'sizes':[round(b[1][i]-b[0][i],4) for i in range(3)],'vertices':len(points),'closed_flag':g.get('closed')})
    result[file]={'profiles':profiles,'dimensions':dimensions,'axis_lines':axis_lines}
    print(file,'profiles',len(profiles),'dimensions',len(dimensions),flush=True)
(ROOT/'evidence/overview_source_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
