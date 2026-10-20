"""Reopen and measure ventilation, source-derived sections and preserved building.

Run with Blender opening the candidate BLEND. Reports coordination clashes and
open measured-body ports separately from numerical transfer failures.
"""
import base64
from collections import Counter
import json
import math
from pathlib import Path
import sys
import zlib

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.architectural_worker import checks_for,digest
from autocad_mcp.blender_worker import actual_points
from autocad_mcp.geometry import bounds
from autocad_mcp.ventilation import plain_text,section_from_text
from autocad_mcp.ventilation_project import block_body


def unpack(name):
    return json.loads(zlib.decompress(base64.b85decode(''.join(bpy.data.texts[name+'.zlib.base85'].as_string().split()))))


source=Path(bpy.data.filepath);before=digest(source)
audit=unpack('Architectural reconstruction audit.json');vent=audit['ventilation']
packets=unpack('AutoCAD combined source provenance.json');records=packets['ventilation']['records']
by_source={r['id']:r for r in records}
by_id={o['element_id']:o for o in bpy.data.objects if o.get('element_id')}
if len(by_id)!=len(audit['elements']):raise ValueError('Saved object/ledger completeness mismatch')
if Counter(o['category'] for o in by_id.values())!=Counter(audit['counts']):raise ValueError('Saved category counts mismatch')
checks=checks_for([(by_id[e['element_id']],e['expected_vertices_mm']) for e in audit['elements']],.25)
measurements=[]
for item in vent['elements']:
    rule=item['ventilation_rule'];kind=rule.get('kind')
    if kind not in ('rectangular_body','round_body'):continue
    root=by_source[rule['root']];frame=vent['frames'][item['floor']]
    actual=actual_points(by_id[item['element_id']]);section=item['section']
    ztext=plain_text(by_source[rule['level_source']]['geometry']['text']).replace(',','.')
    import re
    source_z=float(re.search(r'[+-]?\d+\.\d+',ztext).group())*1000
    body=block_body(records,root,frame,round_section=kind=='round_body')
    if kind=='rectangular_body':
        source_section=section_from_text(by_source[rule['height_source']]['geometry']['text'])
        xy_error=max(min(math.dist(p[:2],q[:2]) for q in body['polygon']) for p in actual)
        box=bounds(actual);z_error=abs(box[0][2]-source_z)
        section_error=max(abs(body['width_mm']-section['width_mm']),abs(box[1][2]-box[0][2]-source_section['height_mm']))
    else:
        count=len(actual)//2;rings=[actual[:count],actual[count:]]
        centres=[[sum(p[i] for p in ring)/len(ring) for i in range(3)] for ring in rings]
        xy_error=max(math.dist(p[:2],q[:2]) for p,q in zip(centres,body['path']))
        z_error=max(abs(p[2]-source_z) for p in centres)
        diameter=body['section']['diameter_mm']
        section_error=max(abs(2*math.dist(p,c)-diameter) for ring,c in zip(rings,centres) for p in ring)
    measurements.append({'element_id':item['element_id'],'xy_error_mm':xy_error,'elevation_error_mm':z_error,
                         'section_error_mm':section_error,'passed':max(xy_error,z_error,section_error)<=.25})

terminal_counts=Counter()
for item in vent['elements']:
    if item['ventilation_rule'].get('kind') in ('square_terminal','round_terminal'):
        terminal_counts[item['ventilation_rule']['kind']]+=1
if terminal_counts!={'square_terminal':28,'round_terminal':13}:
    raise ValueError('Diffuser counts differ from the selected plans/specification')
ahu=next(e for e in vent['elements'] if e['ventilation_rule'].get('kind')=='ahu')
ahu_box=bounds(actual_points(by_id[ahu['element_id']]))
ahu_error=max(abs((ahu_box[1][i]-ahu_box[0][i])-expected) for i,expected in enumerate((961,3378,986)))

# World-metre BVHs detect geometric surface intersections. A building overview
# has no engineered openings; these findings are coordination candidates.
geometry={}
for obj in by_id.values():
    vertices=[obj.matrix_world@v.co for v in obj.data.vertices]
    box=[[min(p[i] for p in vertices) for i in range(3)],[max(p[i] for p in vertices) for i in range(3)]]
    geometry[obj['element_id']]=(box,BVHTree.FromPolygons(vertices,[list(p.vertices) for p in obj.data.polygons]))


def intersects(a,b,allowance=0):
    return all(a[0][i]<=b[1][i]+allowance and b[0][i]<=a[1][i]+allowance for i in range(3))


structures=[o for o in by_id.values() if o['category'] in ('Walls','Columns','Beams','Slabs','Roofing','Parapets')]
core=[o for o in by_id.values() if o['category'].startswith('Ventilation') and o['category']!='Ventilation insulation']
clashes=[]
for obj in core:
    box,bvh=geometry[obj['element_id']]
    for structure in structures:
        other,tree=geometry[structure['element_id']]
        if intersects(box,other) and bvh.overlap(tree):
            clashes.append({'element_id':obj['element_id'],'building_element_id':structure['element_id'],
                            'building_category':structure['category'],'kind':'surface_intersection_requires_coordination'})

open_ports=[]
for item in vent['elements']:
    if item['ventilation_rule'].get('kind') not in ('rectangular_body','round_body'):continue
    for index,point in enumerate(item['ventilation_rule']['path_mm']):
        p=Vector([x/1000 for x in point]);nearest=[]
        for obj in core:
            if obj['element_id']==item['element_id']:continue
            box,tree=geometry[obj['element_id']]
            distance_box=math.sqrt(sum(max(box[0][i]-p[i],0,p[i]-box[1][i])**2 for i in range(3)))
            if distance_box>.6:continue
            hit=tree.find_nearest(p)
            if hit[0] is not None:nearest.append((hit[3]*1000,obj['element_id']))
        gap,other=min(nearest,default=(None,None),key=lambda entry:entry[0])
        # At an intersecting tee, a port may lie inside its neighbour. Ray
        # containment complements distance to the neighbour's exterior surface.
        connected=gap is not None and gap<=5
        if not connected:
            for obj in core:
                if obj['element_id']==item['element_id']:continue
                box,tree=geometry[obj['element_id']]
                if not all(box[0][i]-.0001<=p[i]<=box[1][i]+.0001 for i in range(3)):continue
                direction=Vector((.917,.303,.259)).normalized();origin=p+direction*.000001;crossings=0
                for _ in range(100):
                    location,normal,face,distance=tree.ray_cast(origin,direction,100)
                    if location is None:break
                    crossings+=1;origin=location+direction*.00001
                if crossings%2:connected=True;break
        if not connected:open_ports.append({'element_id':item['element_id'],'port':index,'point_mm':point,'nearest_gap_mm':gap,'nearest_element_id':other})

base_points={identifier:actual_points(obj) for identifier,obj in by_id.items() if not obj['category'].startswith('Ventilation')}
hashes={d['path']:d['sha256'] for d in packets['ventilation_catalogues']['drawings']}
manifest=vent['source_manifest'];hashes[manifest['source_path']]=manifest['source_sha256']
hash_checks=[{'path':path,'unchanged':digest(path)==expected} for path,expected in hashes.items()]
base=Path(__file__).resolve().parents[1]/'artifacts/erdaut_overall_view.blend'
if digest(base)!=vent['base_model_sha256']:raise ValueError('Architectural baseline file changed')
bpy.ops.wm.open_mainfile(filepath=str(base),load_ui=False,use_scripts=False)
baseline={o['element_id']:o for o in bpy.data.objects if o.get('element_id')}
if set(baseline)!=set(base_points):raise ValueError('Original architectural identity set changed')
preserved_error=max(math.dist(p,q) for identifier,obj in baseline.items() for p,q in zip(actual_points(obj),base_points[identifier]))
baseline_unchanged=len(baseline)==1121 and preserved_error<1e-6
result={'reopened':True,'blender_version':bpy.app.version_string,'source_blend':str(source),'source_blend_sha256':before,'objects':len(checks),
        'ventilation_objects':len(vent['elements']),'mesh_checks_passed':all(c['passed'] for c in checks),
        'max_coordinate_error_mm':max(c['coordinate_error_mm'] for c in checks),
        'source_derived_body_checks':measurements,'source_body_checks_passed':all(c['passed'] for c in measurements),
        'terminal_counts':dict(terminal_counts),'ahu_specification_size_error_mm':ahu_error,
        'architectural_objects_preserved':len(baseline),'architectural_geometry_unchanged':baseline_unchanged,
        'architectural_max_change_mm':preserved_error,'source_files_unchanged':all(c['unchanged'] for c in hash_checks),
        'source_hash_checks':hash_checks,'clashes':clashes,'clash_counts':dict(Counter(c['building_category'] for c in clashes)),
        'open_measured_body_ports':open_ports,'note':'Clashes and open ports require engineering coordination; dimensional transfer passes do not certify a fabrication model.',
        'full_fidelity':False,'engineering_coordination_complete':False,'failures':[c for c in checks if not c['passed']]}
result['passed']=result['mesh_checks_passed'] and result['source_body_checks_passed'] and baseline_unchanged and result['source_files_unchanged'] and ahu_error<=.25
if digest(source)!=before:raise ValueError('Verification modified candidate BLEND')
source.with_suffix('.reopen-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k not in ('source_derived_body_checks','source_hash_checks','clashes','open_measured_body_ports','failures')},ensure_ascii=True))
print('COORDINATION',json.dumps({'clash_count':len(clashes),'open_body_ports':len(open_ports)}))
if not result['passed']:raise ValueError('Ventilation reopening verification failed')
