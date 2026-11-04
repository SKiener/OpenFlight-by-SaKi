"""V4 Flow camera mechanism, millimetres. Two installed printed parts.

One-sided M3 friction pivot plus M3 fine screw; one-screw removable phone datum.
The sidewall mount has short blind-insert bosses and retains the V4 front form.
"""
import math
import numpy as np
import manifold3d as M
CAMERA_X=63.153902
PITCH_MIN,PITCH_NOMINAL,PITCH_MAX=-6.,-3.5,-1.
LENS_FORWARD=7.
OPTICAL_HEIGHT=108.05 #1.5mm lower than C, to clear the real display envelope.
C=(CAMERA_X,-math.tan(math.radians(5))*OPTICAL_HEIGHT-LENS_FORWARD*math.cos(math.radians(PITCH_NOMINAL)),OPTICAL_HEIGHT-LENS_FORWARD*math.sin(math.radians(PITCH_NOMINAL)))
APERTURE_CENTER_Z=OPTICAL_HEIGHT
PORT_WIDTH,PORT_HEIGHT,PORT_RADIUS=20.,20.,10.
PORT_REAR_Y,PORT_REAR_Z=-25.8,110.5
FRONT_RAY_RADIUS=7.2
PUPIL_RADIUS=FRONT_RAY_RADIUS # legacy alias; this is the whole lens-front disc, not the entrance pupil.


def ring(B,fr,w,h,iw,ih,n0,n1):
 b=B.rounded_rect(fr,0,0,w,h,3,n0,n1)
 B.cut(b,B.rounded_rect(fr,0,0,iw,ih,2,n0-1,n1+1))
 return b


def port_profile(B,front=True,outer=False,offset=0.):
 w,h,r=(PORT_WIDTH,PORT_HEIGHT,PORT_RADIUS) if front else (16.5,20.,8.)
 if outer:w+=3.;h+=3.;r+=1.5
 origin=(CAMERA_X,-math.tan(math.radians(5))*APERTURE_CENTER_Z,APERTURE_CENTER_Z) if front else (CAMERA_X,PORT_REAR_Y,PORT_REAR_Z)
 fr=B.frame(origin,5. if front else 0.)
 points=[]
 for x,z,a0 in [(w/2-r,h/2-r,0),(-w/2+r,h/2-r,90),(-w/2+r,-h/2+r,180),(w/2-r,-h/2+r,270)]:
  for aa in np.linspace(a0,a0+90,17):
   a=math.radians(aa);points.append(B.loc(fr,x+r*math.cos(a),z+r*math.sin(a),offset))
 return points


def aperture_right(B):
 # A short rear bevel is subtracted only from the existing front wall.
 front=port_profile(B);origin=(CAMERA_X,-math.tan(math.radians(5))*APERTURE_CENTER_Z,APERTURE_CENTER_Z)
 fr=B.frame(origin,5.)
 back=[]
 for x,z,a0 in[(0.,0.,0),(0.,0.,90),(0.,0.,180),(0.,0.,270)]:
  for aa in np.linspace(a0,a0+90,17):
   a=math.radians(aa);back.append(B.loc(fr,x+11*math.cos(a),z+11*math.sin(a),-5.5))
 loft=M.Manifold.hull_points(front+back)
 outside=M.Manifold.hull_points(front+port_profile(B,offset=25.))
 inside=M.Manifold.hull_points(back+[p-100*np.array(fr[2]) for p in back])
 return type(B.box((0,0,0),(1,1,1)))(loft+outside+inside)


def port_outer(B):
 return type(B.box((0,0,0),(1,1,1)))(M.Manifold.hull_points(port_profile(B,outer=True)+port_profile(B,False,True)))


def view_cone(B,pitch):
 # Full rectangular angular field launched from every point of the Ø14.4 lens front.
 # Conservative outside-lens envelope; no entrance-pupil position assumption.
 # Circumscribed 32-gon contains the whole circle (no corner-ray shortcut).
 fr=B.frame(C,pitch);pts=[];r=FRONT_RAY_RADIUS/math.cos(math.pi/32)
 for n in(LENS_FORWARD,180.):
  d=n-LENS_FORWARD
  for sx in(-1,1):
   for sy in(-1,1):
    for t in np.linspace(0,2*math.pi,33)[:-1]:
     pts.append(B.loc(fr,sx*d*math.tan(math.radians(29))+r*math.cos(t),sy*d*math.tan(math.radians(19))+r*math.sin(t),n))
 return M.Manifold.hull_points(pts)


def cradle(B,pitch=PITCH_NOMINAL):
 fr=B.frame(C,pitch)
 b=ring(B,fr,37,36,25,25,-22.6,-18.6)
 B.cut(b,B.pbox(fr,0,-16,-20.6,22,12,8))
 for u in (-14.,14.):
  for v in (-14.,14.):
   B.cut(b,B.pcyl(fr,u,v,1.1,-23,-18))
   B.cut(b,B.pcyl(fr,u,v,2.1,-22.7,-20.8))
 # Two millimetres of rear-PCB component space; screw loads pass through pads.
 for u in(-14.,14.):
  for v in(-14.,14.):
   B.add(b,B.pcyl(fr,u,v,3.,-18.7,-16.6))
   B.cut(b,B.pcyl(fr,u,v,1.1,-23,-16.5))
 # Right rear dogleg lies outside the32mm PCB, front cheek clears the lens.
 B.add(b,B.pbox(fr,17.25,0,-19.05,2.5,9,7.1))
 B.add(b,B.pbox(fr,15.5,0,-7,6,9,14))
 B.add(b,B.cyl(C,(1,0,0),5,12.5,18.5))
 # Flat right cheek connects the pivot to the rear frame, all at u=18.5.
 # It is also the print-bed datum: no isolated pivot begins above the bed.
 cheek=B.pbox(fr,18.,0,-19.05,1,9,7.1)
 B.add(b,type(b)(M.Manifold.batch_hull([cheek.m,B.cyl(C,(1,0,0),5,17.5,18.5).m])))
 B.cut(b,B.cyl(C,(1,0,0),2.1,13.,18.6))
 B.cut(b,B.cyl(C,(1,0,0),1.7,12.4,13.1))
 # Fine-adjust lug at the rear, safely behind lens and PCB.
 B.add(b,B.pbox(fr,12.75,8,-27.75,11.5,4,17.5))
 # Two contact datums plus oneM3 insert locate the removable gauge.
 B.add(b,B.pbox(fr,-21.5,8,-20.6,7,8,8.4))
 p=B.loc(fr,0,8,-20.6)
 B.cut(b,B.cyl(p,(1,0,0),2.1,-25.1,-19.5))
 # Continuous V-min print webs; evaluate actual antenna clearances separately.
 def hulluvn(groups):
  pts=[B.loc(fr,u,v,n) for us,ns,vs in groups for u in us for n in ns for v in vs]
  return type(b)(M.Manifold.hull_points(pts))
 webs=[
  hulluvn([([17.5,18.5],[-22.6,.8],[-18]),([17.5,18.5],[-22.6,4.95],[-4.4])]),
  hulluvn([([17.5,18.5],[-14,.8],[-18]),([12.5,18.5],[-14,4.95],[-7.2,-4.4])]),
  hulluvn([([-18.5,-18],[-22.6,-18.6],[-3]),([-25,-18],[-24.8,-16.4],[4.1])]),
 ]
 fineweb=hulluvn([([12.5,18.5],[-36.5,-19],[-18]),([7,18.5],[-36.5,-19],[6.1])])
 # The fixed adjustment foot passes through this swept clearance window.
 B.cut(fineweb,B.pbox(fr,13.,-.425,-29.25,30,12.95,9.5))
 # Rigid local swept clearances for shaft, head and 2.5mm hex-key access.
 # Compute once in local coordinates across all pitches; the solid itself
 # does not change shape as the camera rotates.
 for radius,zlo,zhi in[(2.1,-25,.3),(3.2,-20,-15.5),(1.8,-35,-19)]:
  points=[]
  for aa in np.linspace(PITCH_MIN,PITCH_MAX,21):
   ff=B.frame(C,float(aa));tipz=fine_tip_z(float(aa))-C[2]
   for dz in(zlo,zhi):
    for t in np.linspace(0,2*math.pi,25)[:-1]:
     delta=np.array([11+radius*math.cos(t),-29+radius*math.sin(t),tipz+dz])
     points.append(B.loc(fr,delta[0],np.dot(delta,ff[1]),np.dot(delta,ff[2])))
  B.cut(fineweb,type(b)(M.Manifold.hull_points(points)))
 webs.append(fineweb)
 for web in webs:B.add(b,web)
 # Local underside relief clears the radar-carrier edge throughout pitch travel.
 B.cut(b,B.pbox(fr,18.,-18.,-6.,4.,3.,7.))
 B.cut(b,B.pbox(fr,17.25,-18.,-13.5,3.5,5.,8.5))
 for u in(-14.,14.):
  for v in(-14.,14.):B.cut(b,B.pcyl(fr,u,v,2.6,-15.3,-12.6))
 for side in(-1,1):
  pts=[B.loc(fr,u,v,n) for u,v in[(side*12.5,0),(side*12.5,12.5),(0,12.5)] for n in(-22.6,-18.6)]
  B.add(b,type(b)(M.Manifold.hull_points(pts)))
 # Integral matte-black face shield hides the PCB; no RF halfspace assumption.
 shield=B.rounded_rect(fr,-.5,-2.5,37,31,3,0.,1.2)
 B.cut(shield,B.pcyl(fr,0,0,8.3,-.1,1.3))
 for u in(-14.,14.):
  for v in(-14.,14.):B.cut(shield,B.pcyl(fr,u,v,3.,-.1,1.3))
 # Service corner clears the full display insertion path.
 B.cut(shield,B.pbox(fr,16.,25.,.6,14,30,4))
 B.add(b,shield)
 B.cut(b,B.cyl(C,(1,0,0),2.1,13.,18.6))
 B.cut(b,B.cyl(C,(1,0,0),1.7,12.4,13.1))
 B.cut(b,B.pcyl(fr,14.,-14.,3.,-15.3,3.6))
 # Re-open the camera fastener bores after adding the print webs.
 for u in(-14.,14.):
  for v in(-14.,14.):
   B.cut(b,B.pcyl(fr,u,v,1.1,-23,-16.5))
   B.cut(b,B.pcyl(fr,u,v,2.1,-22.7,-20.8))
 # Axial insertion and driver access to the lower-right M2 screw.
 B.cut(b,B.pcyl(fr,14.,-14.,2.4,-37,-20.8))
 # Inner lower corner clears the Pi RJ45 body on the mirrored left camera.
 # Three factory screw holes plus a front corner seat retain the identical PCB.
 B.cut(b,B.pbox(fr,-23.,-19.,-33.15,24.,22.,33.7))
 clip=B.pbox(fr,-14.,-14.,-13.75,6.,3.,2.5)
 pts=[B.loc(fr,u,v,n) for u in(-18.5,-16.5) for v,ns in[(-6.,(-20.,-12.5)),(-14.,(-15.,-12.5))] for n in ns]
 B.add(clip,type(b)(M.Manifold.hull_points(pts)))
 B.add(b,clip)
 # A single recessed radial line is the nominal-tilt witness mark.
 marker=B.pbox(fr,18.3,5.7,0,.6,3,.5)
 B.cut(b,marker)
 return b


def fixed_yoke(B):
 fr=B.frame(C,0)
 # Side plate keeps all structure to the right of the moving PCB.
 rear,front=-65.,C[1]+2.
 b=B.box((CAMERA_X+21.05,(rear+front)/2,121.4),(3.1,front-rear,6.8))
 B.add(b,B.cyl(C,(1,0,0),5,19.0,22.6))
 B.add(b,B.pbox(fr,20.85,8,0,3.5,12,4))
 B.cut(b,B.cyl(C,(1,0,0),1.7,18.9,22.8))
 B.cut(b,B.cyl((CAMERA_X+21.1,C[1],C[2]),(1,0,0),1.7,0,1.6,3.3))
 # TwoM3x8 screws face inward, engaging short blind bosses on the sidewall.
 for yy in (-44.,-60.):
  B.add(b,B.box((CAMERA_X+20.85,yy,122.25 if yy==-60 else 123),(3.5,10,8.5 if yy==-60 else 10)))
  B.cut(b,B.cyl((CAMERA_X,yy,123),(1,0,0),1.7,19.,22.7))
 # Fine-screw foot only reaches behind the camera frame.
 B.add(b,B.pbox(fr,15.05,0,-29,15.1,6,7))
 B.add(b,B.pbox(fr,20.85,4,-29,3.5,14,7))
 screw=(CAMERA_X+11,C[1]-29,C[2])
 B.cut(b,B.cyl(screw,(0,0,1),2.1,-3.1,2.5))
 B.cut(b,B.cyl(screw,(0,0,1),1.7,2.4,11))
 # Exact angular end stops contact only at-6 and-1 degrees.
 for angle,v in ((PITCH_MIN,11.5),(PITCH_MAX,4.5)):
  ff=B.frame(C,angle)
  B.add(b,B.pbox(ff,16.8,v,-33.5 if angle==PITCH_MIN else -30.5,11.6,3,5))
 # Nominal reference line on the fixed outer bearing.
 ff=B.frame(C,PITCH_NOMINAL)
 B.cut(b,B.pbox(ff,22.5,6.5,0,.8,2,.5))
 B.cut(b,B.cyl(screw,(0,0,1),1.7,2.4,15))
 for yy in(-44.,-60.):
  B.cut(b,B.cyl((CAMERA_X,yy,123),(1,0,0),1.7,19.,22.7))
  B.cut(b,B.cyl((CAMERA_X,yy,123),(1,0,0),3.3,19.0,20.65,1.65))
 # Clearance over the complete2mm display-insertion movement.
 B.cut(b,B.box((CAMERA_X+20.5,-17,150),(20,38,54)))
 # Clearance to the unchanged lower-right display support, including0.6mm.
 dy=-math.tan(math.radians(5))*125-math.tan(math.radians(25))*55
 D=B.frame((0,dy,180),25)
 B.cut(b,B.pcyl(D,88,-32,5.1,-100,0))
 # Clear view of the moving nominal-angle witness mark.
 B.cut(b,B.pbox(fr,20.8,6.5,0,5,3,2))
 # Recessed nominal label,4mm high,0.45mm strokes.
 z0=119.0;yy=-56.8;xx=CAMERA_X+19.1
 def seg(y,z,w,h):B.cut(b,B.box((xx,y,z),(1.6,w,h)))
 seg(yy+.75,z0+2,1.5,.45);yy+=2.0
 patterns={'3':('a','b','g','c','d'),'5':('a','f','g','c','d')}
 for ch in ['3','.','5','o']:
  if ch=='.':
   B.cut(b,B.cyl((xx,yy+.3,z0+.3),(1,0,0),.3,-.9,.9));yy+=.8;continue
  if ch=='o':continue #degree indicated by the documented angular datum
  coords={'a':(1,3.75,2,.45),'g':(1,2,2,.45),'d':(1,.25,2,.45),'b':(1.75,2.9,.45,1.8),'c':(1.75,1.1,.45,1.8),'f':(.25,2.9,.45,1.8)}
  for key in patterns[ch]:
   y,z,w,h=coords[key];seg(yy+y,z0+z,w,h)
  yy+=2.6
 return b


def shell_additions_right(B,shell,outer):
 for yy in (-44.,-60.):
  boss=B.cyl((CAMERA_X,yy,123),(1,0,0),5,22.6,36)
  x0=CAMERA_X+22.6
  poly=[(95,yy-15),(98,yy-15),(98,yy+5),(x0,yy+5),(x0,yy-5)]
  web=type(shell)(M.CrossSection([poly]).extrude(10).translate((0,0,118)))
  B.add(boss,web)
  B.boolean(boss,B.copy(outer),1)
  B.add(shell,boss)
  B.cut(shell,B.cyl((CAMERA_X,yy,123),(1,0,0),2.1,22.5,28.2))
 # The2mm hex key reaches the flush pivot through a small side access.
 B.cut(shell,B.cyl(C,(1,0,0),2.0,22.3,40))
 return shell


def fine_tip_z(pitch):
 a=math.radians(pitch)
 return C[2]-29*math.tan(a)+6/math.cos(a)-1.5*abs(math.tan(a))


def level_tool(B,pitch=PITCH_NOMINAL):
 fr=B.frame(C,pitch)
 b=B.pbox(fr,-33,8.2,31.1,6,7.6,111.8)
 B.add(b,B.pbox(fr,-20,10,75,30,4,20))
 B.add(b,B.pbox(fr,-30,8,-20.6,10,8,9.0))
 # A 45-degree rib removes the small unsupported tongue step.
 poly=[B.loc(fr,u,v,0) for u,v in [(-32,12),(-30,12),(-30,14)]]
 rib=M.Manifold.hull_points([p+n*__import__('numpy').array(fr[2]) for p in poly for n in(-24.8,-16.4)])
 B.add(b,type(b)(rib))
 # The tongue sits on v12; left vertical face seats at u=-25.
 B.add(b,B.pbox(fr,-24.75,13,-20.6,10.5,2,8.4))
 p=B.loc(fr,0,8,-20.6)
 B.cut(b,B.cyl(p,(1,0,0),1.7,-36.1,-24.8))
 return b


def hardware(B,pitch=PITCH_NOMINAL):
 fr=B.frame(C,pitch)
 pcb=B.pbox(fr,0,0,-15.8,32,32,1.6)
 for u in(-14.,14.):
  for v in(-14.,14.):B.cut(pcb,B.pcyl(fr,u,v,1.1,-17,-14))
 lens=B.pcyl(fr,0,0,7,-15,7)
 return {'H07_OV9281_RAW_V2':pcb,'H08_M12_lens':lens}


def fasteners(B,pitch=PITCH_NOMINAL):
 result={}
 # M3x10 countersunk pivot, total length includes head.
 pivot=B.cyl(C,(1,0,0),1.5,12.6,21.11)
 B.add(pivot,B.cyl(C,(1,0,0),1.5,21.1,22.6,3.0))
 result['H20_Camera_pivot_M3x10_countersunk']=pivot
 for i,yy in enumerate((-44.,-60.)):
  bolt=B.cyl((CAMERA_X,yy,123),(1,0,0),1.5,20.59,27.1)
  B.add(bolt,B.cyl((CAMERA_X,yy,123),(1,0,0),3.0,19.1,20.6,1.5))
  result['H21_Camera_mount_M3x8_CS_'+str(i)]=bolt
 tip=fine_tip_z(pitch)
 c=(CAMERA_X+11,C[1]-29,tip)
 screw=B.cyl(c,(0,0,1),1.5,-16,0)
 B.add(screw,B.cyl(c,(0,0,1),2.75,-19,-16))
 result['H22_Camera_fine_M3x16']=screw
 washer=B.cyl(C,(1,0,0),3.5,18.5,19.0)
 B.cut(washer,B.cyl(C,(1,0,0),1.6,18.4,19.1))
 result['H23_Camera_thrust_washer_0p5']=washer
 for i,x in enumerate((-65.,65.)):
  face=B.frame((0,0,0),5)
  bolt=B.pcyl(face,x,4.5,1.5,-9.4,-3.39)
  B.add(bolt,B.pcyl(face,x,4.5,2.85,-3.4,-1.75))
  result['H25_Front_capture_M3x6_'+str(i)]=bolt
 dy=-math.tan(math.radians(5))*125-math.tan(math.radians(25))*55
 D=B.frame((0,dy,180),25)
 for i,(x,v) in enumerate([(x,v) for x in(-88.,88.) for v in(-32.,32.)]):
  bolt=B.pcyl(D,x,v,1.5,-9,-2.99)
  B.add(bolt,B.pcyl(D,x,v,2.85,-3.,-1.35))
  result['H26_Display_capture_M3x6_'+str(i)]=bolt

 fr=B.frame(C,pitch)
 for i,(u,v) in enumerate((u,v) for u in(-14.,14.) for v in(-14.,14.)):
  if u==-14. and v==-14.:continue # front corner seat replaces this rear screw
  bolt=B.pcyl(fr,u,v,1.,-20.81,-12.8)
  B.add(bolt,B.pcyl(fr,u,v,1.9,-22.4,-20.8))
  result['H27_Camera_M2x8_'+str(i)]=bolt
  nut=B.pcyl(fr,u,v,2.3,-15.,-13.4)
  B.cut(nut,B.pcyl(fr,u,v,1.05,-15.1,-13.3))
  result['H28_Camera_M2_nut_'+str(i)]=nut
 return result


def gauge_fastener(B,pitch=PITCH_NOMINAL):
 fr=B.frame(C,pitch);p=B.loc(fr,0,8,-20.6)
 b=B.cyl(p,(1,0,0),1.5,-36.01,-20)
 B.add(b,B.cyl(p,(1,0,0),2.75,-39,-36))
 return b


def display_rail_relief(B):
 # Rounded local recess below the OEM mounting boss; mounting axis stays fixed.
 relief=B.rounded_rect(B.frame((78.,-39.,124.),0.),0.,0.,14.,10.4,1.,0.,23.)
 # Open the rear screw-head pocket intentionally; retain the full3mm axial seat.
 dy=-math.tan(math.radians(5))*125-math.tan(math.radians(25))*55
 D=B.frame((0,dy,180),25.)
 B.add(relief,B.rounded_rect(D,77.45,-48.5,9.,7.,1.,-21.,-13.18))
 return relief

def front_inner_relief_right(B,outer):
 # Conformal rear relief: preserve at least1.6mm along the FACE normal
 # even where the outside front blends into the display shoulder.
 fr=B.frame((0,0,0),5.)
 pocket=B.rounded_rect(fr,63.85,111.5,44.7,32.,3.,-12.,2.)
 guard=type(outer)(outer.m.translate(-1.6*np.asarray(fr[2])))
 B.boolean(pocket,guard,1)
 return pocket


# Mechanical mirror about the case centre plane. Identical real camera boards
# remain upright; only their symmetric mounting envelope is reflected.
def mirror(body):
 return type(body)(body.m.mirror((1.,0.,0.)))

def aperture(B):
 right=aperture_right(B)
 B.add(right,mirror(right))
 return right

def front_inner_relief(B,outer):
 right=front_inner_relief_right(B,outer)
 B.add(right,mirror(right))
 return right

def shell_additions(B,shell,outer):
 shell_additions_right(B,shell,outer)
 left=type(shell)(M.Manifold())
 shell_additions_right(B,left,outer)
 B.add(shell,mirror(left))
 B.cut(shell,mirror(B.cyl(C,(1,0,0),2.,22.3,40.)))
 return shell
