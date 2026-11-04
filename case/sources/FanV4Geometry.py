"""Noctua side exhaust and two-part Pi-side collection duct; mm.

No temperature/CFD claim. Noctua reference envelope40x40x20,22inclpads.
Existing camera and sensor datums do not change. SideCSheads staywithinX98.
"""
import math,numpy as np,manifold3d as M
from manifold_backend import Body
FAN=(82.2,-54.,51.)
XAX=(1.,0.,0.)
BOLTS=[(y,z)for y in(-70.,-38.)for z in(35.,67.)]
JOIN=[(12.,-61.5),(45.,-61.5)]
SEAM=51.

def xf(x,y,z):return((x,y,z),(0,0,1),(1,0,0))

def duct_section(B,x,y,z,w,h,r):
 # Flat section normalX, u=-Y andv=Z; no fake sidewall thickness.
 return B.rounded_rect(xf(x,y,z),0,0,w,h,r,-.01,.01)

def loft(B,inner=False):
 wall=1.6;pts=[]
 # Outer and innerlofts sharestations; extendopenends slightly tocutthrough.
 sections=[(.5,-40.5,48.,18.,50.,4.),(22.,-48.5,49.,27.2,32.,8.),(58.,-48.5,49.,27.2,32.,8.),(66.7,-54.,51.,40.,40.,5.),(70.7,-54.,51.,40.,40.,5.)]
 result=None
 for a,b in zip(sections,sections[1:]):
  solids=[]
  for xx,y,z,w,h,r in[a,b]:
   if inner:
    w-=2*wall;h-=2*wall;r=max(1,r-wall)
    if xx==.5:xx-=.3
    if xx==70.7:xx+=.3
   solids.append(duct_section(B,xx,y,z,w,h,r).m)
  piece=Body(M.Manifold.batch_hull(solids))
  if result is None:result=piece
  else:B.add(result,piece)
 return result

def duct(B):
 outer=loft(B);inner=loft(B,True);B.cut(outer,inner)
 # Fan-flangecentrehasØ36 opening;cornerlands carryfour M3 nuts.
 flange=B.box((68.7,-54.,51.),(4.,40.,40.));B.cut(flange,B.cyl((0,-54,51),XAX,18,66.6,70.8));B.add(outer,flange)
 for y,z in BOLTS:
  B.add(outer,B.cyl((0,y,z),XAX,4.8,66.7,70.7))
  B.cut(outer,B.cyl((0,y,z),XAX,1.7,62.,72.))
  # Hex pocketopensinward;0.9mmfrontbearinglandretainsnut.
  nut=Body(M.Manifold.cylinder(14.25,3.35,3.35,6).rotate((0,90,0)).translate((55.,y,z)))
  B.cut(outer,nut)
 # M3 seam ears, two identical clamping axes. RearflatclearsUPS.
 for x,y in JOIN:
  ear=B.cyl((x,y,0),(0,0,1),4.,45.,57.)
  B.cut(ear,B.box((x,-80,51),(20,29.8,30))) #Ymin-65.1
  # Bridge toductouterrearwall; nostandalone pillars.
  web=B.box((x,-59.3,51),(8,8,6));B.add(ear,web);B.add(outer,ear)
  B.cut(outer,B.cyl((x,y,0),(0,0,1),1.7,44.8,57.2))
  B.cut(outer,B.cyl((x,y,0),(0,0,1),1.7,55.35,57.1,3.35))
  B.cut(outer,B.cyl((x,y,0),(0,0,1),3.35,57.,80.))
  nut=Body(M.Manifold.cylinder(17.55,3.35,3.35,6).translate((x,y,30.)));B.cut(outer,nut)
 # Split across centreline. No lipprojectsintoflow; thinsoftseamsealoptional.
 lower=Body(outer.m^B.box((0,0,SEAM-250),(1000,1000,500)).m)
 upper=Body(outer.m^B.box((0,0,SEAM+250),(1000,1000,500)).m)
 return lower,upper

def integrate(B,records,put):
 shell=records['01_Rear_shell']['body']
 # Fourloadspreadingbosses: fanpadseatX93.2,closedcountersunkwalloutside.
 for y,z in BOLTS:
  boss=B.cyl((0,y,z),XAX,5.,93.2,97.8);B.add(shell,boss)
  B.cut(shell,B.cyl((0,y,z),XAX,1.7,92.9,98.2))
  B.cut(shell,B.cyl((0,y,z),XAX,1.7,96.25,98.1,3.55))
 # Integral exhaustcollarcloseslateralbypass; cornerreliefsacceptsiliconepads.
 collar=B.rounded_rect(xf(95,-54,51),0,0,42,42,5,-2.2,2.8)
 B.cut(collar,B.cyl((0,-54,51),XAX,18,92.7,97.9))
 for y,z in BOLTS:B.cut(collar,B.box((92.65,y,z),(1.3,10.,10.)))
 B.add(shell,collar)
 # Reopenallfourshaftsandcountersinksafterunifyingtheexhaustcollar.
 for y,z in BOLTS:
  B.cut(shell,B.cyl((0,y,z),XAX,1.7,92.9,98.2))
  B.cut(shell,B.cyl((0,y,z),XAX,1.7,96.25,98.1,3.55))
 # Matching passivefresh-airintake, independentofthesideHDMIbody.
 intake=B.rounded_rect(xf(-96.4,-54,51),0,0,42,42,5,-1.4,1.4)
 B.cut(intake,B.cyl((0,-54,51),XAX,18,-97.9,-94.9));B.add(shell,intake)
 for dy in range(-15,16,5):
  h=2*math.sqrt(18**2-(abs(dy)+1.5)**2)
  B.cut(shell,B.rounded_rect(xf(-98,-54,51),-dy,0,3.,h,1.5,-1.,4.))
 # Sevenroundedverticalslots, boundedbyØ36; noouterbulge.
 for dy in range(-15,16,5):
  h=2*math.sqrt(18**2-(abs(dy)+1.5)**2)
  B.cut(shell,B.rounded_rect(xf(98,-54,51),-dy,0,3.,h,1.5,-5.,1.))
 lower,upper=duct(B)
 put('17_Noctua_airguide_lower',lower,(30,35,40),True,None,'printed')
 put('18_Noctua_airguide_upper',upper,(30,35,40),True,None,'printed')
 # Mechanical reference: 20mmframeplus1mmpadsoneachface;holes32x32.
 frame=B.box(FAN,(20,40,40));B.cut(frame,B.cyl((0,-54,51),XAX,18,71.,94.))
 for y,z in BOLTS:
  B.add(frame,B.box((82.2,y,z),(22.,8.,8.)))
  B.cut(frame,B.cyl((0,y,z),XAX,1.7,70.,94.))
 # Hub/spokes standinforblades,connectedreference—notanactualairflowmesh.
 B.add(frame,B.cyl((0,-54,51),XAX,7.,78.2,87.2))
 for aa in [0,90,180,270]:
  a=math.radians(aa);c=(82.2,-54+12*math.cos(a),51+12*math.sin(a))
  B.add(frame,B.box(c,(2.,15.,2.),(1,0,0),(0,math.cos(a),math.sin(a))))
 put('H29_Noctua_NF_A4x20_5V_PWM',frame,(147,112,84))
 for i,(y,z)in enumerate(BOLTS):
  bolt=B.cyl((0,y,z),XAX,1.5,63.,96.25);B.add(bolt,B.cyl((0,y,z),XAX,1.5,96.25,98.,3.25));put('H30_Noctua_M3x35_CS_'+str(i),bolt,(155,160,165))
  nut=Body(M.Manifold.cylinder(2.4,3.175,3.175,6).rotate((0,90,0)).translate((66.75,y,z)));B.cut(nut,B.cyl((0,y,z),XAX,1.5,66.6,69.3));put('H31_Noctua_M3_nut_'+str(i),nut,(155,160,165))
 for i,(x,y)in enumerate(JOIN):
  bolt=B.cyl((x,y,0),(0,0,1),1.5,45.,55.35);B.add(bolt,B.cyl((x,y,0),(0,0,1),1.5,55.35,57.,3.25));put('H32_Airguide_M3x12_CS_'+str(i),bolt,(155,160,165))
  nut=Body(M.Manifold.cylinder(2.4,3.175,3.175,6).translate((x,y,45.05)));B.cut(nut,B.cyl((x,y,0),(0,0,1),1.5,44.9,47.6));put('H33_Airguide_M3_nut_'+str(i),nut,(155,160,165))
