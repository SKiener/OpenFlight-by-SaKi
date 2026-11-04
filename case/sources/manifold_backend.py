"""Millimetre constructive geometry adapter for the Fusion recipe. No V4 dependency."""
import math
from types import SimpleNamespace
import numpy as np
import manifold3d as M

class Body:

    def __init__(self, m):
        self.m = m

    @property
    def isSolid(self):
        return self.m.status() == M.Error.NoError and self.m.volume() > 0

    @property
    def lumps(self):
        return SimpleNamespace(count=len(self.m.decompose()))

    def copy(self):
        return Body(self.m)

class Backend:

    def __init__(self):
        self.T = self

    def pt(self, p):
        return np.asarray(p, float)

    def copy(self, b):
        return b.copy()

    def box(self, c, size, a=(1, 0, 0), b=(0, 1, 0)):
        mat = np.column_stack([a, b, np.cross(a, b), c])
        return Body(M.Manifold.cube(size, True).transform(mat))

    def cyl(self, c, axis, r, start, end, r2=None):
        axis = np.asarray(axis, float)
        axis /= np.linalg.norm(axis)
        first = np.cross(axis, [1, 0, 0] if abs(axis[0]) < 0.8 else [0, 1, 0])
        first /= np.linalg.norm(first)
        second = np.cross(axis, first)
        mat = np.column_stack([first, second, axis, np.asarray(c) + start * axis])
        return Body(M.Manifold.cylinder(end - start, r, r if r2 is None else r2, 96).transform(mat))

    def createCylinderOrCone(self, p, r, q, s):
        delta = np.asarray(q) - p
        length = float(np.linalg.norm(delta))
        return self.cyl(p, delta / length, r * 10, 0, length, s * 10)

    def add(self, a, b):
        a.m = a.m + b.m
        return a

    def cut(self, a, b):
        a.m = a.m - b.m
        return a

    def boolean(self, a, b, op):
        a.m = a.m ^ b.m
        return a

    def move(self, b, delta):
        b.m = b.m.translate(delta)
        return b

    def frame(self, origin, angle):
        a = math.radians(angle)
        return (origin, (0, -math.sin(a), math.cos(a)), (0, math.cos(a), math.sin(a)))

    def uaxis(self, fr):
        return np.asarray((1.0, 0.0, 0.0)) if fr[1][0] == 0 and fr[2][0] == 0 else np.cross(fr[2], fr[1])

    def frame_aim(self, origin, pitch, yaw):
        p, y = map(math.radians, (pitch, yaw))
        return (origin, (-math.sin(y) * math.sin(p), -math.cos(y) * math.sin(p), math.cos(p)), (math.sin(y) * math.cos(p), math.cos(y) * math.cos(p), math.sin(p)))

    def loc(self, fr, u, v, n=0):
        return np.asarray(fr[0]) + u * self.uaxis(fr) + v * np.asarray(fr[1]) + n * np.asarray(fr[2])

    def pbox(self, fr, u, v, n, w, h, d):
        return self.box(self.loc(fr, u, v, n), (w, h, d), self.uaxis(fr), fr[1])

    def pcyl(self, fr, u, v, r, n0, n1):
        return self.cyl(self.loc(fr, u, v), fr[2], r, n0, n1)

    def rounded_rect(self, fr, u, v, w, h, r, n0, n1):
        b = self.pbox(fr, u, v, (n0 + n1) / 2, w - 2 * r, h, n1 - n0)
        self.add(b, self.pbox(fr, u, v, (n0 + n1) / 2, w, h - 2 * r, n1 - n0))
        for x in [u - w / 2 + r, u + w / 2 - r]:
            for y in [v - h / 2 + r, v + h / 2 - r]:
                self.add(b, self.pcyl(fr, x, y, r, n0, n1))
        return b

    def prism_yz(self, points, width):
        return Body(M.Manifold.hull_points([[x, y, z] for x in [-width / 2, width / 2] for y, z in points]))
