"""Reopen the architectural BLEND and independently measure saved mesh objects."""
import base64
import hashlib
import json
import sys
import zlib
from pathlib import Path

import bpy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.architectural_worker import checks_for,digest
from autocad_mcp.blender_worker import actual_points
from autocad_mcp.geometry import bounds

raw=bpy.data.texts['Architectural reconstruction audit.json.zlib.base85'].as_string()
report=json.loads(zlib.decompress(base64.b85decode(raw)).decode())
by_id={o.get('element_id'):o for o in bpy.data.objects if o.get('element_id')}
objects=[(by_id[e['element_id']],e['expected_vertices_mm']) for e in report['elements']]
checks=checks_for(objects,report['verification']['tolerance_mm'])
overview_checks=[]
if report['project'].get('profile')=='overall_view':
    from collections import defaultdict,Counter
    from autocad_mcp.architecture import closed_footprint,plan_frame
    from autocad_mcp.overview import footprints,window_gaps
    # Re-derive product sizes and aperture positions from embedded ORIGINAL COM
    # records, independently of the modeller's expected-vertex ledger.
    encoded=''.join(bpy.data.texts['AutoCAD combined source provenance.json.zlib.base85'].as_string().split())
    packets=json.loads(zlib.decompress(base64.b85decode(encoded)).decode())
    product=next(r for r in packets['window']['records'] if r['id']=='Model/19346')
    product_box=bounds(closed_footprint(product))
    size=[product_box[1][i]-product_box[0][i] for i in (0,1)]
    groups=defaultdict(list)
    for obj in by_id.values():
        if obj.get('window_assembly'):groups[obj['window_assembly']].append(obj)
    tolerance=report['verification']['tolerance_mm']
    for floor,key,title in [(1,'ground','Model/2F228'),(2,'upper','Model/E11F')]:
        records=packets[key]['records'];frame=plan_frame(records,title)
        gaps=window_gaps(footprints(records,frame),frame['axis_spans_mm'])
        for number,gap in enumerate(gaps,1):
            identifier=f'Floor{floor}/OK1/{number}'
            vertices=[p for obj in groups.get(identifier,[]) for p in actual_points(obj)]
            box=bounds(vertices);axis=gap['axis']
            values=[box[1][axis]-box[0][axis],box[1][2]-box[0][2],box[0][axis],box[0][2]]
            expected=[size[0],size[1],gap['lo']+25,(floor-1)*4050+1000]
            error=max(abs(a-b) for a,b in zip(values,expected))
            overview_checks.append({'window':identifier,'measured_width_mm':values[0],'measured_height_mm':values[1],
                                    'source_position_size_error_mm':error,'passed':error<=tolerance})
    if len(groups)!=37 or len(overview_checks)!=37:raise ValueError('Saved window assembly count mismatch')
    if Counter(obj['category'] for obj in by_id.values())!=Counter(report['counts']):
        raise ValueError('Saved object counts mismatch')
    columns=[obj for obj in by_id.values() if obj.get('category')=='Columns']
    if len(columns)!=22:raise ValueError('Saved structural column count mismatch')
    highest=max(p[2] for obj in by_id.values() for p in actual_points(obj))
    if abs(highest-9670)>tolerance:raise ValueError('Saved overall top is not +9.670')
sources={s['path']:s['sha256'] for s in report['evidence_hashes']}
sources.update({s['source_path']:s['source_sha256'] for s in report['source_plans']})
hashes=[{'path':path,'unchanged':digest(path)==expected} for path,expected in sources.items()]
summary={'reopened':True,'blender_version':bpy.app.version_string,'objects':len(objects),
         'coordinate_checks_passed':all(c['passed'] for c in checks),'failures':[c for c in checks if not c['passed']],
         'max_coordinate_error_mm':max(c['coordinate_error_mm'] for c in checks),
         'source_files_unchanged':all(h['unchanged'] for h in hashes),'hashes':hashes,
         'slab_checks':[c for c in checks if c['category']=='Slabs'],
         'overview_source_window_checks':overview_checks,
         'overview_source_window_checks_passed':all(c['passed'] for c in overview_checks),
         'semantic_building_verified':False,'unresolved':report['unresolved']}
Path(bpy.data.filepath).with_suffix('.reopen-verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k not in ('hashes','unresolved')},ensure_ascii=True))
if not summary['coordinate_checks_passed'] or not summary['source_files_unchanged'] or not summary['overview_source_window_checks_passed']:
    raise ValueError('Saved architectural model failed verification')
