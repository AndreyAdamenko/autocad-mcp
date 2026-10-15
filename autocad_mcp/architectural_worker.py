"""Assemble above-ground wall/column geometry from verified COM provenance.

Inputs are earlier final BLEND artifacts, not DWG conversions. Their embedded
COM records and original DWG hashes are checked. Unresolved parts are reported.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import zlib
from collections import Counter
from pathlib import Path

import bpy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.architecture import classify_layer,closed_footprint,in_frame,inside_polygon,local_footprint,orthogonal_slab,plan_frame,validate_project
from autocad_mcp.blender_worker import actual_points,collection,extrude,mesh_object,render_controls
from autocad_mcp.geometry import bounds,cross,norm,transform


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def decode(name):
    if name in bpy.data.texts:
        return bpy.data.texts[name].as_string()
    return zlib.decompress(base64.b85decode(bpy.data.texts[name+'.zlib.base85'].as_string())).decode()


def checks_for(objects,tolerance):
    bpy.context.view_layer.update()
    checks=[]
    for obj,expected in objects:
        observed=actual_points(obj)
        error=max((norm([p[i]-q[i] for i in range(3)]) for p,q in zip(expected,observed)),default=0)
        box=bounds(observed)
        height=box[1][2]-box[0][2]
        edge_uses=Counter()
        for face in obj.data.polygons:
            v=list(face.vertices)
            for i in range(len(v)):
                edge_uses[tuple(sorted((v[i],v[(i+1)%len(v)])))]+=1
        manifold=bool(edge_uses) and all(n==2 for n in edge_uses.values())
        check={'element_id':obj['element_id'],'source_id':obj['source_id'],'category':obj['category'],
               'coordinate_error_mm':error,'height_error_mm':abs(height-obj['height_mm']),
               'closed_manifold':manifold,'vertices':len(observed)}
        check['passed']=len(expected)==len(observed) and error<=tolerance and check['height_error_mm']<=tolerance and manifold
        if 'expected_volume_mm3' in obj:
            signed=0;surface_area=0
            anchor=observed[0]
            for face in obj.data.polygons:
                v=list(face.vertices)
                for i in range(1,len(v)-1):
                    a,b,c=[observed[k] for k in (v[0],v[i],v[i+1])]
                    surface_area+=norm(cross([b[k]-a[k] for k in range(3)],[c[k]-a[k] for k in range(3)]))/2
                    a,b,c=[[p[k]-anchor[k] for k in range(3)] for p in (a,b,c)]
                    signed+=sum(x*y for x,y in zip(a,cross(b,c)))/6
            volume_error=abs(abs(signed)-obj['expected_volume_mm3'])
            check['volume_error_mm3']=volume_error
            # Bound volume roundoff by the measured vertex error and surface
            # area. A fixed 1 ppm volume threshold alone can reject a sound
            # thin slab whose saved float32 height is accurate to 0.001 mm.
            volume_tolerance=max(1000,obj['expected_volume_mm3']*1e-6,2*surface_area*max(error,1e-6))
            check['volume_tolerance_mm3']=volume_tolerance
            check['passed'] &= volume_error<=volume_tolerance
            from mathutils import Vector
            inverse=obj.matrix_world.inverted()
            holes=json.loads(obj['hole_rectangles_mm'])
            open_holes=[]
            for hole in holes:
                point=Vector(((hole[0][0]+hole[1][0])/2000,(hole[0][1]+hole[1][1])/2000,
                              (obj['base_z_mm']+obj['height_mm']+1000)/1000))
                hit,*_=obj.ray_cast(inverse@point,Vector((0,0,-1)),distance=(obj['height_mm']+2000)/1000)
                open_holes.append(not hit)
            check['through_openings']=len(open_holes)
            check['all_source_openings_clear']=all(open_holes)
            check['passed'] &= all(open_holes)
        checks.append(check)
    return checks


def main():
    args=sys.argv[sys.argv.index('--')+1:]
    project=validate_project(json.loads(Path(args[0]).read_text(encoding='utf-8')))
    target=Path(args[1]).resolve()
    if target.exists() or target.suffix!='.blend':
        raise ValueError('Output must be a new BLEND')
    tolerance=project.get('tolerance_mm',0.25)
    evidence_hashes=[]
    for source in project['evidence_drawings']:
        actual=digest(source['path'])
        if actual!=source['sha256']:
            raise ValueError('Evidence DWG changed: '+source['path'])
        evidence_hashes.append({'path':source['path'],'sha256':actual,'unchanged':True})
    sources=[]
    for floor in project['floors']:
        bpy.ops.wm.open_mainfile(filepath=str(Path(floor['source_blend']).resolve()),load_ui=False,use_scripts=False)
        lines=decode('AutoCAD source provenance.jsonl').splitlines()
        manifest=json.loads(lines[0])['manifest']
        records=[json.loads(line) for line in lines[1:] if line]
        if not manifest.get('readonly') or manifest.get('mm_per_unit')!=1:
            raise ValueError('Only verified read-only millimetre COM records are accepted')
        if digest(manifest['source_path'])!=manifest['source_sha256']:
            raise ValueError('Plan DWG changed since live COM extraction')
        frame=plan_frame(records,floor['title_id'])
        sources.append((floor,manifest,records,frame))
    slab_sources=[]
    for slab in project.get('slabs',[]):
        bpy.ops.wm.open_mainfile(filepath=str(Path(slab['source_blend']).resolve()),load_ui=False,use_scripts=False)
        lines=decode('AutoCAD source provenance.jsonl').splitlines()
        manifest=json.loads(lines[0])['manifest']
        records=[json.loads(line) for line in lines[1:] if line]
        if not manifest.get('readonly') or digest(manifest['source_path'])!=manifest['source_sha256']:
            raise ValueError('Slab source DWG changed or lacks read-only provenance')
        slab_sources.append((slab,manifest,records))
    first_grid=sources[0][3]['grid_mm']
    if any(source[3]['grid_mm']!=first_grid for source in sources[1:]):
        raise ValueError('Floor column grids do not match')
    bpy.ops.wm.read_factory_settings(use_empty=False)
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    scene=bpy.context.scene
    scene.unit_settings.system='METRIC'
    scene.unit_settings.scale_length=1
    scene['datum_absolute_m']=project['datum_absolute_m']
    scene['semantic_building_verified']=False
    model=collection('Architectural reconstruction')
    parent_columns=collection('Columns 400x400',model)
    objects,ledger,issues=[],[],[]
    counts=Counter()
    colors={'Walls':(0.82,0.78,0.64,1),'Facade brick':(0.65,0.45,0.25,1),
            'Insulation':(0.7,0.55,0.8,1),'Unclassified construction':(0.95,0.55,0.15,1),'Columns':(0.55,0.62,0.68,1)}

    def add(points,category,source,floor,base,top,parent,interpretation):
        source_id=source['id']
        rule={'category':category,'height_mm':{'value':top-base},'base_z_mm':{'value':base}}
        obj,expected=extrude(points,rule,parent,source_id,tolerance)
        obj['element_id']=floor['name']+'/'+source_id
        obj['source_id']=source_id
        obj['category']=category
        obj['interpretation']=interpretation
        obj.color=colors[category]
        objects.append((obj,expected))
        ledger.append({'element_id':obj['element_id'],'source_id':source_id,'source_blend':floor['source_blend'],
                       'category':category,'expected_vertices_mm':expected,'base_mm':base,'top_mm':top,
                       'interpretation':interpretation})
        counts[category]+=1

    for index,(floor,manifest,records,frame) in enumerate(sources):
        floor_parent=collection(floor['name'],model)
        parents={}
        base,top=floor['base']['value_mm'],floor['wall_top']['value_mm']
        for record in records:
            g=record['geometry']
            if record.get('space')!='Model' or record.get('status')!='ok' or not in_frame(record,frame):
                continue
            category=classify_layer(record.get('effective_layer',''))
            if category and g['kind']=='polyline' and g.get('closed'):
                try:
                    pts=local_footprint(record,frame,tolerance)
                    # Small porch/canopy posts need heights from their own details.
                    if category=='Unclassified construction' and len(pts)==4:
                        box=bounds(pts)
                        if max(box[1][i]-box[0][i] for i in (0,1))<300:
                            continue
                    if category not in parents:
                        parents[category]=collection(category,floor_parent)
                    add(pts,category,record,floor,base,top,parents[category],
                        'Source plan footprint extruded to section level; local beams, lintels and material classification require review')
                except Exception as exc:
                    issues.append({'source_id':record['id'],'floor':floor['name'],'reason':str(exc)})
        if index==0:
            for column in frame['columns']:
                box=column['source_bbox']; ox,oy=frame['origin_mm']
                pts=[[box[0][0]-ox,box[0][1]-oy,0],[box[1][0]-ox,box[0][1]-oy,0],
                     [box[1][0]-ox,box[1][1]-oy,0],[box[0][0]-ox,box[1][1]-oy,0]]
                # Validate the semantic rectangular profile against actual COM
                # definition linework, never substitute a box for an unknown solid.
                children=[r for r in records if r['id'].startswith(column['id']+'/') and r['geometry']['kind']=='line']
                actual=[]
                for child in children:
                    actual.extend(transform(child['world_matrix'],child['geometry'][k]) for k in ('start','end'))
                for p in pts:
                    world=[p[0]+ox,p[1]+oy,0]
                    if not any(norm([q[i]-world[i] for i in range(3)])<=tolerance for q in actual):
                        raise ValueError('Column definition does not prove its rectangular corners: '+column['id'])
                add(pts,'Columns',column,floor,0,project['column_top']['value_mm'],parent_columns,
                    '400x400 rectangular COM block, matching linework; above datum to section roof underside; foundations excluded')
    if slab_sources:
        slab_parent=collection('Slabs from KZh formwork',model)
        for slab,manifest,records in slab_sources:
            contour=next(r for r in records if r['id']==slab['source_id'])
            from autocad_mcp.geometry import world_geometry
            outline=world_geometry(contour,tolerance)[0]
            box=bounds(outline)
            anchor=next(r for r in records if r['id']==slab['origin_column_id'])['source_bbox']
            origin=[(anchor[0][i]+anchor[1][i])/2 for i in (0,1)]
            def rectangle(record):
                pts=world_geometry(record,tolerance)[0]
                b=bounds(pts)
                if len(pts)!=4 or any(min(norm([p[0]-x,p[1]-y,0]) for x in (b[0][0],b[1][0]) for y in (b[0][1],b[1][1]))>tolerance for p in pts):
                    raise ValueError('Slab source is not a proved axis-aligned rectangular contour')
                return [[p[0]-origin[0],p[1]-origin[1]] for p in b]
            outer=rectangle(contour)
            hole_records=[r for r in records if r['id'].startswith(slab['hole_container_id']+'/') and r['geometry']['kind']=='polyline']
            if len(hole_records)!=slab['expected_holes']:
                raise ValueError('Slab opening count differs from source selection')
            holes=[rectangle(r) for r in hole_records]
            polygons=[[[h[0][0],h[0][1]],[h[1][0],h[0][1]],[h[1][0],h[1][1]],[h[0][0],h[1][1]]] for h in holes]
            for source_id in slab.get('additional_hole_ids',[]):
                record=next(r for r in records if r['id']==source_id)
                points=closed_footprint(record,tolerance)
                polygon=[[p[i]-origin[i] for i in (0,1)] for p in points]
                polygons.append(polygon)
                hole_records.append(record)
                box=[[min(p[i] for p in polygon) for i in (0,1)],[max(p[i] for p in polygon) for i in (0,1)]]
                if not inside_polygon([(box[0][i]+box[1][i])/2 for i in (0,1)],polygon):
                    raise ValueError('Opening needs an explicit verified interior test point')
                holes.append(box)
            top,thickness=slab['top']['value_mm'],slab['thickness']['value_mm']
            vertices,faces,volume=orthogonal_slab(outer,polygons,top-thickness,thickness)
            obj=mesh_object(slab['name'],vertices,[],faces,slab_parent)
            obj['element_id']=slab['name']+'/'+contour['id']
            obj['source_id'],obj['category']=contour['id'],'Slabs'
            obj['height_mm'],obj['base_z_mm']=thickness,top-thickness
            obj['source_openings']=json.dumps([r['id'] for r in hole_records])
            obj['completeness_note']=slab.get('completeness_note','')
            obj['expected_volume_mm3']=volume
            obj['hole_rectangles_mm']=json.dumps(holes)
            obj['source_hole_polygons_mm']=json.dumps(polygons)
            obj.color=(0.7,0.72,0.75,1) if not slab.get('provisional') else (0.95,0.6,0.2,1)
            # Roof hidden only for the cutaway render, still present in the model.
            obj.hide_render=slab.get('hide_in_cutaway',False)
            objects.append((obj,vertices))
            ledger.append({'element_id':obj['element_id'],'source_id':contour['id'],'source_blend':slab['source_blend'],
                           'category':'Slabs','expected_vertices_mm':vertices,'base_mm':top-thickness,'top_mm':top,
                           'source_hole_ids':[r['id'] for r in hole_records],'expected_volume_mm3':volume,
                           'interpretation':slab.get('completeness_note','')})
            counts['Slabs']+=1
    checks=checks_for(objects,tolerance)
    model_points=[p for _,points in objects for p in points]
    qa=collection('QA cameras')
    renders=render_controls(target,model_points,qa)
    report={'project':project,'counts':dict(counts),'evidence_hashes':evidence_hashes,
            'frames':[{k:v for k,v in frame.items() if k!='columns'} for _,_,_,frame in sources],
            'source_plans':[manifest for _,manifest,_,_ in sources],
            'issues':issues,'unresolved':project['unresolved'],'checks':checks,'elements':ledger,'renders':renders,
            'verification':{'coordinate_checks_passed':all(c['passed'] for c in checks),'check_count':len(checks),
                            'failure_count':sum(not c['passed'] for c in checks),'tolerance_mm':tolerance,
                            'semantic_building_verified':False,'full_fidelity':False,
                            'floor_grids_match':True,'source_files_unchanged':True}}
    if not report['verification']['coordinate_checks_passed']:
        raise ValueError('Architectural geometry failed measured checks')
    target.with_suffix('.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    internal=bpy.data.texts.new('Architectural reconstruction audit.json.zlib.base85')
    internal.write(base64.b85encode(zlib.compress(json.dumps(report,ensure_ascii=True).encode())).decode())
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    print(json.dumps({'blend':str(target),'counts':dict(counts),'verification':report['verification']},ensure_ascii=True))


if __name__=='__main__':
    main()
