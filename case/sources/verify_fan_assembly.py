"""Actual hardware, two-stage front insertion, nut/tool paths, finite fluid reserve."""
import json,hashlib,numpy as np,manifold3d as M
from build_v4_camera import OUT,HERE,B,Body,load
import FanV4Geometry as F
m=json.load(open(OUT/'geometry.json'));p={r['name']:load(OUT/r['mesh'])for r in m['parts']if r['kind']!='setup'and not r['name'].startswith('H02')};report={'fan_source_sha256':m['fan_source_sha256'],'installation':[],'tools':[],'nut_paths':[]}
def hits(t,obs,eps=.02):
 out=[]
 for n,b in obs.items():
  v=(t.m^b.m).volume()
  if v>eps:out.append([n,v])
 return out
names=[n for n in p if n.startswith(('17_','18_','H29','H31','H32','H33'))]
assembly=Body(M.Manifold.batch_boolean([p[n].m for n in names],M.OpType.Add))
obs={n:b for n,b in p.items()if n not in names and not n.startswith(('H30','02_','04_','06_','A01_','H03','H04','K_H03','K_H04','R2_'))}
# Radarcarrierandbothradarsremoved;Pi/UPS, cameras, ports andtheir cablesremain.
for dy in np.linspace(90,0,46):
 t=Body(assembly.m.translate((-.8,float(dy),0)));q=hits(t,obs)
 if q:report['installation'].append({'step':'front_slide','delta_mm':[-.8,float(dy),0],'hits':q})
for dx in np.linspace(-.8,0,17):
 t=Body(assembly.m.translate((float(dx),0,0)));q=hits(t,obs)
 if q:report['installation'].append({'step':'seat_to_right','delta_mm':[float(dx),0,0],'hits':q})
# The exportedloftends0.01paststation:0.49mmactualpadreserve; itsettlesontothefanpadswhenclamped.
report['clamp_settlement']=[]
clamp_names=[n for n in p if n.startswith(('17_','18_','H31','H32','H33'))]
for dx in np.linspace(0,.49,6):
 for n in clamp_names:
  t=Body(p[n].m.translate((float(dx),0,0)))
  q=hits(t,{nn:b for nn,b in p.items()if nn not in clamp_names})
  if q:report['clamp_settlement'].append({'body':n,'deltaX_mm':float(dx),'hits':q})
# External2mmhexkey/countersunkheadaccess+fullfanboltinsert.
for i,(y,z)in enumerate(F.BOLTS):
 key=B.cyl((0,y,z),F.XAX,1.3,97.9,130)
 report['tools'].append({'name':'outside_fan_hexkey_'+str(i),'hits':hits(key,{n:b for n,b in p.items()if n!='H30_Noctua_M3x35_CS_'+str(i)})})
 screw=B.cyl((0,y,z),F.XAX,1.5,63,130);B.add(screw,B.cyl((0,y,z),F.XAX,3.3,98.2,132))
 report['tools'].append({'name':'fan_bolt_axial_insert_'+str(i),'hits':hits(screw,{n:b for n,b in p.items()if n!='H30_Noctua_M3x35_CS_'+str(i)})})
 # Nutinsertshape,notrotating screwdrivercylinder.
 nut=Body(M.Manifold.cylinder(14.15,3.175,3.175,6).rotate((0,90,0)).translate((55,y,z)))
 report['nut_paths'].append({'name':'fan_nut_offcase_'+str(i),'hits':hits(nut,{n:p[n]for n in ['17_Noctua_airguide_lower','18_Noctua_airguide_upper']})})
for i,(x,y)in enumerate(F.JOIN):
 key=B.cyl((x,y,0),(0,0,1),1.3,56.9,90)
 report['tools'].append({'name':'seam_hexkey_OFFCASE_'+str(i),'hits':hits(key,{n:b for n,b in p.items()if n.startswith(('17_','18_','H29','H30','H31'))})})
 nut=Body(M.Manifold.cylinder(22.15,3.175,3.175,6).translate((x,y,25)))
 report['nut_paths'].append({'name':'seam_nut_offcase_'+str(i),'hits':hits(nut,{n:p[n]for n in ['17_Noctua_airguide_lower','18_Noctua_airguide_upper']})})
# Open fluiddomain inside guide, includes actual internal webs and seamhardware.
fluid=F.loft(B,True)
for n,b in p.items():
 if n.startswith(('17_','18_','H32','H33')):B.cut(fluid,b)
# Probe interiorcrosssections alongX; fluidpartsmustconnectentrancetooutlet.
rot=fluid.m.rotate((0,-90,0));areas=[{'x_mm':float(x),'area_mm2':rot.slice(float(x)).area()}for x in np.linspace(.6,70.6,141)]
report['airguide']={'components':len([q for q in fluid.m.decompose()if q.volume()>1e-6]),'crosssections':areas,'min_area_mm2':min(a['area_mm2']for a in areas),'mouth_gap_to_cooler_right_edge_mm':2.5,'scope':'Geometriccollectoropeningbesidecooler, notasealedCPUattachment; noCFD/temperatureproof.'}
bb=np.array([p[n].m.bounding_box()for n in['01_Rear_shell','02_Front_sensor_panel','09_Front_display_panel']]).reshape(-1,2,3);report['size_mm']=(bb[:,1].max(0)-bb[:,0].min(0)).tolist()
report['shell_slot_area_mm2']=sum(3*2*np.sqrt(18**2-(abs(dy)+1.5)**2)-(4-np.pi)*1.5**2 for dy in range(-15,16,5))
(OUT/'fan-assembly-check.json').write_text(json.dumps(report,indent=2));print('install',len(report['installation']),report['installation'][:2]);print('tools',[a for a in report['tools']if a['hits']]);print('nut_paths',[a for a in report['nut_paths']if a['hits']]);print('clamp_settlement',report['clamp_settlement']);print('air',report['airguide']['components'],report['airguide']['min_area_mm2'],'slots',report['shell_slot_area_mm2'],'size',report['size_mm'])
