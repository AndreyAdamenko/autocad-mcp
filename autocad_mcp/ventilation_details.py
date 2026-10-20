"""Erdaut overview rules. Every inferred envelope stays explicitly labelled.

Not a generic HVAC designer: route positions come from the selected COM plans,
while un-dimensioned vertical profiles and product internals are approximations.
"""
import math
import re

from .geometry import bounds, transform, world_geometry
from .ventilation import (insulation_sweep, outline_loops, plain_text, section_from_text,
                          segment_distance, sweep)
from .ventilation_project import (inside_plan, local_point, property_value,
                                  inferred_round_system, visible_descendants)


def build_details(records, frames, levels, added, add, prism, issues, assumptions):
    by_id = {r['id']:r for r in records}
    roots = [r for r in records if r['id'].count('/') == 1 and r.get('visible') is not False]
    body_elements = list(added)
    terminal_roots, fittings_roots, paths = set(), set(), []

    def rectangle(center, width, depth):
        x,y = center[:2]
        return [[x-width/2,y-depth/2,0],[x+width/2,y-depth/2,0],
                [x+width/2,y+depth/2,0],[x-width/2,y+depth/2,0]]

    def tubes(path, section, identifier, system, floor, sources, note, category='Ventilation ducts',
              status='accepted_assumption', end=None, rule=None, insulated=0):
        vertices, faces = sweep(path, section, end)
        rule = dict(rule or {}, path_mm=path, end_section=end)
        obj = add(vertices, faces, identifier, category, system, floor, sources, section, note, status=status, rule=rule)
        if insulated:
            vertices, faces = insulation_sweep(path,section,insulated,end)
            add(vertices,faces,identifier+'/insulation','Ventilation insulation',system,floor,sources,section,
                f'{insulated:g} mm insulation from general OV notes; outer shell, no metal quantity claim.',
                status=status,rule={'kind':'insulation','thickness_mm':insulated,'path_mm':path})
        paths.append({'path':path,'section':section,'system':system,'floor':floor,'source_ids':sources})
        return obj

    def nearest_body(point, floor, system=None):
        choices=[]
        for item in body_elements:
            if item['floor'] != floor or system and item['system'] != system:
                continue
            rule=item['ventilation_rule'];path=rule.get('path_mm')
            if not path:continue
            distance,t=segment_distance(point,*path)
            q=[path[0][i]+t*(path[1][i]-path[0][i]) for i in range(3)]
            choices.append((distance,item,q))
        if not choices:raise ValueError('No measured adjacent duct body')
        return min(choices,key=lambda x:x[0])

    def source_paths(root,frame):
        paths,ids=[],[]
        for child in visible_descendants(records,root):
            if child['geometry']['kind'] not in ('line','arc','polyline'):continue
            points,_,_,closed=world_geometry(child,.75)
            points=[local_point(p,frame) for p in points]
            if closed and points:points.append(points[0])
            paths.append(points);ids.append(child['id'])
        return paths,ids

    # Connect facing collinear measured ports. This closes drawn flange gaps
    # and missing reducer envelopes without rerouting a branch across a room.
    candidates=[]
    for i,a in enumerate(body_elements):
        pa=a['ventilation_rule'].get('path_mm')
        if not pa or a['category']!='Ventilation ducts' or a['system'] not in ('П1','В1'):continue
        for b in body_elements[i+1:]:
            pb=b['ventilation_rule'].get('path_mm')
            if not pb or b['category']!='Ventilation ducts' or (a['floor'],a['system'])!=(b['floor'],b['system']):continue
            for ai,bi in ((0,0),(0,1),(1,0),(1,1)):
                start,end=pa[ai],pb[bi];delta=[end[k]-start[k] for k in range(2)];gap=math.hypot(*delta)
                if not 2<gap<600:continue
                da=[pa[ai][k]-pa[1-ai][k] for k in range(2)];db=[pb[bi][k]-pb[1-bi][k] for k in range(2)]
                la,lb=math.hypot(*da),math.hypot(*db)
                if sum(x*y for x,y in zip(da,db))/(la*lb)>-.999:continue
                forward=sum(x*y for x,y in zip(delta,da))/la
                sideways=abs(delta[0]*da[1]-delta[1]*da[0])/la
                if forward<=0 or sideways>max(a['section']['width_mm'],b['section']['width_mm'])/2:continue
                candidates.append((gap,a,b,ai,bi,start,end))
    used_ports=set()
    for gap,a,b,ai,bi,start,end in sorted(candidates,key=lambda value:value[0]):
        ports={(a['element_id'],ai),(b['element_id'],bi)}
        if used_ports & ports:continue
        used_ports.update(ports)
        tubes([start,end],a['section'],'port-join/'+a['element_id'].replace('/','_')+f'/{ai}',a['system'],a['floor'],
              [*a['source_ids'],*b['source_ids']], 'Reducer/flange-gap overview between facing measured duct ports; taper shape inferred.',
              category='Ventilation fittings',end=b['section'],rule={'kind':'port_join','from':a['element_id'],'to':b['element_id']},
              insulated=6 if a['system']=='П1' else 0)

    def label(identifier, floor):
        r=by_id[identifier];g=r['geometry']
        return local_point(transform(r['world_matrix'],g['leader_lines'][0]['vertices'][0]),frames[floor])

    # Bends/branches: body outlines are selected from visible source strokes.
    for floor in ('ground','upper'):
        frame=frames[floor]
        selected=[r for r in roots if r['geometry']['kind']=='block' and inside_plan(r,frame)]
        for root in selected:
            name=root['geometry']['effective_name'];lower=name.lower();identifier=floor+'/'+root['handle']
            center=local_point(transform(root['world_matrix'],root['geometry']['insertion']),frame)
            try:
                if lower in ('по1-90','по1-45-ххх','hvac. rectang_branch. 2'):
                    system='П1' if 'Прит' in root.get('effective_layer','') else 'В1'
                    _,neighbour,_=nearest_body(center,floor,system)
                    section=neighbour['section'];height=section['height_mm'];base=neighbour['base_mm']
                    drawing_paths,ids=source_paths(root,frame)
                    loops=outline_loops(drawing_paths,.1)
                    if not loops:raise ValueError('Visible fitting outline is open')
                    areas=[abs(sum(p[0]*q[1]-q[0]*p[1] for p,q in zip(loop,loop[1:]+loop[:1])))/2 for loop in loops]
                    for n,(loop,area) in enumerate(zip(loops,areas)):
                        if area<max(500,areas[0]*.01):continue
                        prism(loop,base,height,identifier+f'/outline{n}','Ventilation fittings',system,floor,
                              [root['id'],*ids,*neighbour['source_ids']],section,
                              'Visible planar fitting face; height inherited from adjacent measured duct, flange details simplified.',
                              status='accepted_assumption',rule={'kind':'fitting_outline','root':root['id'],'neighbour':neighbour['element_id']})
                    fittings_roots.add(root['id'])
                elif lower == 'perehod кп1':
                    numbers=re.findall(r'\d+',str(property_value(root,'Видимость')))
                    if len(numbers)<3:raise ValueError('Transition sizes absent')
                    diameters=list(map(float,numbers[-2:]));children=visible_descendants(records,root)
                    ports=[]
                    for diameter in diameters:
                        candidates=[]
                        for child in children:
                            if child['geometry']['kind']!='line':continue
                            pts=world_geometry(child)[0]
                            if abs(math.dist(*pts)-diameter)<.5:
                                candidates.append([sum(p[i] for p in pts)/2 for i in range(3)])
                        if not candidates:raise ValueError('Transition cross-section line absent')
                        ports.append(candidates)
                    a,b=max(((a,b) for a in ports[0] for b in ports[1]),key=lambda pair:math.dist(*pair))
                    system=inferred_round_system(center);z,source=levels[(system,floor)]
                    path=[local_point(p,frame)[:2]+[z] for p in (a,b)]
                    tubes(path,{'shape':'round','diameter_mm':diameters[0]},identifier,system,floor,
                          [root['id'],source], 'Conical overview between measured visible section strokes; rolled seams omitted.',
                          category='Ventilation fittings',end={'shape':'round','diameter_mm':diameters[1]},
                          rule={'kind':'round_transition','root':root['id']})
                    fittings_roots.add(root['id'])
                elif lower == 'otvod ko 90':
                    visibility=str(property_value(root,'Видимость'));diameter=float(re.search(r'KO\s+(\d+)',visibility).group(1))
                    children=visible_descendants(records,root)
                    arcs=[c for c in children if c['geometry']['kind']=='arc' and c['geometry']['radius']>diameter/2]
                    if len(arcs)!=1:raise ValueError('Unique drawn elbow centre arc not established')
                    pts=world_geometry(arcs[0],.5)[0];system=inferred_round_system(center);z,source=levels[(system,floor)]
                    path=[local_point(p,frame)[:2]+[z] for p in pts]
                    tubes(path,{'shape':'round','diameter_mm':diameter},identifier,system,floor,[root['id'],arcs[0]['id'],source],
                          'Round elbow swept along actual visible centre arc.',category='Ventilation fittings',status='derived',
                          rule={'kind':'round_elbow','root':root['id'],'axis_source':arcs[0]['id']})
                    fittings_roots.add(root['id'])
                elif lower == 'ктр_1':
                    nums=list(map(float,re.findall(r'\d+',str(property_value(root,'Видимость')))[-2:]))
                    if len(nums)!=2:raise ValueError('Tee section sizes absent')
                    caps=[]
                    for child in visible_descendants(records,root):
                        if child['geometry']['kind']!='line':continue
                        a,b=world_geometry(child)[0];length=math.dist(a,b)
                        if min(abs(length-d) for d in nums)<.5:
                            caps.append(([(a[i]+b[i])/2 for i in range(3)],[(b[i]-a[i])/length for i in range(3)],child['id'],length))
                    pairs=[(a,b) for i,a in enumerate(caps) for b in caps[i+1:]
                           if abs(a[3]-nums[0])<.5 and abs(b[3]-nums[0])<.5 and abs(sum(x*y for x,y in zip(a[1],b[1])))>.999]
                    a,b=max(pairs,key=lambda pair:math.dist(pair[0][0],pair[1][0]))
                    branches=[c for c in caps if abs(c[3]-nums[1])<.5 and abs(sum(x*y for x,y in zip(a[1],c[1])))<.01]
                    branch=max(branches,key=lambda c:segment_distance(c[0],a[0],b[0])[0])
                    _,t=segment_distance(branch[0],a[0],b[0]);joint=[a[0][i]+t*(b[0][i]-a[0][i]) for i in range(3)]
                    system=inferred_round_system(center);z,source=levels[(system,floor)]
                    for n,(ends,diameter) in enumerate([((a[0],b[0]),nums[0]),((joint,branch[0]),nums[1])]):
                        path=[local_point(p,frame)[:2]+[z] for p in ends]
                        tubes(path,{'shape':'round','diameter_mm':diameter},identifier+f'/branch{n}',system,floor,
                              [root['id'],a[2],b[2],branch[2],source], 'Tee envelope connects measured ports; intersecting branch envelopes are not a fabricated union.',
                              category='Ventilation fittings',rule={'kind':'round_tee','root':root['id']})
                    fittings_roots.add(root['id'])
                elif lower == 'a$c2d247ba6':
                    arcs=[c for c in visible_descendants(records,root) if c['geometry']['kind']=='arc'
                          and abs(c['geometry']['sweep']-math.pi)<1e-4]
                    if not arcs or any(math.dist(arcs[0]['geometry']['center'],c['geometry']['center'])>.001 for c in arcs[1:]):
                        raise ValueError('Elevation elbow symbol has no unique circular section centre')
                    arc=arcs[0];g=arc['geometry'];end=local_point(transform(arc['world_matrix'],g['center']),frame)
                    rim=local_point(world_geometry(arc)[0][0],frame)
                    diameter=round(2*math.dist(rim[:2],end[:2]));system=inferred_round_system(center);z,source=levels[(system,floor)]
                    tubes([center[:2]+[z],end[:2]+[z]],{'shape':'round','diameter_mm':diameter},identifier+'/plan-leg',system,floor,
                          [root['id'],arc['id'],source], 'Horizontal leg of out-of-plane elbow symbol; bend envelope simplified.',
                          category='Ventilation fittings',rule={'kind':'elevation_elbow','root':root['id']})
                    if system=='В2' and end[0]>0:
                        tubes([end[:2]+[z],end[:2]+[1805]],{'shape':'round','diameter_mm':diameter},identifier+'/dryer-drop',system,floor,
                              [root['id'],arc['id'],'Model/273961'], 'Dryer extraction drop to schematic +1.805; exact cabinet fitting simplified.',
                              category='Ventilation fittings')
                    fittings_roots.add(root['id'])
            except (ValueError,KeyError,AttributeError,IndexError) as exc:
                if lower in ('по1-90','по1-45-ххх','hvac. rectang_branch. 2','perehod кп1','otvod ko 90','ктр_1','a$c2d247ba6'):
                    issues.append({'id':root['id'],'floor':floor,'part':'fitting','reason':str(exc)})

    # Symbol arrows are never extruded. Square terminals use the outer physical
    # four-vertex frame drawn inside the block, while nominal neck size is kept.
    terminals=[]
    for floor in ('ground','upper'):
        frame=frames[floor];offset=0 if floor=='ground' else 4050
        for root in roots:
            g=root['geometry'];kind=g['kind']
            if kind=='block' and inside_plan(root,frame) and re.match(r'^D (150|225|300)x\1',g['effective_name']):
                size=float(re.search(r'D (\d+)',g['effective_name']).group(1))
                center=local_point(transform(root['world_matrix'],g['insertion']),frame)
                quads=[]
                for c in visible_descendants(records,root):
                    if c['geometry']['kind']=='polyline' and c['geometry'].get('closed') and len(c['geometry']['vertices'])==4:
                        pts=[local_point(p,frame) for p in world_geometry(c)[0]];box=bounds(pts)
                        if max(abs(box[1][i]-box[0][i]-(size+100)) for i in (0,1))<.25:quads.append((pts,c['id']))
                if len(quads)!=1:raise ValueError('Diffuser physical frame not uniquely established: '+root['id'])
                polygon,source=quads[0];system='П1' if 'Прит' in root.get('effective_layer','') else 'В1'
                section={'shape':'rectangular','width_mm':size,'height_mm':size}
                prism(polygon,offset+2750,30,floor+'/'+root['handle']+'/terminal','Ventilation terminals',system,floor,[root['id'],source],section,
                      'Plan frame and nominal neck measured; face thickness 30 mm and mounting +2.750 above floor are unconfirmed overview assumptions.',
                      status='accepted_assumption',rule={'kind':'square_terminal','root':root['id'],'outline_source':source,'mounting_assumed':True})
                terminal_roots.add(root['id'])
                terminals.append({'point':center,'z':offset+2780,'floor':floor,'system':system,'source':root['id'],
                                  'diameter':{150:100,225:160,300:200}[size]})
        vda=[]
        for r in roots:
            text=plain_text(r['geometry'].get('text',''))
            if 'ВДА' not in text:continue
            for line in r['geometry'].get('leader_lines',[]):
                point=local_point(transform(r['world_matrix'],line['vertices'][0]),frame)
                if -1000<point[0]<31000 and -1000<point[1]<14000:vda.append((point,r['id'],text))
        for root in roots:
            g=root['geometry']
            if g['kind']!='circle' or not inside_plan(root,frame) or not 40<g['radius']<80:continue
            center=local_point(transform(root['world_matrix'],g['center']),frame)
            if not vda:continue
            distance,label_id,_=min((math.dist(center[:2],p[:2]),sid,text) for p,sid,text in vda)
            if distance>200:continue
            system=inferred_round_system(center);z,source=levels[(system,floor)];diameter=round(2*g['radius'])
            # Keep the axis convention of the source, no invented ceiling drop.
            tubes([center[:2]+[z-25],center[:2]+[z+25]],{'shape':'round','diameter_mm':diameter},
                  floor+'/'+root['handle']+'/terminal',system,floor,[root['id'],label_id,source],
                  'Circular terminal location and nominal neck circle from plan; 50 mm product envelope depth is assumed.',
                  category='Ventilation terminals',rule={'kind':'round_terminal','root':root['id']})
            terminal_roots.add(root['id'])

    # Flexible connectors use MLine's recorded baseline path, not a claimed full
    # style reconstruction. The vertical fall to the assumed ceiling is labelled.
    flex_count=0
    for floor in ('ground','upper'):
        for r in roots:
            g=r['geometry']
            if not g.get('centerline') or not inside_plan(r,frames[floor]):continue
            points=[local_point(transform(r['world_matrix'],p),frames[floor]) for p in g['centerline']]
            candidates=[t for t in terminals if t['floor']==floor]
            if not candidates:continue
            distance,terminal,reverse=min((math.dist(points[k][:2],t['point'][:2]),t,k==0)
                for t in candidates for k in (0,-1))
            if distance>900:
                issues.append({'id':r['id'],'part':'flex','reason':'No square terminal within 900 mm of baseline endpoint'});continue
            if reverse:points.reverse()
            _,body,start=nearest_body(points[0],floor,terminal['system'])
            path=[p[:2]+[start[2]+(terminal['z']-start[2])*i/max(1,len(points)-1)] for i,p in enumerate(points)]
            end=terminal['point'][:2]+[terminal['z']]
            if math.dist(path[-1],end)>1:path.append(end)
            tubes(path,{'shape':'round','diameter_mm':terminal['diameter']},floor+'/'+r['handle']+'/flex',terminal['system'],floor,
                  [r['id'],terminal['source'],*body['source_ids']], 'Flexible envelope follows recorded MLine baseline in XY; style offset and ceiling fall are assumed.',
                  category='Ventilation fittings',rule={'kind':'flexible','root':r['id'],'terminal_source':terminal['source']},
                  insulated=6 if terminal['system']=='П1' else 0)
            flex_count+=1

    # Floor penetrations and roof outlets: XY from plan cross-section glyphs,
    # not from displaced callout text/arrowheads on fan housings.
    def center_of(identifier,floor):
        pts=world_geometry(by_id[identifier])[0];box=bounds([local_point(p,frames[floor]) for p in pts])
        return [(box[0][i]+box[1][i])/2 for i in (0,1)]

    for system,diameter0,diameter1,circle,ground_label,upper_label,roof_level in [
        ('В3',160,200,'Model/26D1C5','Model/26C0E8','Model/26CB97','Model/2739FB'),
        ('В4',125,160,'Model/26D1C4','Model/26C0EB','Model/26CB9B','Model/27375F')]:
        center=local_point(transform(by_id[circle]['world_matrix'],by_id[circle]['geometry']['center']),frames['roof'])[:2]
        z0=levels[(system,'ground')][0];z1=levels[(system,'upper')][0]
        tubes([center+[z0],center+[z1-250]],{'shape':'round','diameter_mm':diameter0},system+'/riser/low',system,'risers',
              [circle,ground_label,upper_label], 'Riser position from roof section circle; diameter changes between floor sections. Transition position below upper branch is assumed.')
        tubes([center+[z1-250],center+[z1]],{'shape':'round','diameter_mm':diameter0},system+'/riser/reducer',system,'risers',
              [circle,ground_label,upper_label], '250 mm reducer length assumed between the documented floor diameters.',
              category='Ventilation fittings',end={'shape':'round','diameter_mm':diameter1})
        tubes([center+[z1],center+[8570]],{'shape':'round','diameter_mm':diameter1},system+'/riser/high',system,'risers',
              [circle,upper_label,roof_level], 'Roof mounting elevation +8.570 from schematic; riser centre from plan.')
        # 400 mm square mounting sleeve is dimensioned by its plan footprint.
        outline='Model/26D45E' if system=='В3' else 'Model/26D464'
        polygon=[local_point(p,frames['roof']) for p in world_geometry(by_id[outline])[0]]
        prism(polygon,8170,400,system+'/mount','Ventilation equipment',system,'roof',[outline,roof_level],
              {'shape':'rectangular','width_mm':400,'height_mm':400},
              '400 mm mounting sleeve overview; plan footprint measured, MONST-315-400 designation; internals omitted.',status='accepted_assumption')
        outer='Model/26D45F' if system=='В3' else 'Model/26D465'
        polygon=[local_point(p,frames['roof']) for p in world_geometry(by_id[outer])[0]]
        prism(polygon,8570,330,system+'/fan','Ventilation equipment',system,'roof',[outer,roof_level],
              {'shape':'rectangular','width_mm':580,'height_mm':580},
              'KV 40/31-4D simplified housing: measured 580 mm plan footprint; 330 mm housing height is unconfirmed.',status='accepted_assumption',
              rule={'kind':'roof_fan','outline_source':outer,'product_height_assumed':True})

    # External В2 riser and cowl; +10.070 is the schematic outlet level.
    section_arc=by_id['Model/26D1EE']
    center=local_point(transform(section_arc['world_matrix'],section_arc['geometry']['center']),frames['roof'])[:2]
    tubes([center+[3185],center+[9870]],{'shape':'round','diameter_mm':315},'В2/riser','В2','risers',
          ['Model/26D1EE','Model/26D1E8','Model/27394B','Model/273996'],'Exterior riser at measured section-arc centre; roof cowl envelope stops at schematic +10.070.',insulated=40)
    tubes([center+[9870],center+[10070]],{'shape':'round','diameter_mm':630},'В2/cowl','В2','roof',
          ['Model/26D46A','Model/273996'],'Simplified conical cowl; diameter and 200 mm profile are assumed.',category='Ventilation equipment',
          end={'shape':'round','diameter_mm':1})

    for system,poly,section,start,level_id in [
        ('ВЕ1','Model/26D1C8',{'shape':'rectangular','width_mm':200,'height_mm':200},3100,'Model/273AAD'),
        ('ВЕ2','Model/26D1CF',{'shape':'rectangular','width_mm':150,'height_mm':100},3100,'Model/273A72'),
        ('В5','Model/2763DE',{'shape':'rectangular','width_mm':150,'height_mm':100},3300,'Model/27EF5F')]:
        center=center_of(poly,'roof')
        tubes([center+[start],center+[8570]],section,system+'/riser',system,'risers',[poly,level_id],
              'Vertical duct at the repeated floor/roof cross-section. Outlet at +8.570 is an overview assumption where schematic states roof level.',insulated=40)
        wider={**section,'width_mm':section['width_mm']+240,'height_mm':section['height_mm']+240}
        tubes([center+[8570],center+[8770]],wider,system+'/cowl',system,'roof',[poly,level_id],
              'Simplified roof hood; 200 mm height and overhang are assumed.',category='Ventilation equipment',
              end={'shape':'rectangular','width_mm':1,'height_mm':1})

    # В1 facade riser and horizontal discharge. Rectangular scheme elevation is
    # interpreted at the underside of the horizontal outlet.
    center=center_of('Model/26D1F3','roof');section={'shape':'rectangular','width_mm':400,'height_mm':200}
    tubes([center+[3200],center+[9970]],section,'В1/facade-riser','В1','risers',
          ['Model/26D1F3','Model/27CFDB','Model/27CFD5','Model/27CCAD'],'External return riser, bottom of roof discharge +9.870.',insulated=40)
    outlet=label('Model/26D209','roof')[:2]
    tubes([center+[9970],[center[0],outlet[1],9970]],section,'В1/roof-discharge','В1','roof',
          ['Model/26D1F3','Model/26D209','Model/27CCAD'],'Overview discharge toward source mesh-covered outlet; bend joint simplified.',insulated=40)
    ground=center_of('Model/279326','ground');upper=center_of('Model/26CCC0','upper');section={'shape':'rectangular','width_mm':250,'height_mm':150}
    tubes([ground+[3175],upper+[7275]],section,'В1/floor-riser','В1','risers',
          ['Model/279326','Model/26CCC0','Model/27CFDB','Model/27CFD5'],
          'Return inter-floor riser from repeated extract-layer section glyphs; 24 mm plan drift retained as a slight offset.')
    supply_ground=center_of('Model/26BEE7','ground');supply_upper=center_of('Model/26CCBE','upper')
    tubes([supply_ground+[3250],supply_upper+[7350]],{'shape':'rectangular','width_mm':300,'height_mm':200},
          'П1/floor-riser','П1','risers',['Model/26BEE7','Model/26CCBE','Model/27CFE4','Model/27CFCF'],
          'Supply inter-floor 300 x 200 riser from actual supply-layer section glyphs.',insulated=6)

    def body_connector(handle,floor,index,end,system,end_section=None,tag='connection'):
        body=next(e for e in body_elements if e['ventilation_rule'].get('root')=='Model/'+handle and e['floor']==floor)
        start=body['ventilation_rule']['path_mm'][index]
        tubes([start,end],body['section'],floor+'/'+handle+'/'+tag,system,floor,body['source_ids'],
              'Overview fitting between measured duct port and documented terminal/section position; profile of connecting boot is assumed.',
              category='Ventilation fittings',end=end_section,rule={'kind':'overview_connector','from':body['element_id'],'unconfirmed_profile':True},
              insulated=6 if system=='П1' else 0)
    body_connector('26C1E5','ground',1,ground+[3175],'В1',section,'riser-boot')
    body_connector('26CCD8','upper',0,upper+[7275],'В1',section,'riser-boot')
    body_connector('26CCCA','upper',0,supply_upper+[7350],'П1',{'shape':'rectangular','width_mm':300,'height_mm':200},'riser-boot')
    facade=center_of('Model/26D1F3','roof')
    body_connector('26C199','ground',0,facade+[3200],'В1',tag='facade-boot')

    # Intake and natural exhaust grilles; physical face sizes from leader text.
    for system,identifier,size,z in [('П1','Model/26C1AB',(400,600),2750),('П1','Model/26C27B',(250,100),2750),
                                      ('П1','Model/26C061',(150,100),2750),('ВЕ1','Model/26C142',(200,200),3100),
                                      ('ВЕ2','Model/26C151',(150,100),3100)]:
        center=label(identifier,'ground');section={'shape':'rectangular','width_mm':size[0],'height_mm':size[1]}
        polygon=rectangle(center,size[0],30)
        prism(polygon,z,size[1],system+'/grille/'+identifier.split('/')[-1],'Ventilation terminals',system,'ground',[identifier],section,
              'Nominal face from callout; XY at leader arrow, 30 mm depth and face direction simplified.',status='accepted_assumption',
              rule={'kind':'grille','label_source':identifier})
        if system in ('ВЕ1','ВЕ2'):
            shaft=center_of('Model/26D1C8' if system=='ВЕ1' else 'Model/26D1CF','roof')
            tubes([center[:2]+[z+size[1]/2],shaft+[z+size[1]/2]],section,system+'/grille-branch',system,'ground',
                  [identifier], 'Natural exhaust inlet branch between drawn grille and shaft positions; junction simplified.')
        elif identifier=='Model/26C1AB':
            body_connector('26C152','ground',1,center[:2]+[3050],'П1',tag='intake-boot')
        elif identifier=='Model/26C27B':
            body_connector('26C26B','ground',1,center[:2]+[2800],'П1',section,'grille-drop')
        elif identifier=='Model/26C061':
            body_connector('26C317','ground',1,center[:2]+[2800],'П1',section,'grille-drop')
    # Third dryer outlet is represented diagrammatically at its callout anchor;
    # unlike the two elbow-symbol drops its detailed branch is not dimensioned.
    dryer=label('Model/26C7F3','ground')[:2]
    tubes([dryer+[3185],dryer+[1805]],{'shape':'round','diameter_mm':200},'В2/dryer-middle','В2','ground',
          ['Model/26C7F3','Model/273961'],'Third dryer extraction stub at О2 callout; exact port and tee connection require equipment coordination.',
          category='Ventilation fittings',rule={'kind':'dryer_stub','position_assumed':True})

    # Complete unit envelope takes the latest specification dimensions, keeping
    # the plan's measured centre. No hidden fan/heat exchanger geometry invented.
    a=by_id['Model/26C0F2']['source_bbox'];b=by_id['Model/26C0EC']['source_bbox']
    center=[(min(a[0][i],b[0][i])+max(a[1][i],b[1][i]))/2-frames['ground']['origin_mm'][i] for i in (0,1)]
    prism(rectangle(center,961,3378),2707,986,'П1-В1/AHU','Ventilation equipment','П1/В1','ground',
          ['Model/26C0F2','Model/26C0EC','OV.СО_REV1:4/3BD7'],{'shape':'rectangular','width_mm':961,'height_mm':986},
          'VVS021 supply/extract unit 961 x 986(h) x 3378 from REV1 specification; plan centre retained. Ceiling suspension/2707 mm base inferred from adjacent port centre, not dimensioned.',
          status='accepted_assumption',rule={'kind':'ahu','specification_size_mm':[961,3378,986],'base_assumed':True})
    # Inline fan has a drawn 220 x 550 plan body; its vertical dimension remains
    # a documented display envelope, with the duct axis +3.300 preserved.
    polygon=[local_point(p,frames['ground']) for p in world_geometry(by_id['Model/276134'])[0]]
    prism(polygon,3190,220,'В5/inline-fan','Ventilation equipment','В5','ground',['Model/276134','Model/27613E'],
          {'shape':'round','diameter_mm':100},'K100/1 simplified 220 mm high body at +3.300 axis; plan outline measured, housing height assumed.',
          status='accepted_assumption',rule={'kind':'inline_fan','outline_source':'Model/276134'})
    fan_box=bounds(polygon);fan_x=(fan_box[0][0]+fan_box[1][0])/2
    body_connector('276149','ground',0,[fan_x,fan_box[0][1],3300],'В5',tag='fan-inlet')
    body_connector('276154','ground',1,[fan_x,fan_box[1][1],3300],'В5',tag='fan-outlet')

    assumptions.extend([
        {'part':'Square terminal mounting','value_mm':2750,'status':'unconfirmed_overview_assumption','note':'Relative to each finished floor; no explicit common ceiling elevation found.'},
        {'part':'Fitting heights and flexible drops','status':'inferred','note':'Inherited from adjacent ducts. Tee envelopes overlap internally; no sheet-metal quantities or flow validation.'},
        {'part':'Equipment and hoods','status':'simplified','note':'Measured plan footprints and latest AHU specification; un-dimensioned heights, suspension and hood profiles are assumed.'},
        {'part':'Revision discrepancy','status':'recorded','note':'AVS012 plan symbols are older than VVS021 specification. Latest 961 x 986 x 3378 envelope chosen; connection offsets are provisional.'},
        {'part':'Roof offsets and penetrations','status':'coordination_required','note':'No openings cut into the preserved building. Intersections are reported separately; supports, dampers and internal product parts omitted.'},
    ])
    return {'square_terminal_assemblies':len(terminals),'round_terminal_assemblies':len(terminal_roots)-len(terminals),
            'fitting_source_assemblies':len(fittings_roots),'flexible_connectors':flex_count,'terminal_source_ids':sorted(terminal_roots)}
