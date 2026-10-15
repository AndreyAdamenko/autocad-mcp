"""Build the accepted exterior overview from original COM records in final BLENDs.

No geometry exchange file is read. The JSON project contains selections,
evidence and explicitly accepted simplifications only. This profile makes no
claim of construction/BIM completeness.
"""
from __future__ import annotations
import base64,json,sys,zlib
from collections import Counter
from pathlib import Path
import bpy
from mathutils import Vector

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.architecture import closed_footprint,plan_frame,source_world_bounds,rectangular_slab
from autocad_mcp.architectural_worker import checks_for,decode,digest
from autocad_mcp.blender_worker import actual_points,collection,extrude,mesh_object
from autocad_mcp.geometry import bounds,world_geometry
from autocad_mcp.overview import footprints,window_gaps,gap_bands,exterior_gaps


def rect(x0,y0,x1,y1):
    if x1<=x0 or y1<=y0:raise ValueError('Invalid rectangle')
    return [[x0,y0,0],[x1,y0,0],[x1,y1,0],[x0,y1,0]]


def strip(gap,lo,hi,perpendicular):
    a,b=perpendicular
    return rect(lo,a,hi,b) if gap['axis']==0 else rect(a,lo,b,hi)


def local(record,origin):
    return [[p[0]-origin[0],p[1]-origin[1],0] for p in closed_footprint(record)]


def main():
    args=sys.argv[sys.argv.index('--')+1:]
    config=json.loads(Path(args[0]).read_text(encoding='utf-8'));target=Path(args[1]).resolve()
    if target.exists():raise ValueError('Refusing to overwrite a BLEND')
    if config.get('profile')!='overall_view' or config.get('accepted_simplifications') is not True:
        raise ValueError('This profile requires explicitly accepted overview simplifications')
    tolerance=config.get('tolerance_mm',0.25)
    profile_depth=config['overview_parameters']['window_profile_depth_mm']
    if profile_depth.get('confirmed') is not False or not 10<=profile_depth['value_mm']<=200:
        raise ValueError('Unconfirmed secondary window depth must remain labelled and bounded')
    packets={};hashes={}
    for key,path in config['overview_sources'].items():
        bpy.ops.wm.open_mainfile(filepath=path,load_ui=False,use_scripts=False)
        lines=decode('AutoCAD source provenance.jsonl').splitlines()
        manifest=json.loads(lines[0])['manifest'];records=[json.loads(s) for s in lines[1:] if s]
        if not manifest.get('readonly') or manifest.get('mm_per_unit')!=1:
            raise ValueError('Read-only millimetre COM provenance required: '+key)
        for p,h in [(manifest['source_path'],manifest['source_sha256']),
                    (manifest.get('origin_source_path'),manifest.get('origin_source_sha256'))]:
            if p:
                if digest(p)!=h:raise ValueError('DWG changed: '+p)
                hashes[p]=h
        packets[key]={'manifest':manifest,'records':records,'source_blend':path}
    window_by={r['id']:r for r in packets['window']['records']}
    detail_boxes=[bounds(closed_footprint(window_by[i])) for i in ('Model/1933A','Model/19346')]
    for box,expected_size in zip(detail_boxes,[(1050,1800),(1000,1750)]):
        if any(abs(box[1][i]-box[0][i]-expected_size[i])>tolerance for i in (0,1)):
            raise ValueError('Current window COM outline contradicts overview product dimensions')
    if any(abs(abs(detail_boxes[1][j][i]-detail_boxes[0][j][i])-25)>tolerance for j in (0,1) for i in (0,1)):
        raise ValueError('Window perimeter joints are not the expected 25 mm')
    for source in config['evidence_drawings']:
        if digest(source['path'])!=source['sha256']:raise ValueError('Evidence changed')
        hashes[source['path']]=source['sha256']
    bpy.ops.wm.open_mainfile(filepath=config['base_model'],load_ui=False,use_scripts=False)
    old=json.loads(decode('Architectural reconstruction audit.json'))
    ledger=[e for e in old['elements'] if e['category']!='Columns']
    for obj in list(bpy.data.objects):
        if obj.type in ('CAMERA','LIGHT') or obj.get('category')=='Columns':
            bpy.data.objects.remove(obj,do_unlink=True)
    by_id={o.get('element_id'):o for o in bpy.data.objects if o.get('element_id')}
    objects=[(by_id[e['element_id']],e['expected_vertices_mm']) for e in ledger]
    colors={'Walls':(.72,.69,.61,1),'Insulation':(.61,.55,.66,1),
            'Facade brick':(.70,.57,.35,1),'Unclassified construction':(.56,.57,.57,1),
            'Columns':(.48,.50,.52,1),'Beams':(.48,.50,.52,1),
            'Window frames':(.34,.32,.28,1),'Glass':(.26,.40,.47,1),
            'Doors':(.36,.34,.30,1),'Parapets':(.56,.55,.51,1),
            'Cornice':(.32,.34,.35,1),'Canopies':(.32,.34,.35,1),
            'Plinth':(.32,.34,.35,1),'Roofing':(.28,.29,.30,1),
            'Porches':(.45,.46,.45,1),'Finishes':(.67,.65,.60,1)}
    for obj,_ in objects:
        obj.color=colors.get(obj['category'],(.6,.6,.6,1));obj.hide_render=False
    model=collection('Overall view additions');parents={};serial=0
    def add(points,base,top,category,source_ids,source_key,note,status='derived'):
        nonlocal serial
        if top<=base:raise ValueError('Nonpositive height')
        serial+=1
        parent=parents.setdefault(category,collection('Overview - '+category,model)) if category not in parents else parents[category]
        rule={'category':category,'base_z_mm':{'value':base},'height_mm':{'value':top-base}}
        obj,expected=extrude(points,rule,parent,';'.join(source_ids),tolerance)
        obj.name=f'{category} {serial:04d}'
        obj['element_id']=f'Overview/{serial:04d}';obj['source_id']=';'.join(source_ids)
        obj['category']=category;obj['evidence_status']=status;obj['interpretation']=note
        obj['source_blend']=packets[source_key]['source_blend'];obj.color=colors[category]
        objects.append((obj,expected))
        ledger.append({'element_id':obj['element_id'],'category':category,'source_id':obj['source_id'],
                       'source_blend':obj['source_blend'],'expected_vertices_mm':expected,
                       'base_mm':base,'top_mm':top,'interpretation':note,'evidence_status':status})
        return obj
    def ring(outer,inner,base,top,category,ids,key,note,status='derived'):
        # Four joined, individually closed solids. Interior faces at joins are
        # harmless for an overview; source boundary coordinates are retained.
        x0,y0=outer[0][:2];x1,y1=outer[1][:2];a,b=inner[0][:2];c,d=inner[1][:2]
        if not (x0<a<c<x1 and y0<b<d<y1):raise ValueError('Invalid nested source rings')
        return [add(p,base,top,category,ids,key,note,status) for p in
                (rect(x0,y0,x1,b),rect(x0,d,x1,y1),rect(x0,b,a,d),rect(c,b,x1,d))]

    # Actual KZh009 column footprints, not the 24 architectural symbols.
    frame_records=packets['frame']['records'];column_candidates=[]
    for r in frame_records:
        if r['geometry']['kind']!='polyline':continue
        try:p=closed_footprint(r)
        except ValueError:continue
        b=bounds(p);s=[b[1][i]-b[0][i] for i in (0,1)]
        if b[0][1]>0 and all(abs(v-400)<tolerance for v in s):column_candidates.append((r,p,b))
    unique={}
    for r,p,b in column_candidates:
        center=tuple(round((b[0][i]+b[1][i])/2,3) for i in (0,1))
        unique.setdefault(center,(r,p))
    if len(unique)!=22:raise ValueError(f'KZh column count: {len(unique)} != 22')
    origin=[min(c[i] for c in unique) for i in (0,1)]
    if any(abs(max(c[i] for c in unique)-origin[i]-s)>tolerance for i,s in enumerate((30000,12900))):
        raise ValueError('KZh frame does not match architectural axes')
    for r,p in unique.values():
        add(local(r,origin),0,7800,'Columns',[r['id']],'frame','KZh009 400x400 profile; upper parts only, foundations outside overview scope','drawing')
    # Only the source 350 mm main beams; 250 mm profiles are not silently
    # assigned the same section depth.
    beam_count=0
    for cluster,top in [(True,3950),(False,8000)]:
        selected=[]
        for r in frame_records:
            if r['geometry']['kind']!='polyline':continue
            try:p=closed_footprint(r)
            except ValueError:continue
            b=bounds(p);s=[b[1][i]-b[0][i] for i in (0,1)]
            if (b[0][1]>0)!=cluster or min(s)<349.9 or min(s)>350.1 or max(s)<2000:continue
            selected.append(r)
        other_origin=origin if cluster else [origin[0],min(bounds(closed_footprint(r))[0][1]+200 for r in frame_records if r['geometry']['kind']=='polyline' and r['id'].startswith('Model/1A98'))]
        for r in selected:
            add(local(r,other_origin),top-550,top,'Beams',[r['id'],'KZh001:350x550'],'frame','350 mm COM footprint; 550 mm depth from KZh001; detailing omitted')
            beam_count+=1

    floors=[];opening_ledger=[];assembly_windows=[]
    for index,(key,title,base,top) in enumerate([('ground','Model/2F228',0,3750),('upper','Model/E11F',4050,7800)]):
        records=packets[key]['records'];frame=plan_frame(records,title)
        profiles=footprints(records,frame);gaps=window_gaps(profiles,frame['axis_spans_mm'])
        if len(gaps)!=(16,21)[index]:raise ValueError('Window positions do not match source schedule')
        floors.append((key,records,frame,profiles))
        for number,gap in enumerate(gaps,1):
            bands=gap_bands(profiles,gap)
            if not any(b['category'] in ('Walls','Unclassified construction') for b in bands):
                raise ValueError('No physical wall flanks at window')
            ids=gap['source_ids']+['AR11_12:Model/1933A','AR11_12:Model/19346']
            for band in bands:
                for low,high in [(base,base+975),(base+2775,top)]:
                    add(strip(gap,gap['lo'],gap['hi'],band['perpendicular']),low,high,band['category'],band['source_ids'],key,
                        'Infill below/above opening; exact plan flanks, 1050x1800 opening from current AR11_12 detail; +1000 product sill with 25 mm joint')
            # Current REV3 product: 1000x1750 inside 1050x1800, 25 mm each side.
            lo=gap['lo']+25;hi=gap['hi']-25;axis=gap['axis']
            wall=next(b for b in bands if b['category'] in ('Walls','Unclassified construction'))['perpendicular']
            outward_low=gap['side'] in ('south','west')
            plane=(wall[0]+35) if outward_low else (wall[1]-35)
            depth=config['overview_parameters']['window_profile_depth_mm']['value_mm']
            def piece(u0,u1,z0,z1,cat='Window frames',part_depth=None):
                thick=part_depth or depth
                obj=add(strip(gap,lo+u0,lo+u1,[plane-thick/2,plane+thick/2]),base+1000+z0,base+1000+z1,
                        cat,ids,'window','Current product outline and face profiles; profile depth simplified for overall view','assumed secondary depth')
                obj['window_assembly']=f'Floor{index+1}/OK1/{number}'
                return obj
            # 50 mm perimeter, 40 mm mullions: actual face layout in REV3 detail.
            for u0,u1,z0,z1 in [(0,50,0,1750),(950,1000,0,1750),(50,950,0,50),(50,950,1700,1750),
                                  (355,395,50,1700),(50,950,1205,1245)]:piece(u0,u1,z0,z1)
            for u0,u1,z0,z1 in [(50,355,50,1205),(395,950,50,1205),(50,355,1245,1700),(395,950,1245,1700)]:
                piece(u0,u1,z0,z1,'Glass',24)
            assembly_windows.append({'id':f'Floor{index+1}/OK1/{number}','floor':index+1,'gap':gap,'width_mm':1000,'height_mm':1750})
            opening_ledger.append({'floor':index+1,'side':gap['side'],'axis':axis,'lo':gap['lo'],'hi':gap['hi'],
                                   'base_mm':base+975,'top_mm':base+2775,'category':'Window','source_ids':ids})
        # Four real entrance gaps and the upper glazed facade. The 400 mm
        # interruptions are column positions, never counted as openings.
        for gap in exterior_gaps(profiles,frame['axis_spans_mm']):
            width=gap['hi']-gap['lo']
            if width<1100:continue
            is_glazed=gap['side']=='south' and abs(width-2880)<tolerance
            if index and not is_glazed:continue
            if not index and gap['side'] not in ('south','north','west','east'):continue
            door_height=2400 if is_glazed else 2080
            bands=gap_bands(profiles,gap)
            for band in bands:
                add(strip(gap,gap['lo'],gap['hi'],band['perpendicular']),base+door_height,top,
                    band['category'],band['source_ids'],key,'Wall head above source entrance/glazed gap; nominal steel door height uses plan block designation')
            if is_glazed:
                # Positions of glass panels are independently present in the
                # COM plan; the gap supplies the full enclosing opening.
                plane=-75;lo=gap['lo']+50;hi=gap['hi']-50
                divisions=[lo,lo+615,hi-615,hi]
                for a,b in zip(divisions,divisions[1:]):
                    add(strip(gap,a+25,b-25,[plane-15,plane+15]),base+50,base+2350,'Glass',gap['source_ids'],key,
                        'Glazed facade product simplified, 7-16-7=30 mm glazing; 2400 overall height from AR opening detail','accepted simplification')
                for a in divisions:
                    add(strip(gap,a-25,a+25,[plane-35,plane+35]),base,base+2400,'Window frames',gap['source_ids'],key,'Glazed facade mullions simplified','assumed secondary profile')
                for z in (base,base+2350):
                    add(strip(gap,lo,hi,[plane-35,plane+35]),z,z+50,'Window frames',gap['source_ids'],key,'Glazed facade rails simplified','assumed secondary profile')
            else:
                nominal=1260 if gap['side'] in ('west','east') else 1160
                mid=(gap['lo']+gap['hi'])/2;plane={'west':-130,'east':30130,'north':13130}[gap['side']]
                add(strip(gap,mid-nominal/2,mid+nominal/2,[plane-25,plane+25]),base,base+2080,
                    'Doors',gap['source_ids'],key,'Nominal 1260/1160x2080 from actual plan block name; flat closed door without hardware','accepted nominal product')
            opening_ledger.append({'floor':index+1,'side':gap['side'],'axis':gap['axis'],'lo':gap['lo'],'hi':gap['hi'],
                                   'base_mm':base,'top_mm':base+door_height,'category':'Glazed facade' if is_glazed else 'Door','source_ids':gap['source_ids']})

    # Close floor-edge cladding using the source full perimeter rectangle.
    # The concealed junction profile is an explicit overall-view simplification.
    outer=[[-460,-460],[30460,13360]];inner=[[-340,-340],[30340,13240]]
    for base,top in [(3750,4050),(7800,8000)]:
        ring(outer,inner,base,top,'Facade brick',['AR2_3:source facade contours','AR7:section levels'],'ground',
             'Continuous floor-edge facing for exterior view; concealed support junction simplified','accepted junction simplification')
    ring(outer,inner,-450,0,'Plinth',['AR7:ground -0.450','AR8_9:RAL7037'],'ground',
         'Exterior plinth envelope; underground foundation construction excluded','accepted massing')

    roof=packets['roof']['records'];roof_by={r['id']:r for r in roof}
    axes=[]
    for r in roof:
        if r['geometry']['kind']=='line' and 'Оси' in r.get('effective_layer',''):
            axes.extend(world_geometry(r)[0])
    # Extremities of axis lines include overhangs. Identify vertical/horizontal
    # constant coordinates, then validate the exact 30 m x 12.9 m grid span.
    vertical=[];horizontal=[]
    for r in roof:
        if r['geometry']['kind']!='line' or 'Оси' not in r.get('effective_layer',''):continue
        p=world_geometry(r)[0]
        if abs(p[0][0]-p[1][0])<tolerance:vertical.append(p[0][0])
        if abs(p[0][1]-p[1][1])<tolerance:horizontal.append(p[0][1])
    roof_origin=[min(vertical),min(horizontal)]
    if abs(max(vertical)-min(vertical)-30000)>tolerance or abs(max(horizontal)-min(horizontal)-12900)>tolerance:
        raise ValueError('Roof axes mismatch')
    roof_boxes={i:bounds(local(roof_by['Model/'+i],roof_origin)) for i in ('8EA','8EB','8EC','8ED','9A3')}
    ring(roof_boxes['8EB'],roof_boxes['8EA'],8000,9670,'Parapets',['Model/8EB','Model/8EA'],'roof','Parapet core source plan ring, source section top +9.670')
    ring(roof_boxes['8EC'],roof_boxes['8EB'],8000,9670,'Insulation',['Model/8EC','Model/8EB'],'roof','150 mm source plan insulation ring')
    # 120 mm brick outside the source 40 mm ventilated gap.
    brick_inner=roof_boxes['8ED'];brick_outer=[[brick_inner[0][0]-120,brick_inner[0][1]-120],[brick_inner[1][0]+120,brick_inner[1][1]+120]]
    ring(brick_outer,brick_inner,8000,9670,'Facade brick',['Model/8ED','AR7:Model/3329'],'roof','120 mm facing outside recorded gap; plan-derived outer face')
    ring(roof_boxes['9A3'],brick_outer,8450,9670,'Cornice',['Model/9A3','AR13:Model/475'],'roof',
         '1220 mm cornice height; attribution of roof outer contour 9A3 to fascia envelope accepted for overall view','accepted contour interpretation')
    roof_p=local(roof_by['Model/8EA'],roof_origin)
    roof_slab=next(obj for obj,_ in objects if obj.get('category')=='Slabs' and obj['base_z_mm']==7800)
    holes=json.loads(roof_slab['hole_rectangles_mm'])
    verts,faces,volume=rectangular_slab(bounds(roof_p),holes,8000,374)
    parent=collection('Overview - roofing',model)
    roofing=mesh_object('Roofing minimum buildup 374 mm',verts,[],faces,parent)
    roofing['element_id']='Overview/roofing';roofing['source_id']='Model/8EA;AR7:Model/2618';roofing['category']='Roofing'
    roofing['height_mm']=374;roofing['base_z_mm']=8000;roofing.color=colors['Roofing']
    roofing['expected_volume_mm3']=volume;roofing['hole_rectangles_mm']=json.dumps(holes)
    objects.append((roofing,verts));ledger.append({'element_id':roofing['element_id'],'source_id':roofing['source_id'],
       'category':'Roofing','source_blend':packets['roof']['source_blend'],'expected_vertices_mm':verts,'base_mm':8000,'top_mm':8374,
       'evidence_status':'accepted flattened roof surface','interpretation':'40+200+114+20 mm minimum buildup; source five service holes retained; slopes and thin membranes flattened'})

    ground_key,ground_records,ground_frame,_=floors[0]
    upper_key,upper_records,upper_frame,_=floors[1]
    upper_by={r['id']:r for r in upper_records};ground_by={r['id']:r for r in ground_records}
    canopy_ids=['15BB3','15BB4','15BB5','15BB6']
    porch_pairs=[('2F222','2F225'),('2F202','2F205'),('2F208','2F20B'),('2F20E','2F211')]
    for cid,pair in zip(canopy_ids,porch_pairs):
        p=local(upper_by['Model/'+cid],upper_frame['origin_mm'])
        add(p,2600,3400,'Canopies',['Model/'+cid,'AR14_21:+2.600/+3.400'],'upper',
            'Exact plan projection, 2.600/3.400 envelope; internal framing and slope simplified','accepted envelope simplification')
        bs=[bounds(local(ground_by['Model/'+i],ground_frame['origin_mm'])) for i in pair]
        box=[[min(b[0][i] for b in bs) for i in (0,1)],[max(b[1][i] for b in bs) for i in (0,1)]]
        for i in pair:
            add(local(ground_by['Model/'+i],ground_frame['origin_mm']),-450,-30,'Porches',['Model/'+i],'ground',
                'Source side outline, platform -0.030 above ground -0.450; concealed construction simplified','accepted entry massing')
        movement=0 if cid in ('15BB3','15BB5') else 1
        side=1-movement
        towards_low=cid in ('15BB3','15BB4')
        steps={}
        for r in ground_records:
            if r.get('effective_layer')!='Лестницы' or r['geometry']['kind']!='line':continue
            pts=world_geometry(r)[0];pts=[[q[i]-ground_frame['origin_mm'][i] for i in (0,1)] for q in pts]
            if abs(pts[0][movement]-pts[1][movement])>tolerance or abs(pts[0][side]-pts[1][side])<500:continue
            if not all(box[0][i]-tolerance<=q[i]<=box[1][i]+tolerance for q in pts for i in (0,1)):continue
            steps.setdefault(round(pts[0][movement],3),(r['id'],pts))
        if len(steps)!=3:raise ValueError('Entry needs three independently recorded riser lines: '+cid+': '+str(len(steps)))
        coordinates=sorted(steps,reverse=towards_low)
        first=steps[coordinates[0]][1];clear=[min(q[side] for q in first),max(q[side] for q in first)]
        boundary=box[1][movement] if towards_low else box[0][movement]
        landing=([coordinates[0],boundary] if towards_low else [boundary,coordinates[0]])
        pseudo={'axis':side}
        add(strip(pseudo,*clear,landing),-450,-30,'Porches',[steps[c][0] for c in coordinates],'ground',
            'Landing boundary from actual plan riser line, top -0.030','derived')
        for i,(a,b) in enumerate(zip(coordinates,coordinates[1:])):
            add(strip(pseudo,*clear,sorted([a,b])),-450,-30-(i+1)*140,'Porches',[steps[a][0],steps[b][0]],'ground',
                '300 mm source tread intervals; three equal 140 mm rises derived from 420 mm level difference','derived equal rises')
        for r in ground_records:
            if r['geometry']['kind']!='polyline' or r.get('effective_layer')!='KG_Конструкции_50':continue
            try:pts=local(r,ground_frame['origin_mm'])
            except ValueError:continue
            b=bounds(pts)
            if max(b[1][i]-b[0][i] for i in (0,1))>200:continue
            if all(box[0][i]-tolerance<=b[j][i]<=box[1][i]+tolerance for i in (0,1) for j in (0,1)):
                add(pts,-30,2600,'Unclassified construction',[r['id']],'ground',
                    'Small entry post plan profile; height to canopy underside, material unclassified','derived post height')
    # Floor finishes preserve the actual upper slab openings and mesh topology.
    slab=next((obj,e) for obj,e in objects if obj.get('category')=='Slabs' and obj['base_z_mm']==3750)
    original,expected=slab
    verts=[[p[0],p[1],3950+(p[2]-3750)/2] for p in expected]
    parent=collection('Overview - floor finishes',model)
    finish=mesh_object('Upper floor finish 100 mm',verts,[],[list(f.vertices) for f in original.data.polygons],parent)
    finish['element_id']='Overview/upper finish';finish['source_id']=original['source_id'];finish['category']='Finishes'
    finish['height_mm']=100;finish['base_z_mm']=3950;finish.color=colors['Finishes']
    objects.append((finish,verts));ledger.append({'element_id':finish['element_id'],'source_id':finish['source_id'],
       'category':'Finishes','source_blend':config['base_model'],'expected_vertices_mm':verts,'base_mm':3950,'top_mm':4050,
       'evidence_status':'derived','interpretation':'AR10 finish buildup 100 mm; same source slab holes retained'})

    # Ground plane is a presentation aid, not terrain geometry from the DWG.
    aid=collection('Presentation - hide for measurements')
    ground=mesh_object('Illustrative ground plane',[[ -5000,-6000,-455],[35000,-6000,-455],[35000,19000,-455],[-5000,19000,-455]],[],[[0,1,2,3]],aid)
    ground.color=(.19,.21,.19,1);ground['presentation_only']=True
    checks=checks_for(objects,tolerance)
    if not all(c['passed'] for c in checks):raise ValueError('Overview mesh checks failed: '+str([c for c in checks if not c['passed']][:5]))
    for path,expected_hash in hashes.items():
        if digest(path)!=expected_hash:raise ValueError('Source changed during build')
    counts=dict(Counter(obj['category'] for obj,_ in objects))
    if len(assembly_windows)!=37 or len(unique)!=22:raise ValueError('Source completeness counts failed')
    # Sample each actual window aperture, excluding its own glazing, against
    # the reconstructed opaque building solids. Camera renders are additional QA.
    blockers=[obj for obj,_ in objects if obj['category'] in ('Walls','Unclassified construction','Facade brick','Insulation','Columns','Beams')]
    aperture_checks=[]
    for item in assembly_windows:
        gap=item['gap'];axis=gap['axis'];other=1-axis
        start=[0.,0.,(item['floor']-1)*4050+1850];start[axis]=(gap['lo']+gap['hi'])/2
        sign=-1 if gap['side'] in ('south','west') else 1
        start[other]=(-1000 if sign<0 else (12900 if other==1 else 30000)+1000)
        direction=[0,0,0];direction[other]=-sign
        hits=[]
        for obj in blockers:
            inverse=obj.matrix_world.inverted();point=inverse@Vector([v/1000 for v in start])
            ray=inverse.to_3x3()@Vector(direction)
            hit,location,*_=obj.ray_cast(point,ray,distance=1.3)
            if hit:hits.append(obj['element_id'])
        aperture_checks.append({'id':item['id'],'unobstructed_opaque_wall_sample':not hits,'blockers':hits})
    if not all(c['unobstructed_opaque_wall_sample'] for c in aperture_checks):
        raise ValueError('Opaque material blocks a window: '+str([c for c in aperture_checks if c['blockers']]))

    scene=bpy.context.scene;scene.unit_settings.system='METRIC';scene.unit_settings.scale_length=1
    scene['model_purpose']='Overall exterior appearance; accepted simplifications, not construction/BIM verification'
    scene['full_fidelity']=False;scene['semantic_building_verified']=False
    scene['datum_absolute_m']=416.5
    scene.render.engine='BLENDER_WORKBENCH';scene.display.shading.light='STUDIO'
    scene.display.shading.color_type='OBJECT';scene.display.shading.show_shadows=True
    scene.display.shading.show_cavity=True;scene.display.shading.cavity_type='BOTH'
    scene.display.shading.background_type='WORLD';scene.world.color=(.07,.075,.08)
    scene.render.resolution_x=1800;scene.render.resolution_y=1100;scene.render.resolution_percentage=100
    scene.render.image_settings.file_format='PNG'
    qa=collection('Overview cameras');renders=[];center=Vector((15,6.45,4.4))
    offsets={'front':Vector((0,-55,4)),'iso':Vector((32,-43,28)),'rear':Vector((-32,43,25)),'roof':Vector((0,0,60))}
    camera=None
    for label,offset in offsets.items():
        data=bpy.data.cameras.new('View - '+label);obj=bpy.data.objects.new('View - '+label,data);qa.objects.link(obj)
        data.type='ORTHO';data.ortho_scale=38 if label=='front' else 43;data.clip_end=300
        obj.location=center+offset;obj.rotation_euler=(0,0,0) if label=='roof' else (-offset).to_track_quat('-Z','Y').to_euler()
        scene.camera=obj;path=target.with_name(target.stem+'.'+label+'.png');scene.render.filepath=str(path)
        bpy.ops.render.render(write_still=True);renders.append(str(path))
        if label=='iso':camera=obj
    scene.camera=camera
    # Save a useful initial GUI view, with the complete exterior visible.
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type=='VIEW_3D':
                area.spaces.active.region_3d.view_location=center
                area.spaces.active.region_3d.view_distance=47
                area.spaces.active.region_3d.view_rotation=camera.rotation_euler.to_quaternion()
                area.spaces.active.shading.type='SOLID';area.spaces.active.shading.color_type='OBJECT'
                area.spaces.active.clip_end=500
    unresolved=config['unresolved']
    report={'project':config,'counts':counts,'windows':assembly_windows,'openings':opening_ledger,
            'aperture_checks':aperture_checks,'elements':ledger,'checks':checks,'renders':renders,
            'source_plans':[p['manifest'] for p in packets.values()],
            'evidence_hashes':[{'path':p,'sha256':h,'unchanged':True} for p,h in hashes.items()],
            'unresolved':unresolved,'issues':[],
            'verification':{'coordinate_checks_passed':True,'check_count':len(checks),'failure_count':0,'tolerance_mm':tolerance,
                'window_count_matches_schedule':True,'windows_per_floor':[16,21],'structural_columns':22,
                'window_aperture_samples_clear':True,'source_files_unchanged':True,
                'overall_view_profile_completed':True,'semantic_building_verified':False,'full_fidelity':False}}
    for name in ('Architectural reconstruction audit.json','Architectural reconstruction audit.json.zlib.base85'):
        if name in bpy.data.texts:bpy.data.texts.remove(bpy.data.texts[name])
    bpy.data.texts.new('Architectural reconstruction audit.json.zlib.base85').write(base64.b85encode(zlib.compress(json.dumps(report,ensure_ascii=True).encode())).decode())
    # Blender's text editor is inefficient for multi-megabyte single lines.
    # Short lines keep the full original COM provenance without losing records.
    encoded=base64.b85encode(zlib.compress(json.dumps(packets,ensure_ascii=True).encode())).decode()
    bpy.data.texts.new('AutoCAD combined source provenance.json.zlib.base85').write('\n'.join(encoded[i:i+120] for i in range(0,len(encoded),120)))
    target.with_suffix('.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    print(json.dumps({'blend':str(target),'counts':counts,'verification':report['verification']},ensure_ascii=True))


if __name__=='__main__':main()
