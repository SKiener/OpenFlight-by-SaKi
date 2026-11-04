"""Exact millimetre mesh assembly for SolidCraft, with a native form study."""
from pathlib import Path
import json,math,hashlib
HERE=Path(__file__).resolve().parent
from build_v4_camera import OUT,FLOW
commands=json.loads((FLOW/'Flow/solidcraft_form.json').read_text())['commands']
commands.append({'command':'browser.visibility','params':{'items':['Flow_Rear_Envelope','Rear_Form','Rear_Shoulder','Full_Section'],'visible':False}})
meta=json.loads((OUT/'geometry.json').read_text());hidden=[]
for r in meta['parts']:
 n=r['name']; commands.append({'command':'file.insert_mesh','params':{'path':str(OUT/r['mesh']),'name':n}})
 col=[235,236,233] if n.startswith('01_') else [24,27,30] if r['printed'] else [15,19,22] if n.startswith('H08_') else r['rgb']
 if n.startswith('13_'):col=[240,160,40]
 commands.append({'command':'appearance.assign','params':{'bodies':[n],'color':col,'pbr':{'metallic':0,'roughness':.65,'dielectric_ior':1.45}}})
 if r['kind'] in ('clearance','setup'):hidden.append(n)
commands.append({'command':'browser.visibility','params':{'items':hidden,'visible':False}})
values={'com.openflight.flow-kamera':{'revision':'V4 Dual Camera F · Noctua','base_sc_sha256':meta['base_sc_sha256'],'dimensions_mm':[196,100,232],'status':'Exact closed STL mesh assembly; native rear-form study only. Full model is not a native parametric BRep. Physical fit/load/thermal tests pending.','camera':'Two identical OV9281 cameras X±63.153902; baseline126.307804mm; opticalZ108.05 above case bottom; nominalflush0; eachpitch-6..-1 nominal-3.5. ThreeM2screws+frontcornerseatperPCB. LeftphonegaugebeforeconnectingPiUSBcables. Software/syncnotconfigured.','fan':'NoctuaNF-A4x20_5VPWM40x40x20/22pads;centre82.2,-54,51;axis+Xexhaust;4M3x35flushsidebolts;two-piecePi-sideguide17/18;grilleinside196x100x232outerbounds;physicalthermalperformanceuntested.','front':'Two front assemblies; common2mm upward capture; no visible front screws; two underside locks. Tripod cartridge bore7.5mm by user request.','printing':'Use supplied print orientation/report;Follow newE printreview;02/03/09 require localizedsupports; not blanket support-free claim.'}}
commands.append({'command':'document.metadata.set','params':{'values':values}})
for name,yaw,pitch,target,d in [('Produkt',3.5,.25,[0,-38,116],390),('Front',math.pi,0,[0,-35,116],360),('Rueckseite',0,.18,[0,-55,116],360),('Kamera_Rechts',math.pi+.3,.15,[63,-10,108],100),('Kamera_Links',math.pi-.3,.15,[-63,-10,108],100),('Luefter',1.57,.2,[82,-54,51],100)]:
 commands.append({'command':'view.save','params':{'name':name,'camera':{'target':target,'yaw':yaw,'pitch':pitch,'distance':d,'fov':45}}})
(HERE/'solidcraft_assembly.json').write_text(json.dumps({'commands':commands},indent=2))
