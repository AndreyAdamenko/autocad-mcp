"""Drawing-backed helpers for the accepted overall-view modelling profile."""
from .architecture import classify_layer,closed_footprint,in_frame


def footprints(records,frame,margin=1200):
    result=[]
    for r in records:
        if r.get('space')!='Model' or r.get('status')!='ok' or r['geometry']['kind']!='polyline' or not in_frame(r,frame,margin):
            continue
        try:points=closed_footprint(r)
        except ValueError:continue
        points=[[p[i]-frame['origin_mm'][i] for i in (0,1)] for p in points]
        box=[[min(p[i] for p in points) for i in (0,1)],[max(p[i] for p in points) for i in (0,1)]]
        result.append({'id':r['id'],'layer':r['effective_layer'],'category':classify_layer(r['effective_layer']),
                       'points':points,'bbox':box})
    return result


def section_intervals(points,axis,coordinate):
    """Intersect a polygon with a line; preserve L-shaped wall footprints."""
    other=1-axis;crossings=[]
    for i,a in enumerate(points):
        b=points[(i+1)%len(points)]
        if (a[other]>coordinate)!=(b[other]>coordinate):
            crossings.append(a[axis]+(b[axis]-a[axis])*(coordinate-a[other])/(b[other]-a[other]))
    crossings.sort()
    if len(crossings)%2:raise ValueError('Polygon does not have an unambiguous line section')
    return list(zip(crossings[::2],crossings[1::2]))


def window_gaps(profiles,spans,tolerance=0.25):
    """Recover real 1050 mm breaks in the external gasblock contours."""
    return [gap for gap in exterior_gaps(profiles,spans,tolerance) if abs(gap['hi']-gap['lo']-1050)<=tolerance]


def exterior_gaps(profiles,spans,tolerance=0.25):
    """Union actual sections of exterior footprints, including RC/gasblock joins."""
    external=[p for p in profiles if p['layer'].startswith('KG_Стены_Наруж') or p['category']=='Unclassified construction']
    sides={'south':(0,-200,0),'north':(0,spans[1],spans[1]+200),
           'west':(1,-200,0),'east':(1,spans[0],spans[0]+200)}
    result=[]
    for side,(axis,lo,hi) in sides.items():
        # RC profiles are centred on the axis while gasblock profiles may align
        # to its outer face. A section 50 mm outside the axis lies inside both,
        # avoiding an unstable section exactly on a polygon boundary.
        section=lo+(150 if side in ('south','west') else 50)
        strips=[]
        for p in external:
            for a,b in section_intervals(p['points'],axis,section):
                strips.append({'lo':a,'hi':b,'ids':[p['id']]})
        merged=[]
        for strip in sorted(strips,key=lambda s:s['lo']):
            if merged and strip['lo']<=merged[-1]['hi']+tolerance:
                merged[-1]['hi']=max(merged[-1]['hi'],strip['hi']);merged[-1]['ids']+=strip['ids']
            else:merged.append(dict(strip))
        for a,b in zip(merged,merged[1:]):
            if b['lo']-a['hi']>tolerance:
                result.append({'side':side,'axis':axis,'lo':a['hi'],'hi':b['lo'],'wall_band':[lo,hi],
                               'section_coordinate_mm':section,
                               'source_ids':a['ids']+b['ids']})
    return result


def gap_bands(profiles,gap,tolerance=0.25):
    """Use matching physical layers on both flanks, preserving the source air gap."""
    axis=gap['axis'];q=1-axis;bands=[];sections=[]
    mid=gap.get('section_coordinate_mm',sum(gap['wall_band'])/2)
    for location in [gap['lo']-1,gap['hi']+1]:
        rows=[]
        for p in profiles:
            if p['category'] not in ('Walls','Unclassified construction','Insulation','Facade brick'):continue
            for lo,hi in section_intervals(p['points'],q,location):
                if hi-lo<=600 and abs((lo+hi)/2-mid)<600:
                    rows.append((p,lo,hi))
        sections.append(rows)
    for left,lo,hi in sections[0]:
        for right,a,b in sections[1]:
            if left['category']==right['category'] and max(abs(lo-a),abs(hi-b))<=tolerance:
                if not any(v['category']==left['category'] and max(abs(lo-v['perpendicular'][0]),abs(hi-v['perpendicular'][1]))<=tolerance for v in bands):
                    bands.append({'category':left['category'],'perpendicular':[lo,hi],'source_ids':[left['id'],right['id']]})
    return bands
