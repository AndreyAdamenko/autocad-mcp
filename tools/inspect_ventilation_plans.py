"""Compact audit of root annotations and equipment glyphs in COM provenance."""
import base64,json,sys,zlib
from pathlib import Path
import bpy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.ventilation_project import column_frames,local_point,inside_plan
from autocad_mcp.ventilation import plain_text
from autocad_mcp.geometry import transform,world_geometry,bounds
ls=zlib.decompress(base64.b85decode(bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string())).decode().splitlines()
rs=[json.loads(s) for s in ls[1:]]
config=json.loads((Path(__file__).resolve().parents[1]/'evidence/ventilation/project.json').read_text(encoding='utf-8'))
frames=column_frames(rs,config['regions'])
for floor in ('ground','upper','roof'):
 print('FLOOR',floor)
 frame=frames[floor]
 for r in rs:
  if r['id'].count('/')!=1:continue
  g=r['geometry'];kind=g['kind']
  if g.get('leader_lines'):
   for line in g['leader_lines']:
    p=local_point(transform(r['world_matrix'],line['vertices'][0]),frame)
    text=plain_text(g.get('text',''))
    if -1600<p[0]<31600 and -1000<p[1]<14500 and (floor=='roof' or any(s in text.lower() for s in ['вда','раг','отсос','ве1','ве2','п1/в1','в5','в2','в3','в4','нар.'])):
     print(r['id'],'LABEL',text,'ARROW',p)
  elif kind in ('circle','polyline') and inside_plan(r,frame) and ('Выт' in r.get('layer','') or 'Оборуд' in r.get('layer','')):
   pts,_,_,closed=world_geometry(r,1);box=bounds([local_point(p,frame) for p in pts])
   if closed and max(box[1][i]-box[0][i] for i in (0,1))<2000:
    print(r['id'],kind,r['layer'],box, 'radius',g.get('radius'))
