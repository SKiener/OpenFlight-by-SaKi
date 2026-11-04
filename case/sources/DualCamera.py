"""Independent left/right camera states for the symmetric dual-camera case."""
import numpy as np
import CameraV4Geometry as G
LEFT_CRADLE='14_Camera_cradle_LEFT'
LEFT_YOKE='15_Camera_fixed_yoke_LEFT'
LEFT_GAUGE='16_Camera_phone_reference_LEFT_REMOVE_FOR_USE'
CAMERA_PREFIXES=('H20','H21','H22','H23','H27','H28')
def left_name(n):return n+'_LEFT'
def is_left(n):return n.endswith('_LEFT') or n in (LEFT_CRADLE,LEFT_YOKE,LEFT_GAUGE)
def base_name(n):return n[:-5] if n.endswith('_LEFT') else n
def transform(b,side):return G.mirror(b) if side=='left' else b

def camera_parts(B,side,pitch,cradle_nominal=None):
 cradle=G.cradle(B) if cradle_nominal is None else cradle_nominal
 right=type(cradle)(cradle.m.translate(-np.array(G.C)).rotate([pitch-G.PITCH_NOMINAL,0,0]).translate(G.C))
 ps={'05_Camera_cradle_adjustable':right,**G.hardware(B,pitch),**{n:b for n,b in G.fasteners(B,pitch).items() if n.startswith(('H22','H27','H28'))}}
 if side=='right':return ps
 return {(LEFT_CRADLE if n=='05_Camera_cradle_adjustable' else left_name(n)):G.mirror(b) for n,b in ps.items()}

def datums(B):
 lens=B.loc(B.frame(G.C,G.PITCH_NOMINAL),0,0,G.LENS_FORWARD)
 return {'right':{'pivot_mm':list(G.C),'lens_nominal_mm':lens.tolist()},'left':{'pivot_mm':[-G.C[0],G.C[1],G.C[2]],'lens_nominal_mm':[-float(lens[0]),float(lens[1]),float(lens[2])]},'mechanical_baseline_mm':2*G.CAMERA_X,'reflection_plane':'global X=0','assumed_camera_model':'same OV9281 RAW V2 32x32mm,28mm hole grid and M12 envelope','software_or_sync_configuration_changed':False}
