"""Product and assembly renders directly from final CAD meshes, no fake bevels."""
import bpy,json,math
from pathlib import Path
from mathutils import Vector
HERE=Path(__file__).resolve().parent;ROOT=HERE/'prototype'
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
def mat(name,rgb):
 m=bpy.data.materials.new(name);m.diffuse_color=(*rgb,1);m.use_nodes=True
 b=m.node_tree.nodes.get('Principled BSDF');b.inputs['Base Color'].default_value=(*rgb,1);b.inputs['Roughness'].default_value=.58
 return m
white=mat('Traffic White',(.82,.83,.81));black=mat('Mattschwarz',(.012,.015,.019));foil=mat('PTFE',(.84,.85,.83));glass=mat('Display',(.018,.027,.03));orange=mat('Einstelllehre',(.88,.4,.04))
for r in json.loads((ROOT/'geometry.json').read_text())['parts']:
 n=r['name']
 if r['kind'] in ('clearance','setup'):continue
 bpy.ops.wm.stl_import(filepath=str(ROOT/r['mesh']));o=bpy.context.object;o.name=n;o.scale=(.001,)*3
 mm=white if n.startswith('01_') else black if r['printed'] else black if n.startswith('H08_') else foil if n.startswith('A01_') else glass if n.startswith(('H01','H02')) else mat(n,tuple(v/255 for v in r['rgb']))
 o.data.materials.append(mm)
 bpy.ops.object.shade_smooth_by_angle(angle=math.radians(30),keep_sharp_edges=True)
ground=mat('Backdrop',(.28,.30,.32));bpy.ops.mesh.primitive_plane_add(size=200,location=(0,0,-.001));bpy.context.object.data.materials.append(ground)
s=bpy.context.scene;s.render.engine='CYCLES';s.cycles.samples=24;s.render.resolution_x=1400;s.render.resolution_y=1300;s.render.resolution_percentage=100;s.world.color=(.22,)*3;s.view_settings.exposure=-.35
for loc,power,size in [((-.2,.4,.6),45,.5),((.3,-.4,.45),35,.4),((-.3,-.2,.3),20,.3)]:
 bpy.ops.object.light_add(type='AREA',location=loc);o=bpy.context.object;o.data.energy=power;o.data.shape='DISK';o.data.size=size;o.rotation_euler=(Vector((0,-.04,.11))-o.location).to_track_quat('-Z','Y').to_euler()
bpy.ops.object.camera_add();cam=bpy.context.object;s.camera=cam;cam.data.type='ORTHO';s.render.image_settings.file_format='PNG'
def shot(name,loc,target,scale):
 cam.location=loc;cam.data.ortho_scale=scale;cam.rotation_euler=(Vector(target)-cam.location).to_track_quat('-Z','Y').to_euler();s.render.filepath=str(HERE/(name+'.png'));bpy.ops.render.render(write_still=True)
shot('Front',(.36,.7,.34),(0,-.04,.115),.34)
shot('Luefter_Aussen',(.65,.1,.19),(.07,-.053,.057),.16)
shot('Rueckseite',(.34,-.7,.34),(0,-.04,.115),.34)
for o in bpy.data.objects:
 if o.name.startswith(('02_','06_','09_','03_','A01_','H01','H02','K01','K02','H19','H25','H26','04_','H03','H04','K_H03','K_H04','R2_')):o.hide_render=True
shot('Luefter_Montage',(.27,.35,.25),(.035,-.047,.055),.205)
bpy.ops.wm.save_as_mainfile(filepath=str(HERE/'OpenFlight_V4_Kamera.blend'))
