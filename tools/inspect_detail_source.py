"""Inspect actual embedded COM entities and render a readable selected CAD region."""
import base64,json,re,sys,zlib
from pathlib import Path
import bpy
from mathutils import Vector
args=sys.argv[sys.argv.index('--')+1:]
lines=zlib.decompress(base64.b85decode(bpy.data.texts['AutoCAD source provenance.jsonl.zlib.base85'].as_string())).decode().splitlines()
records=[json.loads(line) for line in lines[1:] if line]
rows=[]
for r in records:
    g=r['geometry']
    if g['kind'] in ('text','dimension'):
        rows.append({'id':r['id'],'layer':r['effective_layer'],'bbox':r.get('source_bbox'),'geometry':g})
Path(args[0]).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
if len(args)>1:
    x0,y0,x1,y1=map(float,args[1:5]);camera=bpy.context.scene.camera
    camera.location=((x0+x1)/2000,(y0+y1)/2000,1000)
    camera.rotation_euler=(0,0,0);camera.data.ortho_scale=max(x1-x0,y1-y0)/1000
    camera.data.clip_end=2000
    scene=bpy.context.scene;scene.render.resolution_x=2000;scene.render.resolution_y=2000
    scene.render.filepath=str(Path(args[0]).resolve().with_suffix('.png'))
    bpy.ops.render.render(write_still=True)
