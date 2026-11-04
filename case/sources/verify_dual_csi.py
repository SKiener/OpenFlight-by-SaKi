import json,numpy as np,manifold3d as M
from build_v4_camera import B,OUT,Body,load
import CameraV4Geometry as G,DualCamera as D
m=json.load(open(OUT/'geometry.json'));p={r['name']:load(OUT/r['mesh']) for r in m['parts'] if r['kind']!='setup' and not r['name'].startswith('H02')}
def hits(s,obs):return [[n,(s.m^b.m).volume()]for n,b in obs.items() if (s.m^b.m).volume()>.02]
res={'fan_source_sha256':m['fan_source_sha256'],'camera_source_sha256':m['camera_source_sha256'],'exit_checks':[],'corridor_candidates':[],'nominal_paths':{},'note':'16mm wide x0.6mm reserve,design bend radius6. Actual ribbon plugs and cable length unmodelled; assembly confirmation required.'}
for side in['right','left']:
 for angle in[-6.,-3.5,-1.]:
  f=B.frame(G.C,angle);N=np.asarray(f[2]);V=np.asarray(f[1]);start=B.loc(f,0,-13.5,-19.7);path=[start-6*np.sin(a)*N-6*(1-np.cos(a))*V for a in np.linspace(0,np.pi/2,33)];path.append(path[-1]-12*V);segments=[]
  for a,b in zip(path,path[1:]):
   tangent=(b-a)/np.linalg.norm(b-a);sn=np.cross([1,0,0],tangent)
   segments.append(M.Manifold.hull_points([q+u*np.array([1,0,0])+n*sn for q in[a,b]for u in[-8,8]for n in[-.3,.3]]))
  strip=D.transform(Body(M.Manifold.batch_boolean(segments,M.OpType.Add)),side);obs={**p,**D.camera_parts(B,side,angle,p['05_Camera_cradle_adjustable'])}
  res['exit_checks'].append({'side':side,'angle':angle,'hits':hits(strip,obs),'bounds_mm':strip.m.bounding_box()})
  if angle==-3.5:res['nominal_paths'][side]=[(q*np.array([-1,1,1]) if side=='left'else q).tolist()for q in path]
 for y in[-43,-38,-33,-29]:
  for z in([82]if side=='right' else[74,76,78,80]):
   x=36 if side=='right'else-36;s=B.box((x,y,z),(58,4,8 if side=='left'else 12));res['corridor_candidates'].append({'side':side,'center':[x,y,z],'size':[58,4,8 if side=='left'else 12],'hits':hits(s,p)})
(OUT/'dual-csi.json').write_text(json.dumps(res,indent=2));print('EXIT',[(r['side'],r['angle'],r['hits']) for r in res['exit_checks']]);print('FREE',[r for r in res['corridor_candidates']if not r['hits']])
