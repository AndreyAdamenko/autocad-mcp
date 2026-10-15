"""Drawing-backed assembly helpers. Unknown architectural parts stay explicit."""
from __future__ import annotations

import math
import re

from .geometry import bounds,norm,transform,world_geometry


def plan_frame(records,title_id,expected_x=30000,expected_y=12900,tolerance=0.25):
    title=next((r for r in records if r['id']==title_id),None)
    if not title or title['geometry']['kind']!='text':
        raise ValueError(f"Missing plan title {title_id}")
    point=transform(title['world_matrix'],title['geometry']['point'])
    columns=[]
    for r in records:
        g=r['geometry']
        if r.get('space')!='Model' or r['id'].count('/')!=1 or g['kind']!='block':
            continue
        if 'колонн' not in (r.get('effective_layer','')+' '+g.get('effective_name','')).lower():
            continue
        if not re.search(r'400\s*[xх×]\s*400',g.get('effective_name',''),re.I) or not r.get('source_bbox'):
            continue
        box=r['source_bbox']
        center=[(box[0][i]+box[1][i])/2 for i in range(3)]
        if 2000<point[1]-center[1]<22000 and abs(point[0]-center[0])<25000:
            if max(abs(box[1][i]-box[0][i]-400) for i in (0,1))>tolerance:
                raise ValueError(f"Column {r['id']} contradicts its 400x400 designation")
            columns.append(r)
    if len(columns)!=24:
        raise ValueError(f"Expected 24 source columns around {title_id}; got {len(columns)}")
    centers=[[(r['source_bbox'][0][i]+r['source_bbox'][1][i])/2 for i in range(2)] for r in columns]
    origin=[min(p[i] for p in centers) for i in range(2)]
    spans=[max(p[i] for p in centers)-origin[i] for i in range(2)]
    if abs(spans[0]-expected_x)>tolerance or abs(spans[1]-expected_y)>tolerance:
        raise ValueError(f"Column axes {spans} contradict section dimensions {expected_x}x{expected_y}")
    grid=sorted((round(p[0]-origin[0],2),round(p[1]-origin[1],2)) for p in centers)
    return {'title_id':title_id,'title':title['geometry']['text'],'origin_mm':origin,
            'axis_spans_mm':spans,'grid_mm':grid,'columns':columns}


def in_frame(record,frame,margin=1200):
    box=source_world_bounds(record)
    if not box:
        return False
    origin=frame['origin_mm']
    return all(box[0][i]>=origin[i]-margin and box[1][i]<=origin[i]+frame['axis_spans_mm'][i]+margin for i in range(2))


def source_world_bounds(record):
    """COM bounds belong to the enclosing definition; map them before selection."""
    box=record.get('source_bbox')
    if not box:
        return None
    matrix=record['world_matrix']
    return bounds([transform(matrix,[x,y,z]) for x in (box[0][0],box[1][0])
                   for y in (box[0][1],box[1][1]) for z in (box[0][2],box[1][2])])


def local_footprint(record,frame,tolerance=0.25):
    pts=closed_footprint(record,tolerance)
    origin=frame['origin_mm']
    return [[p[0]-origin[0],p[1]-origin[1],0] for p in pts]


def closed_footprint(record,tolerance=0.25):
    """Accept a closed flag or proved coincident endpoints; never bridge a gap."""
    pts,_,_,closed=world_geometry(record,tolerance)
    coincident=len(pts)>3 and norm([pts[-1][i]-pts[0][i] for i in range(3)])<=1e-6
    if not closed and not coincident:
        raise ValueError('Source contour is open')
    if coincident:
        pts=pts[:-1]
    clean=[]
    for p in pts:
        if not clean or norm([p[i]-clean[-1][i] for i in range(3)])>1e-6:
            clean.append(p)
    if len(clean)<3 or max(p[2] for p in clean)-min(p[2] for p in clean)>tolerance:
        raise ValueError('Source footprint must be horizontal before projection')
    return clean


def classify_layer(layer):
    if layer.startswith('KG_Стены_'):
        return 'Walls'
    if layer=='KG_Стена_Кирпич_Облицовка':
        return 'Facade brick'
    if layer=='KG_Утеплитель':
        return 'Insulation'
    if layer=='KG_Конструкции_50':
        return 'Unclassified construction'
    return None


def check_parameter(parameter,name):
    value=parameter.get('value_mm')
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError(f'{name} must have finite value_mm')
    if parameter.get('confirmed') is not True or not parameter.get('source_ids'):
        raise ValueError(f'{name} requires confirmed drawing evidence and source_ids')
    return float(value)


def validate_project(project):
    if len(project.get('floors',[]))<1:
        raise ValueError('At least one floor is required')
    for floor in project['floors']:
        base=check_parameter(floor['base'],'floor base')
        top=check_parameter(floor['wall_top'],'wall top')
        if top<=base:
            raise ValueError('Wall top must exceed floor base')
    if check_parameter(project['column_top'],'column top')<=0:
        raise ValueError('Column top must exceed datum')
    for slab in project.get('slabs',[]):
        check_parameter(slab['top'],'slab top')
        if check_parameter(slab['thickness'],'slab thickness')<=0:
            raise ValueError('Slab thickness must be positive')
    if not project.get('evidence_drawings'):
        raise ValueError('Source drawing hashes are required')
    return project


def rectangular_slab(outer,holes,base,thickness):
    polygons=[[[h[0][0],h[0][1]],[h[1][0],h[0][1]],[h[1][0],h[1][1]],[h[0][0],h[1][1]]] for h in holes]
    return orthogonal_slab(outer,polygons,base,thickness)


def inside_polygon(point,polygon):
    x,y=point
    inside=False
    for i,a in enumerate(polygon):
        b=polygon[(i+1)%len(polygon)]
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
            inside=not inside
    return inside


def orthogonal_slab(outer,holes,base,thickness):
    """Watertight slab with orthogonal polygon openings, including notches.

    Grid cells avoid reliance on an ambiguous hole tessellation API. Internal
    vertical faces are omitted; neighbouring cap cells share welded vertices.
    """
    if thickness<=0:
        raise ValueError('Slab thickness must be positive')
    boxes=[]
    areas=[]
    for polygon in holes:
        if len(polygon)<4 or any(min(abs(p[0]-polygon[(i+1)%len(polygon)][0]),abs(p[1]-polygon[(i+1)%len(polygon)][1]))>1e-6 for i,p in enumerate(polygon)):
            raise ValueError('Slab openings must be orthogonal polygons')
        h=[[min(p[i] for p in polygon) for i in (0,1)],[max(p[i] for p in polygon) for i in (0,1)]]
        if not (outer[0][0]<h[0][0]<h[1][0]<outer[1][0] and outer[0][1]<h[0][1]<h[1][1]<outer[1][1]):
            raise ValueError('Slab opening must lie strictly inside its source outline')
        boxes.append(h)
        areas.append(abs(sum(polygon[i][0]*polygon[(i+1)%len(polygon)][1]-polygon[(i+1)%len(polygon)][0]*polygon[i][1] for i in range(len(polygon)))/2))
    for i,a in enumerate(boxes):
        for b in boxes[i+1:]:
            if min(a[1][0],b[1][0])>max(a[0][0],b[0][0]) and min(a[1][1],b[1][1])>max(a[0][1],b[0][1]):
                raise ValueError('Overlapping slab openings require explicit topology')
    xs=sorted(set([outer[0][0],outer[1][0]]+[p[0] for h in holes for p in h]))
    ys=sorted(set([outer[0][1],outer[1][1]]+[p[1] for h in holes for p in h]))
    cells=set()
    for i in range(len(xs)-1):
        for j in range(len(ys)-1):
            x,y=(xs[i]+xs[i+1])/2,(ys[j]+ys[j+1])/2
            if not any(inside_polygon([x,y],h) for h in holes):
                cells.add((i,j))
    vertices,faces,lookup=[],[],{}
    def vertex(i,j,k):
        key=(i,j,k)
        if key not in lookup:
            lookup[key]=len(vertices)
            vertices.append([xs[i],ys[j],base+k*thickness])
        return lookup[key]
    for i,j in sorted(cells):
        corners=[(i,j),(i+1,j),(i+1,j+1),(i,j+1)]
        bottom=[vertex(a,b,0) for a,b in corners]
        top=[vertex(a,b,1) for a,b in corners]
        faces.extend([list(reversed(bottom)),top])
        neighbours=[(i,j-1),(i+1,j),(i,j+1),(i-1,j)]
        for edge,neighbour in enumerate(neighbours):
            if neighbour not in cells:
                nxt=(edge+1)%4
                faces.append([bottom[edge],bottom[nxt],top[nxt],top[edge]])
    area=(outer[1][0]-outer[0][0])*(outer[1][1]-outer[0][1])-sum(areas)
    grid_area=sum((xs[i+1]-xs[i])*(ys[j+1]-ys[j]) for i,j in cells)
    if abs(area-grid_area)>max(1e-3,abs(area)*1e-10):
        raise ValueError('Opening topology contradicts polygon area')
    return vertices,faces,area*thickness
