from pathlib import Path
import sys,json,math,hashlib
from types import SimpleNamespace
import numpy as np
import manifold3d as M
import trimesh
from manifold_backend import Body,Backend
import CameraV4Geometry as G
import DualCamera as DG
import FanV4Geometry as FG
HERE=Path(__file__).resolve().parent
BASE=HERE/'BasisV4'
FLOW=HERE/'BasisModules'
sys.path.insert(0,str(FLOW))
from Flow.form_geometry import rear_envelope,mouth_tool,taper_mask
B=Backend();B.INTERSECTION=1
B.rounded_rear_envelope=lambda inner=False:Body(rear_envelope(inner))
OUT=HERE/'prototype';OUT.mkdir(exist_ok=True)
records={}

def put(name,b,rgb=(226,229,230),printed=False,pose=None,kind='hardware'):
 components=b.m.decompose()
 material=[c for c in components if abs(c.volume())>1e-8]
 if len(material)!=len(components):b=Body(M.Manifold.compose(material))
 if len(material)!=1:raise ValueError((name,'not one connected solid',len(material)))
 if b.m.status()!=M.Error.NoError or b.m.volume()<=0:raise ValueError((name,b.m.status()))
 records[name]={'body':b.copy(),'rgb':rgb,'printed':printed,'pose':pose,'kind':kind}

def mesh(b):
 for tol in [1e-6,1e-5,1e-4,.001,.002,.005]:
  raw=b.m.as_original().simplify(tol).to_mesh64()
  for digits in [8,9,7,6]:
   m=trimesh.Trimesh(np.asarray(raw.vert_properties)[:,:3],np.asarray(raw.tri_verts),process=False)
   m.merge_vertices(digits_vertex=digits);m.remove_unreferenced_vertices()
   variants=[m]
   clean=m.copy();clean.update_faces(clean.nondegenerate_faces(height=1e-8));clean.remove_unreferenced_vertices();variants.append(clean)
   for candidate in variants:
    if candidate.is_watertight and candidate.is_winding_consistent:
     probe=trimesh.load_mesh(trimesh.util.wrap_as_stream(candidate.export(file_type='stl_ascii')),file_type='stl')
     if probe.is_watertight and probe.is_winding_consistent:return candidate
 raise ValueError('Unable to export closed mesh from valid kernel body')

def load(p):
 m=trimesh.load_mesh(p)
 raw=M.Mesh64(np.array(m.vertices,dtype=np.float64),np.array(m.faces,dtype=np.uint64),tolerance=1e-7);raw.merge()
 b=M.Manifold(raw)
 assert b.status()==M.Error.NoError,(p,b.status())
 return Body(b)

def build():
 p=json.loads((BASE/'CAD/parameters.json').read_text())
 env=dict(p,B=B,Body=Body,M=M,np=np,math=math,put=put,POST_CUTS=[],F=SimpleNamespace(BooleanTypes=SimpleNamespace(IntersectionBooleanType=1)),mouth_tool=mouth_tool,taper_mask=taper_mask)
 exec((BASE/'CAD/geometry_helpers.py').read_text(),env)
 env['FACE']=B.frame((0,0,0),5)
 env['UPPER']=B.frame((0,env['front_y'](125),125),25)
 env['REAR']=B.frame((0,-100,116),0)
 code=(BASE/'CAD/geometry_body.py').read_text()
 start=code.index('# Corrected microphone handedness')
 end=code.index('# Original Pi/UPS stack',start)
 code=code[:start]+"# Microphone and old camera mounts removed; new mechanism follows.\n"+code[end:]
 code=code.replace('for sx in (-1,1):\n    for zz in (29,35,41):', 'for sx in ():\n    for zz in (29,35,41):')
 # New optical aperture is subtracted consistently before the front is split.
 marker="put('06_PTFE_clamp',bezel"
 code=code.replace(marker,"B.cut(bezel,G.aperture(B))\nB.cut(foil,G.aperture(B))\nB.cut(front,G.aperture(B))\n"+marker)
 code=code.replace("put('01_Rear_shell',shell","G.shell_additions(B,shell,outer)\nput('01_Rear_shell',shell")
 code=code.replace('for x in (-70,70):','for x in (-70,30):')
 code=code.replace("'K11_19mm_switch_wire_space_UNMEASURED',B.pcyl(SH,36,0,13,3,50)","'K11_19mm_switch_BODY_depth25_plus3',B.pcyl(SH,36,0,13,3.05,28)")
 # Confirmed button depth<=25mm. Wires leave the reserved body to the left,
 # then down in front of the battery retaining cap, away from camera/CSI.
 code=code.replace("put('K11_19mm_switch_BODY_depth25_plus3',B.pcyl(SH,36,0,13,3.05,28),(148,104,61),kind='clearance')", """put('K11_19mm_switch_BODY_depth25_plus3',B.pcyl(SH,36,0,13,3.05,28),(148,104,61),kind='clearance')
start=B.loc(SH,36,0,25)
N=np.asarray(SH[2]);L=np.array([-1.,0.,0.])
wirepath=[start+9*math.sin(a)*N+9*(1-math.cos(a))*L for a in np.linspace(0,math.pi/2,33)]
wirepath.append(np.array([8.,-63.,wirepath[-1][2]]))
q=wirepath[-1].copy()
wirepath += [q+8*math.sin(a)*L+np.array([0.,0.,-8*(1-math.cos(a))]) for a in np.linspace(0,math.pi/2,25)[1:]]
wirepath.append(np.array([wirepath[-1][0],wirepath[-1][1],80.]))
route=None
for a,b in zip(wirepath,wirepath[1:]):
 delta=b-a;length=float(np.linalg.norm(delta))
 piece=B.cyl(a,delta/length,2.5,-.1,length+.1)
 if route is None:route=piece
 else:B.add(route,piece)
put('K13_Switch_wire_bundle_5mm_LEFT',route,(130,160,205),kind='clearance')
""")

 # Cameraandmicno longer ride on the front: merge sensorband into lowerfront.
 code=code.replace('for v in (97.5,113)]','for v in (113,)]')
 # All removable fronts engage by the same2mm upward movement.
 code=code.replace('travel=5):','travel=2):')
 code=code.replace('    back=-th\n','    back=-th\n    web=1.6 if th>=5 else 1.0\n    skin=1.5 if th>=5 else .8\n')
 code=code.replace('back+1,-.8','back+web,-skin').replace('back+1.05','back+web+.05').replace('back-1,-.8','back-1,-skin')
 code=code.replace('if v!=113:concealed_keyhole(front,fr,x,v,5)','concealed_keyhole(front,fr,x,v,5)')
 start_relief=code.index('# Rear-only relief')
 end_relief=code.index("put('02_Front_sensor_panel'",start_relief)
 code=code[:start_relief]+"# Old lateral-entry relief/keys removed;camera loads are carried byshell.\n"+code[end_relief:]
 # Thin nuts associatedwiththeoldcamera/microphone disappearedwiththeirtracks.
 code=code.replace('1.5,-10,-4)','1.5,-9.4,-3.39)').replace('2.85,-4,-2.35)','2.85,-3.4,-1.75)')
 code=code.replace('Z,3.175,-1,16','Z,3.75,-1,16').replace('07_Tripod_insert_adapter_PILOT_ONLY','07_Tripod_insert_adapter_7p5mm')
 # Recess the OEM display screw heads:3mm pass-through instead of9.47mm.
 code=code.replace("cut(rail,B.pcyl(D,side*77.45,v,1.7,-21,-9.8))","cut(rail,B.pcyl(D,side*77.45,v,1.7,-21,-9.8))\n        cut(rail,B.pcyl(D,side*77.45,v,3.0,-21,-13.18))")
 code=code.replace('136+250-.15','126.0+250-.15').replace('136-250+.15','126.0-250+.15')
 new_relief="""
# Straight2mm vertical entry:minimum swept display allowance, no lateralcut.
clearance=B.pbox(D,0,0,-8.875,165.3,102.4,15.55)
positions=[(n,s) for n in(0,45) for s in(0,2)]
sweep=Body(M.Manifold.batch_hull([clearance.m.translate(-n*np.asarray(FACE[2])+s*np.asarray(FACE[1])) for n,s in positions]))
cut(lower,sweep)
# Bottom locking tabs need a deep, open shaft slot behind the retaining web.
for x in(-65,65):
 cut(lower,Body(M.Manifold.batch_hull([B.pcyl(FACE,x,v,1.8,-15,-3.35).m for v in(4.5,6.5)])))
 cut(lower,B.pcyl(FACE,x,6.5,3.3,-15,-1.5))
"""
 code=code.replace("put('10_Sensor_bridge',sensor_bridge,ACCENT,True,('worldZ',1),'printed')",'')
 code=code.replace('cut(lower,B.box((0,0,98.8+250-.15),(600,600,500)))','')
 code=code.replace("put('02_Front_sensor_panel',lower",new_relief+"\nput('02_Front_sensor_panel',lower")
 # Flush camera needs a local recess below the right OEM display mount.
 code=code.replace("    put('03_Display_bracket_'+label","    cut(rail,G.display_rail_relief(B) if side>0 else G.mirror(G.display_rail_relief(B)))\n    put('03_Display_bracket_'+label")
 code=code.replace("put('02_Front_sensor_panel',lower","cut(lower,G.front_inner_relief(B,outer))\nput('02_Front_sensor_panel',lower")
 # Raise only the concealed right capture by2mm for the pivot-key path.
 code=code.replace("[(FACE,x,v) for x in (-91.5,91.5) for v in (113,)]","[(FACE,x,115) for x in (-91.5,91.5)]")
 code=code.replace("B.pcyl(FACE,x,113,1.5,-10,-4)","B.pcyl(FACE,x,115,1.5,-10,-4)")
 code=code.replace("B.pcyl(FACE,x,113,2.85,-4,-2.35)","B.pcyl(FACE,x,115,2.85,-4,-2.35)")
 code=code.replace("B.pcyl(FACE,x,113,","B.pcyl(FACE,x,115,")
 env['G']=G
 exec(compile(code,'V4_Flow_NoMic_geometry','exec'),env)
 # Existing geometries not affected by this camera revision remain byte-derived
 # from the verified source meshes, not approximate re-evaluations.
 changed={'09_Front_display_panel','H19_Sensor_bridge_frame_screw_left','H19_Sensor_bridge_frame_screw_right','03_Display_bracket_left','03_Display_bracket_right','01_Rear_shell','02_Front_sensor_panel','06_PTFE_clamp','10_Sensor_bridge','A01_PTFE_0p2_CUT_ONLY'}
 meta=json.loads((BASE/'Pruefberichte/geometry.json').read_text())
 for rec in meta['parts']:
  n=rec['name']
  if n in records and n not in changed:
   records[n]['body']=load(BASE/'CAD'/rec['mesh'])
 put('05_Camera_cradle_adjustable',G.cradle(B),(30,35,40),True,None,'printed')
 put('12_Camera_fixed_yoke',G.fixed_yoke(B),(30,35,40),True,None,'printed')
 put('13_Camera_phone_reference_REMOVE_FOR_USE',G.level_tool(B),(220,140,40),True,None,'setup')
 for n,b in G.hardware(B).items():put(n,b,(40,110,75))
 for n,b in G.fasteners(B).items():put(n,b,(150,156,163))
 put('H24_SETUP_gauge_M3x16_cap',G.gauge_fastener(B),(150,156,163),kind='setup')
 # New left assembly is an exact mechanical mirror; right assembly unchanged.
 put(DG.LEFT_CRADLE,G.mirror(G.cradle(B)),(30,35,40),True,None,'printed')
 put(DG.LEFT_YOKE,G.mirror(G.fixed_yoke(B)),(30,35,40),True,None,'printed')
 put(DG.LEFT_GAUGE,G.mirror(G.level_tool(B)),(220,140,40),True,None,'setup')
 for n,b in G.hardware(B).items():put(DG.left_name(n),G.mirror(b),(40,110,75))
 for n,b in G.fasteners(B).items():
  if n.startswith(('H20','H21','H22','H23','H27','H28')):put(DG.left_name(n),G.mirror(b),(150,156,163))
 put(DG.left_name('H24_SETUP_gauge_M3x16_cap'),G.mirror(G.gauge_fastener(B)),(150,156,163),kind='setup')
 FG.integrate(B,records,put)
 output=[]
 for n,r in records.items():
  print('EXPORT',n,flush=True)
  try:m=mesh(r['body'])
  except Exception:
   raw=r['body'].m.to_mesh64()
   np.savez(OUT/('FAILED_'+n+'.npz'),vertices=np.asarray(raw.vert_properties)[:,:3],faces=np.asarray(raw.tri_verts))
   print('FAILED',n,'components',[(a.volume(),a.bounding_box()) for a in r['body'].m.decompose()],flush=True)
   raise
  folder=OUT/('assembly-STL' if r['printed'] else 'reference-meshes');folder.mkdir(exist_ok=True)
  dest=folder/(n+'.stl');m.export(dest,file_type='stl_ascii')
  check=trimesh.load_mesh(dest)
  if not(check.is_watertight and check.is_winding_consistent):print('MESH_INVALID',n,flush=True)
  solids=len(r['body'].m.decompose())
  if solids!=1:print('SOLIDS',n,solids,[(a.volume(),a.bounding_box()) for a in r['body'].m.decompose()],flush=True)
  output.append({'name':n,'printed':r['printed'],'kind':r['kind'],'rgb':r['rgb'],'mesh':str(dest.relative_to(OUT)),'bounds_mm':check.bounds.tolist(),'volume_mm3':check.volume,'closed':check.is_watertight,'solid_count':solids})
 (OUT/'geometry.json').write_text(json.dumps({'base_sc_sha256':(BASE/'SOURCE_SC_SHA256.txt').read_text().strip() if (BASE/'SOURCE_SC_SHA256.txt').exists() else hashlib.sha256((BASE/'OpenFlightCaseV4_Flow_SensorFront.sc').read_bytes()).hexdigest(),'fan_source_sha256':hashlib.sha256((HERE/'FanV4Geometry.py').read_bytes()).hexdigest(),'parts':output,'cameras':DG.datums(B),'dual_source_sha256':hashlib.sha256((HERE/'DualCamera.py').read_bytes()).hexdigest(),'pivot_mm':G.C,'lens_nominal_mm':B.loc(B.frame(G.C,G.PITCH_NOMINAL),0,0,G.LENS_FORWARD).tolist(),'camera_source_sha256':hashlib.sha256((HERE/'CameraV4Geometry.py').read_bytes()).hexdigest(),'build_source_sha256':hashlib.sha256((HERE/'build_v4_camera.py').read_bytes()).hexdigest()},indent=2))
 (OUT/'geometry_body.py').write_text(code)
 print('Built',len(output),'parts',flush=True)
 return records
if __name__=='__main__':build()
