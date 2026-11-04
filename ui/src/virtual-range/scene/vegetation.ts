import {
  Color,
  ConeGeometry,
  CylinderGeometry,
  IcosahedronGeometry,
  InstancedMesh,
  Matrix4,
  MeshBasicMaterial,
  MeshLambertMaterial,
  PlaneGeometry,
  Quaternion,
  Vector3,
  type BufferGeometry,
  type Scene,
} from 'three';
import { FAIRWAY, color, fairwayEdgeM, fairwayHalfWidth, mergeParts, type Spot } from './parts';
import { between, radialTexture, type Random } from './textures';

const foliageShade = (tint: Color, y: number): Color => tint.clone().multiplyScalar(0.82 + Math.min(y, 9) * 0.035);
const bushShade = (tint: Color, y: number): Color => tint.clone().multiplyScalar(0.8 + Math.min(y, 2) * 0.12);

function treeShapes() {
  const conifer = mergeParts([
    { geometry: new CylinderGeometry(0.22, 0.3, 2.4, 5).translate(0, 1.2, 0), tint: color('#5a4330') },
    { geometry: new ConeGeometry(2.6, 4.2, 7).translate(0, 3.6, 0), tint: color('#2d5a31'), shade: foliageShade },
    { geometry: new ConeGeometry(2.0, 3.6, 7).translate(0, 5.6, 0), tint: color('#2f5f33'), shade: foliageShade },
    { geometry: new ConeGeometry(1.3, 3.0, 7).translate(0, 7.4, 0), tint: color('#336536'), shade: foliageShade },
  ]);
  const broadleaf = mergeParts([
    { geometry: new CylinderGeometry(0.25, 0.35, 3.2, 5).translate(0, 1.6, 0), tint: color('#5c4532') },
    {
      geometry: new IcosahedronGeometry(2.8, 0).scale(1, 0.9, 1).translate(0, 5.0, 0),
      tint: color('#3d7536'),
      shade: foliageShade,
    },
    { geometry: new IcosahedronGeometry(1.9, 0).translate(0.9, 6.5, 0.4), tint: color('#46813c'), shade: foliageShade },
  ]);
  const poplar = mergeParts([
    { geometry: new CylinderGeometry(0.18, 0.26, 2.2, 5).translate(0, 1.1, 0), tint: color('#5a4632') },
    {
      geometry: new IcosahedronGeometry(1.45, 0).scale(1, 2.5, 1).translate(0, 5.2, 0),
      tint: color('#4a7c37'),
      shade: foliageShade,
    },
    {
      geometry: new IcosahedronGeometry(1.05, 0).scale(1, 1.9, 1).translate(0.2, 7.6, -0.15),
      tint: color('#53873d'),
      shade: foliageShade,
    },
  ]);
  const bush = mergeParts([
    {
      geometry: new IcosahedronGeometry(1.2, 0).scale(1.25, 0.8, 1.1).translate(0, 0.8, 0),
      tint: color('#3d6b31'),
      shade: bushShade,
    },
    {
      geometry: new IcosahedronGeometry(0.85, 0).scale(1.1, 0.85, 1).translate(0.85, 0.95, 0.4),
      tint: color('#46773a'),
      shade: bushShade,
    },
  ]);
  return { conifer, broadleaf, poplar, bush };
}

function probeTreeSpots(rand: Random): Spot[] {
  const spots: Spot[] = [];
  for (const side of [-1, 1]) {
    for (let down = -12; down < 322; down += between(rand, 5, 9)) {
      const rows = down < 0 ? 2 : rand() < 0.5 ? 3 : 2;
      for (let row = 0; row < rows; row++) {
        const x = side * (fairwayHalfWidth(Math.max(down, 0)) + 13 + row * 9 + between(rand, 0, 9));
        spots.push([x, -(down + between(rand, -2, 2))]);
      }
    }
  }
  for (let x = -270; x <= 270; x += between(rand, 6, 10)) {
    for (let row = 0; row < 3; row++) spots.push([x + between(rand, -3, 3), -(334 + row * 11 + between(rand, 0, 9))]);
  }
  return spots;
}

const isClear = (spots: readonly Spot[], [x, z]: Spot, gapM: number): boolean =>
  spots.every(([sx, sz]) => Math.hypot(sx - x, sz - z) >= gapM);

const TREE_SCALE: [number, number] = [1.0, 1.9];
const POPLAR_SCALE: [number, number] = [1.0, 1.5];
const BUSH_SCALE: [number, number] = [0.55, 1.05];
// Widest crown at full scale: the broadleaf's 2.9 m reach for every new tree, the bush's 1.9 m for the bushes.
const NEW_TREE_CROWN_M = 2.9 * TREE_SCALE[1];
const BUSH_CROWN_M = 1.9 * BUSH_SCALE[1];

function reachesFairway([x, z]: Spot, crownM: number): boolean {
  const down = -z;
  if (down + crownM < FAIRWAY.startM || down - crownM > FAIRWAY.endM) return false;
  return Math.abs(x) - crownM < fairwayEdgeM(down + crownM);
}

const clearOfFairway = (downM: number, crownM: number): number => fairwayEdgeM(downM + crownM) + crownM;

function extraTreeSpots(rand: Random, taken: Spot[]): Spot[] {
  const extra: Spot[] = [];
  const tryPlant = (spot: Spot) => {
    if (reachesFairway(spot, NEW_TREE_CROWN_M) || !isClear(taken, spot, 4)) return;
    taken.push(spot);
    extra.push(spot);
  };
  for (const side of [-1, 1]) {
    for (let down = 2; down < 318; down += between(rand, 3, 7)) {
      tryPlant([side * (clearOfFairway(down, NEW_TREE_CROWN_M) + between(rand, 0, 6)), -down]);
    }
  }
  const clumps: Spot[] = [
    [-128, 319],
    [-84, 324],
    [-40, 318],
    [6, 326],
    [46, 319],
    [92, 324],
    [134, 318],
  ];
  for (const [cx, down] of clumps) {
    for (let i = 0, trees = 5 + Math.floor(rand() * 5); i < trees; i++) {
      tryPlant([cx + between(rand, -10, 10), -(down + between(rand, -6, 6))]);
    }
  }
  return extra;
}

function bushSpots(rand: Random, taken: readonly Spot[]): Spot[] {
  const spots: Spot[] = [];
  for (const side of [-1, 1]) {
    for (let down = 8; down < 312; down += between(rand, 5, 11)) {
      const x = side * (clearOfFairway(down, BUSH_CROWN_M) + between(rand, 0, 3));
      for (let i = 0, group = 1 + Math.floor(rand() * 3); i < group; i++) {
        const spot: Spot = [x + side * between(rand, 0, 2.5), -(down + between(rand, -2.5, 2.5))];
        if (!reachesFairway(spot, BUSH_CROWN_M) && isClear(taken, spot, 3) && isClear(spots, spot, 1.6))
          spots.push(spot);
      }
    }
  }
  return spots;
}

interface ShadowShape {
  offset: [x: number, z: number];
  size: [x: number, z: number];
}

interface Planting {
  x: number;
  z: number;
  scale: number;
  shadow: ShadowShape;
  matrix: Matrix4;
  colour: Color;
}

const TREE_SHADOW: ShadowShape = { offset: [2.2, -1.6], size: [7, 6] };
const BUSH_SHADOW: ShadowShape = { offset: [0.8, -0.6], size: [3.6, 3.1] };

interface Stand {
  spots: Spot[];
  rand: Random;
  scale: [min: number, max: number];
}

interface Halves<T> {
  ordered: T[];
  kept: number;
}

/** Every second instance first, so showing the first half thins a row evenly. */
function halvingOrder<T>(items: readonly T[]): Halves<T> {
  const ordered = [...items.filter((_, index) => index % 2 === 0), ...items.filter((_, index) => index % 2 === 1)];
  return { ordered, kept: Math.ceil(items.length / 2) };
}

const matrix = new Matrix4();
const quaternion = new Quaternion();
const position = new Vector3();
const scale = new Vector3();
const up = new Vector3(0, 1, 0);

function plant(geometry: BufferGeometry, material: MeshLambertMaterial, stands: Stand[], shadow: ShadowShape) {
  const plantings: Planting[] = [];
  for (const { spots, rand, scale: size } of stands) {
    for (const [x, z] of spots) {
      const s = between(rand, size[0], size[1]);
      quaternion.setFromAxisAngle(up, between(rand, 0, Math.PI * 2));
      const placed = new Matrix4().compose(
        position.set(x, 0, z),
        quaternion,
        scale.set(s, s * between(rand, 0.9, 1.15), s)
      );
      const t = between(rand, 0.84, 1.12);
      const colour = new Color(t * between(rand, 0.95, 1.05), t, t * between(rand, 0.9, 1.0));
      plantings.push({ x, z, scale: s, shadow, matrix: placed, colour });
    }
  }
  const halves = halvingOrder(plantings);
  const mesh = new InstancedMesh(geometry, material, halves.ordered.length);
  halves.ordered.forEach((planting, i) => {
    mesh.setMatrixAt(i, planting.matrix);
    mesh.setColorAt(i, planting.colour);
  });
  return { mesh, halves };
}

function buildShadows(stands: Halves<Planting>[]) {
  const texture = radialTexture([
    [0, 'rgba(255,255,255,1)'],
    [0.55, 'rgba(255,255,255,0.55)'],
    [1, 'rgba(255,255,255,0)'],
  ]);
  const ordered = [
    ...stands.flatMap(({ ordered, kept }) => ordered.slice(0, kept)),
    ...stands.flatMap(({ ordered, kept }) => ordered.slice(kept)),
  ];
  const shadows = new InstancedMesh(
    new PlaneGeometry(1, 1).rotateX(-Math.PI / 2),
    new MeshBasicMaterial({
      map: texture,
      color: '#000000',
      transparent: true,
      opacity: 0.28,
      depthWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: -1,
      polygonOffsetUnits: -4,
    }),
    ordered.length
  );
  quaternion.identity();
  ordered.forEach(({ x, z, scale: s, shadow }, i) => {
    position.set(x + shadow.offset[0] * s, 0.04, z + shadow.offset[1] * s);
    matrix.compose(position, quaternion, scale.set(shadow.size[0] * s, 1, shadow.size[1] * s));
    shadows.setMatrixAt(i, matrix);
  });
  shadows.renderOrder = -4;
  return { shadows, keptCount: stands.reduce((sum, { kept }) => sum + kept, 0) };
}

export interface Vegetation {
  setHalved(halved: boolean): void;
}

export function plantVegetation(scene: Scene, rand: Random, detail: Random): Vegetation {
  const shapes = treeShapes();
  const probeSpots = probeTreeSpots(rand);
  const coniferSpots: Spot[] = [];
  const broadleafSpots: Spot[] = [];
  for (const spot of probeSpots) (rand() < 0.55 ? coniferSpots : broadleafSpots).push(spot);
  const taken = [...probeSpots];
  const extraConifers: Spot[] = [];
  const extraBroadleafs: Spot[] = [];
  const poplarSpots: Spot[] = [];
  for (const spot of extraTreeSpots(detail, taken)) {
    const pick = detail();
    (pick < 0.5 ? poplarSpots : pick < 0.75 ? extraBroadleafs : extraConifers).push(spot);
  }

  const material = new MeshLambertMaterial({ vertexColors: true });
  const forest = [
    plant(
      shapes.conifer,
      material,
      [
        { spots: coniferSpots, rand, scale: TREE_SCALE },
        { spots: extraConifers, rand: detail, scale: TREE_SCALE },
      ],
      TREE_SHADOW
    ),
    plant(
      shapes.broadleaf,
      material,
      [
        { spots: broadleafSpots, rand, scale: TREE_SCALE },
        { spots: extraBroadleafs, rand: detail, scale: TREE_SCALE },
      ],
      TREE_SHADOW
    ),
    plant(shapes.poplar, material, [{ spots: poplarSpots, rand: detail, scale: POPLAR_SCALE }], TREE_SHADOW),
    plant(shapes.bush, material, [{ spots: bushSpots(detail, taken), rand: detail, scale: BUSH_SCALE }], BUSH_SHADOW),
  ];
  const { shadows, keptCount } = buildShadows(forest.map(({ halves }) => halves));
  scene.add(...forest.map(({ mesh }) => mesh), shadows);
  return {
    setHalved(halved) {
      for (const { mesh, halves } of forest) mesh.count = halved ? halves.kept : halves.ordered.length;
      shadows.count = halved ? keptCount : shadows.instanceMatrix.count;
    },
  };
}
