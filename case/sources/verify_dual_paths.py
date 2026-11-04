import json,numpy as np
import CameraV4Geometry as G
import DualCamera as DG
from build_v4_camera import OUT,B,load
meta=json.load(open(OUT/'geometry.json'));p={r['name']:load(OUT/r['mesh']) for r in meta['parts'] if r['kind']!='setup' and not r['name'].startswith('H02')}
rows=[]
sets=[('display',['09_Front_display_panel','03_Display_bracket_left','03_Display_bracket_right','H01_Elecrow_DLH05170B_164p9x102x15p15','K01_Display_back_PCB_buttons','K02_Display_straight_HDMI_USB'],B.frame((0,-36.58,180),25),['02_','06_','A01_']),('front',['02_Front_sensor_panel','06_PTFE_clamp','A01_PTFE_0p2_CUT_ONLY'],B.frame((0,0,0),5),[])]
for name,group,fr,skip in sets:
 fixed={n:b for n,b in p.items() if n not in group and not n.startswith(tuple(skip))}
 if name=='display':
  for side in['left','right']:fixed.update(DG.camera_parts(B,side,-1.,p['05_Camera_cradle_adjustable']))
 n=np.array(fr[2]);v=np.array(fr[1]);path=[d*n-2*v for d in np.linspace(45,0,46)]+[d*v for d in np.linspace(-2,0,21)[1:]];hits=[]
 for i,t in enumerate(path):
  for a in group:
   for k,b in fixed.items():
    vol=(p[a].m.translate(t)^b.m).volume()
    if vol>.03:hits.append([i,a,k,vol])
 rows.append({'assembly':name,'camera_pitch_for_installation':-1. if name=='display' else -3.5,'poses':len(path),'hits':hits});print(name,len(hits),hits[:5],flush=True)
(OUT/'dual-front-paths.json').write_text(json.dumps(rows,indent=2))
