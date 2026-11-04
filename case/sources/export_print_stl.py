"""Reproduce the reviewed print orientations from the CAD builder output."""
from pathlib import Path
import json,numpy as np,trimesh
HERE=Path(__file__).resolve().parent
meta=json.loads((HERE/'prototype/geometry.json').read_text());parts={r['name']:r for r in meta['parts']}
poses=json.loads((HERE/'Drucklagen.json').read_text())['parts'];out=HERE/'print-STL';out.mkdir(exist_ok=True)
for r in poses:
 p=parts[r['part']];m=trimesh.load_mesh(HERE/'prototype'/p['mesh']);m.apply_transform(np.array(r['source_to_print_mm']))
 assert m.is_watertight and m.is_winding_consistent,r['part']
 m.export(out/(r['part']+'.stl'),file_type='stl_ascii')
print(out)
