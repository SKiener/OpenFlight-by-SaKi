import json,numpy as np,manifold3d as M,trimesh
from pathlib import Path
from build_v4_camera import B,OUT,Body,load
import CameraV4Geometry as G,DualCamera as D
m=json.load(open(OUT/'geometry.json'));p={r['name']:load(OUT/r['mesh'])for r in m['parts']if r['kind']!='setup' and not r['name'].startswith('H02')};r=json.load(open(OUT/'dual-csi.json'))
def hits(t):return [[n,(t.m^b.m).volume()]for n,b in p.items()if(t.m^b.m).volume()>.02]
r['selected_corridors']=[q for q in r['corridor_candidates']if q['center']in[[36,-43,82],[-36,-33,80]]]
r['left_transition_checks']=[]
for e in r['exit_checks']:
 if e['side']!='left':continue
 f=B.frame(G.C,e['angle']);N=np.array(f[2]);V=np.array(f[1]);start=B.loc(f,0,-13.5,-19.7)-6*N-18*V;start[0]*=-1;end=np.array([-G.CAMERA_X,-33.,80.]);t=(end-start)/np.linalg.norm(end-start);sn=np.cross([1,0,0],t)
 s=Body(M.Manifold.hull_points([q+u*np.array([1,0,0])+n*sn for q in[start,end]for u in[-8,8]for n in[-.3,.3]]))
 r['left_transition_checks'].append({'angle':e['angle'],'start':start.tolist(),'end':end.tolist(),'hits':hits(s)})
print('transitions',r['left_transition_checks']);(OUT/'dual-csi.json').write_text(json.dumps(r,indent=2))
# Measured actual sections at both optical planes, Y/Z horizontal/vertical.
svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="1000" viewBox="0 0 1440 1000"><rect width="1440" height="1000" fill="#fafaf8"/><style>text{font-family:Arial,sans-serif;fill:#1d262a}</style><text x="40" y="40" font-size="28">OpenFlight E · zwei bündige Kameras</text>']
for j,side in enumerate(['left','right']):
 x=(-1 if side=='left'else 1)*G.CAMERA_X;ox=50+j*700
 def pt(y,z):return(ox+(y+105)*3,830-z*3)
 svg.append(f'<text x="{ox}" y="85" font-size="22">{side} · X={x:.6f} mm</text>')
 for n,b in p.items():
  if n.startswith(('K','R2','H19','H20','H21','H22','H23','H25','H26','H27','H28')):continue
  mm=trimesh.Trimesh(vertices=np.array(b.m.to_mesh().vert_properties)[:,:3],faces=np.array(b.m.to_mesh().tri_verts),process=False);sec=mm.section(plane_origin=[x,0,0],plane_normal=[1,0,0])
  if sec is not None:
   for line in sec.discrete:
    pts=' '.join('%.2f,%.2f'%pt(a[1],a[2])for a in line);svg.append(f'<polyline points="{pts}" fill="none" stroke="'+('#c85757'if n.startswith('H04')else'#568777'if n.startswith('H07')else'#7e8991')+'" stroke-width="1.2"/>')
 for a,col in[(-6,'#68a9c1'),(-3.5,'#1876a2'),(-1,'#68a9c1')]:
  f=B.frame(G.C,a);o=B.loc(f,0,0,G.LENS_FORWARD)
  for da in[-19,19]:
   ang=np.radians(a+da);end=o+np.array([0,180*np.cos(ang),180*np.sin(ang)]);s0=pt(o[1],o[2]);s1=pt(end[1],end[2]);svg.append(f'<path d="M{s0[0]},{s0[1]} L{s1[0]},{s1[1]}" stroke="{col}" stroke-width="1"/>')
 svg.append(f'<text x="{ox}" y="890" font-size="18">Z=108,050 mm · nominal −3,5° · Rücksprung 0,000 mm</text>')
svg.append('<text x="50" y="945" font-size="18">Baseline 126,307804 mm · volles Sichtfeld 58° × 38° ab Ø14,4-mm-Front.</text><text x="50" y="975" font-size="16">Geometrie-Nachweis. Endliche Radarhüllen separat geprüft; keine HF-Leistungszusage. Reale Kabel und Objektive abgleichen.</text></svg>')
Path('Kamera_Schnitt_Nachweis.svg').write_text(''.join(svg))
