"""Static and independently moving dual camera checks against actual exported bodies."""
import json,hashlib,math
from pathlib import Path
import numpy as np
import CameraV4Geometry as G
import DualCamera as DG
from build_v4_camera import B,Body,OUT,load
meta=json.load(open(OUT/'geometry.json'));rec={r['name']:r for r in meta['parts']};active={n:load(OUT/r['mesh']) for n,r in rec.items() if r['kind']!='setup' and not n.startswith('H02')}
base=Path(__file__).resolve().parent/'BasisE'/'CAD';oldmeta=json.load(open(base/'geometry.json'));oldrec={r['name']:r for r in oldmeta['parts'] if r['kind']!='setup' and not r['name'].startswith('H02')}
old={};changed=set(active)-set(oldrec)
for n in active.keys()&oldrec.keys():
 if hashlib.sha256((OUT/rec[n]['mesh']).read_bytes()).digest()!=hashlib.sha256((base/oldrec[n]['mesh']).read_bytes()).digest():changed.add(n)
report={'fan_source_sha256':meta['fan_source_sha256'],'source_sha256':meta['camera_source_sha256'],'build_sha256':meta['build_source_sha256'],'dual_source_sha256':meta['dual_source_sha256'],'static_new_overlaps':[],'sweep':[],'fov':[],'clearances':{},'changed_or_new_bodies':sorted(changed)}
def aabbhit(a,b):
 aa=np.array(a.m.bounding_box()).reshape(2,3);bb=np.array(b.m.bounding_box()).reshape(2,3)
 return np.all(aa[1]>bb[0]-1e-8) and np.all(bb[1]>aa[0]-1e-8)
ns=list(active)
for i,n in enumerate(ns):
 for nn in ns[i+1:]:
  if n not in changed and nn not in changed:continue
  if not aabbhit(active[n],active[nn]):continue
  v=(active[n].m^active[nn].m).volume()
  if v<=.03:continue
  before=0.
  if n in oldrec and nn in oldrec:
   for name in[n,nn]:
    if name not in old:old[name]=load(base/oldrec[name]['mesh'])
   before=(old[n].m^old[nn].m).volume()
  if v>before+.03:report['static_new_overlaps'].append([n,nn,v,before])
for side in['right','left']:
 nominal=DG.camera_parts(B,side,G.PITCH_NOMINAL,active['05_Camera_cradle_adjustable']);moving_names=set(nominal);fixed={n:b for n,b in active.items() if n not in moving_names};fine_name='H22_Camera_fine_M3x16'+('_LEFT' if side=='left' else '');yoke='12_Camera_fixed_yoke' if side=='right' else DG.LEFT_YOKE
 for angle in np.linspace(-6.,-1.,51):
  ps=DG.camera_parts(B,side,float(angle),active['05_Camera_cradle_adjustable'])
  for n,b in ps.items():
   for nn,bb in fixed.items():
    if n==fine_name and nn==yoke:continue
    if not aabbhit(b,bb):continue
    v=(b.m^bb.m).volume()
    if v>.03:report['sweep'].append([side,float(angle),n,nn,v])
  cone=DG.transform(Body(G.view_cone(B,float(angle))),side)
  objs={**fixed,**ps};own_lens='H08_M12_lens'+('_LEFT' if side=='left' else '')
  for n,b in objs.items():
   if n==own_lens or not aabbhit(b,cone):continue
   v=(b.m^cone.m).volume()
   if v>.03:report['fov'].append([side,float(angle),n,v])
 # Independent pitches cannot collide: rotation about X preserves all X ranges.
 groups={s:DG.camera_parts(B,s,G.PITCH_NOMINAL,active['05_Camera_cradle_adjustable']) for s in['left','right']}
 left_max=max(b.m.bounding_box()[3] for b in groups['left'].values());right_min=min(b.m.bounding_box()[0] for b in groups['right'].values())
report['independent_pitch_lateral_gap_mm']=float(right_min-left_max)
(OUT/'dual-geometry-check.json').write_text(json.dumps(report,indent=2));print(json.dumps({k:(len(v) if isinstance(v,list) else v) for k,v in report.items()},indent=2));print('first',report['static_new_overlaps'][:12],report['sweep'][:12],report['fov'][:8])
