from pathlib import Path
import json,numpy as np,trimesh
ROOT=Path(__file__).resolve().parent;P=ROOT/'prototype';m=json.load(open(P/'geometry.json'))
svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="850" viewBox="0 0 1400 850"><rect width="1400" height="850" fill="#f8faf8"/><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10Z" fill="context-stroke"/></marker></defs><style>text{font-family:Arial,sans-serif;fill:#21302d}</style><text x="40" y="45" font-size="28">OpenFlight F · Pi-Luftführung und seitlicher Noctua</text><text x="40" y="78" font-size="17">Tatsächlicher CAD-Schnitt bei Z = 51,01 mm · Gehäuse 196 × 100 × 232 mm</text>']
def pt(x,y):return(600+x*4.3,160-y*4.3)
cols={'01_':'#7c8f88','04_':'#849baf','H03':'#397951','H04':'#c66a64','H09':'#60676b','H10':'#6d956c','H11':'#397951','K03':'#7b9dac','K07':'#adbdc8','17_':'#1c3430','18_':'#1c3430','H29':'#a07b54'}
for r in m['parts']:
 n=r['name'];c=next((col for pre,col in cols.items()if n.startswith(pre)),None)
 if c is None:continue
 mesh=trimesh.load_mesh(P/r['mesh']);sec=mesh.section(plane_origin=[0,0,51.01],plane_normal=[0,0,1])
 if sec is not None:
  for line in sec.discrete:
   pts=' '.join('%.2f,%.2f'%pt(v[0],v[1])for v in line);svg.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="1.8"/>')
def arrow(points,c):
 d='M '+' L '.join('%.2f %.2f'%pt(*p)for p in points);svg.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="4" marker-end="url(#arrow)"/>')
arrow([(-118,-54),(-96,-54)],'#298dac');arrow([(1,-40.5),(22,-48.5),(54,-48.5),(70,-54)],'#d57345');arrow([(94,-54),(119,-54)],'#d57345')
labels=[(80,130,'Ball / Vorderseite'),(40,680,'Frischluftgitter: 548 mm² freie Schlitzfläche'),(40,715,'Luft verteilt sich dreidimensional um Stecker und Platinen.'),(700,680,'Pi-seitiger Sammler → Noctua → seitliche Abluft'),(700,715,'Kanal geometrisch durchgängig; kleinster Schnitt 609 mm².')]
for x,y,s in labels:svg.append(f'<text x="{x}" y="{y}" font-size="19">{s}</text>')
svg.append('<text x="40" y="790" font-size="17">Pfeile kennzeichnen die beabsichtigte Luftführung, keine CFD-Simulation. Pi Active Cooler bleibt montiert.</text><text x="40" y="820" font-size="17">Die tatsächliche Kühlwirkung und der Einfluss realer Kabel werden beim geschlossenen Aufbau geprüft.</text></svg>');(ROOT/'Luftfuehrung_Schnitt.svg').write_text(''.join(svg))
