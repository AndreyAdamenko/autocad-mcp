"""Render saved ventilation/building QA views without modifying the BLEND."""
import base64,json,sys,zlib
from pathlib import Path
import bpy
from mathutils import Vector

report=json.loads(zlib.decompress(base64.b85decode(''.join(bpy.data.texts['Architectural reconstruction audit.json.zlib.base85'].as_string().split()))).decode())
ledger={e['element_id']:e for e in report['elements']}
scene=bpy.context.scene
objects=[o for o in scene.objects if o.type=='MESH' and o.get('element_id')]
for obj in scene.objects:
    if not obj.get('element_id'):obj.hide_render=True
camera_data=bpy.data.cameras.new('Ventilation QA camera');camera=bpy.data.objects.new('Ventilation QA camera',camera_data)
scene.collection.objects.link(camera);scene.camera=camera
camera_data.type='ORTHO';camera_data.clip_end=1000
scene.render.engine='BLENDER_WORKBENCH';scene.display.shading.light='STUDIO';scene.display.shading.color_type='OBJECT'
scene.display.shading.show_shadows=True;scene.display.shading.show_cavity=True
scene.display.shading.background_type='WORLD';scene.world.color=(.055,.065,.075)
scene.render.resolution_x=1800;scene.render.resolution_y=1100;scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG'
source=Path(bpy.data.filepath);renders=[]
for view in ('exterior','networks','ground','upper','roof'):
    for obj in objects:
        item=ledger[obj['element_id']];vent=item['category'].startswith('Ventilation')
        floor=item.get('floor')
        if view=='exterior':visible=True
        elif view=='networks':visible=vent
        elif view=='ground':visible=(vent and floor in ('ground',)) or (not vent and item['base_mm']<3900 and item['top_mm']<3900)
        elif view=='upper':visible=(vent and floor in ('upper',)) or (not vent and 3900<=item['base_mm']<7800 and item['top_mm']<=7800)
        else:visible=vent and floor=='roof' or not vent and item['category'] in ('Roofing','Parapets','Cornice')
        obj.hide_render=not visible
    center=Vector((15,6.45,4.5 if view in ('exterior','networks') else {'ground':3.3,'upper':7.4,'roof':8.6}[view]))
    offset=Vector((25,-32,25)) if view in ('exterior','networks') else Vector((0,0,50))
    camera.location=center+offset;camera.rotation_euler=(-offset).to_track_quat('-Z','Y').to_euler()
    if view not in ('exterior','networks'):camera.rotation_euler=(0,0,0)
    camera_data.ortho_scale=42 if view in ('exterior','networks') else 36
    path=source.with_name(source.stem+'.'+view+'.png');scene.render.filepath=str(path)
    bpy.ops.render.render(write_still=True);renders.append(str(path))
print(json.dumps({'renders':renders}))
