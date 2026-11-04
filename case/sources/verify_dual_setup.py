"""Both camera adjustment/phone/lens/tool states; setup gauges used one at a time."""
import json,numpy as np,manifold3d as M
import CameraV4Geometry as G
import DualCamera as DG
from build_v4_camera import OUT,B,Body,load,mesh
meta=json.load(open(OUT/'geometry.json'));p={r['name']:load(OUT/r['mesh']) for r in meta['parts'] if r['kind']!='setup' and not r['name'].startswith('H02')}
report={k:[] for k in['internal','gauge','gauge_bolt','phone','gauge_removal','lens','J1','fine_key','pivot_key','m2_access']};tool_variants={}
def hits(tool,obs,eps=.02):
 rows=[]
 for n,b in obs.items():
  v=(tool.m^b.m).volume()
  if v>eps:rows.append([n,v])
 return rows
for side in['right','left']:
 tr=lambda b:DG.transform(b,side);name=lambda n:DG.left_name(n) if side=='left' else n
 cradle_name='05_Camera_cradle_adjustable' if side=='right' else DG.LEFT_CRADLE
 tool_variants[side]={q:[] for q in['down','front']}
 for angle in[-6.,-3.5,-1.]:
  fr=B.frame(G.C,angle);N=np.array(fr[2]);V=np.array(fr[1]);U=np.array([1.,0,0]);mov=DG.camera_parts(B,side,angle,p['05_Camera_cradle_adjustable']);objs={**p,**mov};setup={n:b for n,b in objs.items() if not n.startswith(('02_','06_','A01_'))}
  cradle=mov[cradle_name]
  ownhw={**G.hardware(B,angle),**{n:b for n,b in G.fasteners(B,angle).items() if n.startswith(('H22','H27','H28'))}}
  report['internal'].append({'side':side,'angle':angle,'hits':hits(cradle,{name(n):tr(b) for n,b in ownhw.items()})})
  gauge_obs={n:b for n,b in setup.items() if side!='left' or not n.startswith(('K05_USB_plug_-25','K05_USB_plug_-8','K06_USB_route_-25','K06_USB_route_-8'))}
  gauge=tr(G.level_tool(B,angle));report['gauge'].append({'side':side,'angle':angle,'hits':hits(gauge,gauge_obs)})
  report['gauge_bolt'].append({'side':side,'angle':angle,'hits':hits(tr(G.gauge_fastener(B,angle)),{**gauge_obs,'own_gauge':gauge})})
  phone=tr(B.pbox(fr,-20,17,75,150,10,75));report['phone'].append({'side':side,'angle':angle,'hits':hits(phone,{**gauge_obs,'own_gauge':gauge})})
  g0=G.level_tool(B,angle);solids=[g0.m]
  for tri in mesh(g0).triangles:
   q=M.Manifold.hull_points(np.vstack([tri,tri+150*N]))
   if q.volume()>1e-7:solids.append(q)
  swept=tr(Body(M.Manifold.batch_boolean(solids,M.OpType.Add)));report['gauge_removal'].append({'side':side,'angle':angle,'continuous_travel_mm':150,'hits':hits(swept,gauge_obs,.001)})
  c=B.loc(fr,0,8,-20.6)
  for variant,axis in[('down',-V),('front',N)]:
   short=48 if side=='right' else 54
   key=B.cyl(c,U,1.6,-short,-38.9);B.add(key,B.cyl(c-short*U,axis,1.6,-.1,35 if side=='right' else 60))
   tool_variants[side][variant].append({'angle':angle,'hits':hits(tr(key),gauge_obs)})
  exclude={name('H07_OV9281_RAW_V2'),name('H08_M12_lens')}
  obs={n:b for n,b in objs.items() if n not in exclude}
  report['lens'].append({'side':side,'angle':angle,'hits':hits(tr(B.pcyl(fr,0,0,7.2,-15,87)),obs)})
  report['J1'].append({'side':side,'angle':angle,'hits':hits(tr(B.pbox(fr,0,-13.5,-18.1,20,5,3)),obs)})
  tip=G.fine_tip_z(angle);center=np.array([G.CAMERA_X+11,G.C[1]-29,tip]);depth=32 if side=='right' else 38.8;key=B.cyl(center,(0,0,1),1.6,-depth,-18.8);B.add(key,B.cyl(center+np.array([0,0,-depth]),(-1,0,0) if side=='right' else (0,-1,0),1.6,-.1,60 if side=='right' else 50))
  fineobs={n:b for n,b in objs.items() if n!=name('H22_Camera_fine_M3x16') and not n.startswith(('02_','06_','A01_'))};report['fine_key'].append({'side':side,'angle':angle,'hits':hits(tr(key),fineobs)})
  centerpcb=tr(B.rounded_rect(fr,0,0,24,24,4,-18.6,-16.6));report['internal'].append({'side':side,'angle':angle,'name':'rear_PCB_2mm','hits':hits(centerpcb,{cradle_name:cradle})})
 key=tr(B.cyl(G.C,(1,0,0),1.3,22.2,45));report['pivot_key'].append({'side':side,'hits':hits(key,{n:b for n,b in p.items() if n!=name('H20_Camera_pivot_M3x10_countersunk')})})
 fr=B.frame(G.C,G.PITCH_NOMINAL)
 for u in[-14.,14.]:
  for v in[-14.,14.]:
   if u==-14. and v==-14.:continue
   rear=B.pcyl(fr,u,v,1.9,-52.4,-20.8);B.add(rear,B.pcyl(fr,u,v,1.,-50.81,-12.8));front=B.pcyl(fr,u,v,2.8,-13.4,20)
   report['m2_access'].append({'side':side,'uv':[u,v],'rear_hits':hits(tr(rear),{cradle_name:p[cradle_name]},1e-7),'front_hits':hits(tr(front),{cradle_name:p[cradle_name]},1e-7)})
report['conditions']={'left_phone_gauge':'Both Pi USB plugs (-25 and -8) and their cable tails temporarily disconnected; fixed K04 IO bodies retained. Remove gauge before reconnecting.','fine_key_left':'Actual90degLkey:20mm short axial shaft,50mm horizontal longleg rearwards(-Y); bend38.8mm below screwtip; no cable exclusions.','camera_fastening':'3 M2 screws per camera, fourth corner front seat; identical PCB unmodified.'}
report['gauge_tool_variants']=tool_variants
report['selected_gauge_tool']={s:next((k for k in['down','front'] if all(not a['hits'] for a in tool_variants[s][k])),None) for s in tool_variants}
(OUT/'dual-setup-check.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:len([r for r in v if r.get('hits') or r.get('rear_hits') or r.get('front_hits')]) for k,v in report.items() if isinstance(v,list)}));print('gauge_tool',report['selected_gauge_tool']);print('bad',[[k,r] for k,v in report.items() if isinstance(v,list) for r in v if r.get('hits') or r.get('rear_hits') or r.get('front_hits')][:12])
