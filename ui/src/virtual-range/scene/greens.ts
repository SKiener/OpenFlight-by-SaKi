import {
  BufferGeometry,
  CylinderGeometry,
  DoubleSide,
  Float32BufferAttribute,
  Mesh,
  MeshBasicMaterial,
  MeshLambertMaterial,
  PlaneGeometry,
  RingGeometry,
  Vector3,
  type Camera,
  type Color,
  type Object3D,
  type Raycaster,
} from 'three';
import type { UnitSystem } from '../../utils/units';
import { distanceLabel } from '../distance';
import { GREENS } from '../layout';
import { SIGN_PRIORITY } from './labelLayout';
import { color, fogShader, mergeParts } from './parts';
import { between, createSign, type Random, type Sign } from './textures';

type Point = [x: number, z: number];

const FRINGE_M = 1.8;
const RIM_WOBBLE = 0.1;
const greenRadiusM = (distanceM: number): number => 6 + distanceM * 0.028;
const rimDistance = (x: number, z: number, cx: number, cz: number, r: number): number =>
  Math.hypot((x - cx) / r, (z - cz) / (r * 1.25));
const domeHeight = (rim: number): number => 0.12 + 0.35 * (1 - Math.min(1, rim));

/** Top of the turf at (x, z), 0 off the greens, so marks lying on a green are not buried in its dome. */
export function turfHeightAt(x: number, z: number): number {
  let height = 0;
  for (const { distanceM, sideM } of GREENS) {
    const r = greenRadiusM(distanceM);
    const rim = rimDistance(x, z, sideM, -distanceM, r);
    // 4 cm covers both the wavy rim and the flat fan triangles between the rim points.
    if (rim <= (1 + RIM_WOBBLE) * (1 + FRINGE_M / r)) height = Math.max(height, domeHeight(rim) + 0.04);
  }
  return height;
}

function blob(rand: Random, cx: number, cz: number, rx: number, rz: number, segments: number, wobble: number): Point[] {
  const p1 = between(rand, 0, 6);
  const p2 = between(rand, 0, 6);
  const rim: Point[] = [];
  for (let i = 0; i < segments; i++) {
    const a = (i / segments) * Math.PI * 2;
    const k = 1 + wobble * (0.6 * Math.sin(2 * a + p1) + 0.4 * Math.sin(3 * a + p2));
    rim.push([cx + Math.cos(a) * rx * k, cz + Math.sin(a) * rz * k]);
  }
  return rim;
}

interface PlacedGreen {
  cx: number;
  cz: number;
  flag: string;
}

function buildTurf(rand: Random): { turf: Mesh; placed: PlacedGreen[] } {
  const position: number[] = [];
  const colours: number[] = [];
  const push = (x: number, y: number, z: number, c: Color) => {
    position.push(x, y, z);
    colours.push(c.r, c.g, c.b);
  };
  const greenCentre = color('#93d46c');
  const greenEdge = color('#7fc35a');
  const fringe = color('#68ad4b');
  const sand = color('#e2d3a6');
  const sandEdge = color('#cdbb8b');

  const placed = GREENS.map(({ distanceM, sideM, flag }) => {
    const cx = sideM;
    const cz = -distanceM;
    const r = greenRadiusM(distanceM);
    const rim = blob(rand, cx, cz, r, r * 1.25, 40, RIM_WOBBLE);
    const grow = 1 + FRINGE_M / r;
    const outer = rim.map(([x, z]): Point => [cx + (x - cx) * grow, cz + (z - cz) * grow]);
    const dome = (x: number, z: number) => domeHeight(rimDistance(x, z, cx, cz, r));
    for (let i = 0; i < rim.length; i++) {
      const [ax, az] = rim[i];
      const [bx, bz] = rim[(i + 1) % rim.length];
      push(cx, dome(cx, cz), cz, greenCentre);
      push(bx, dome(bx, bz), bz, greenEdge);
      push(ax, dome(ax, az), az, greenEdge);
      const [ox, oz] = outer[i];
      const [px, pz] = outer[(i + 1) % outer.length];
      push(ax, dome(ax, az), az, fringe);
      push(bx, dome(bx, bz), bz, fringe);
      push(ox, 0.08, oz, fringe);
      push(ox, 0.08, oz, fringe);
      push(bx, dome(bx, bz), bz, fringe);
      push(px, 0.08, pz, fringe);
    }
    const bunkers = rand() < 0.5 ? 1 : 2;
    for (let b = 0; b < bunkers; b++) {
      const side = b === 0 ? (rand() < 0.5 ? -1 : 1) : -1;
      const bx0 = cx + side * (r * 0.9 + between(rand, 1, 3));
      const bz0 = cz + r * between(rand, 0.6, 1.1);
      const brim = blob(rand, bx0, bz0, between(rand, 2.5, 3.8), between(rand, 1.8, 2.6), 20, 0.18);
      for (let i = 0; i < brim.length; i++) {
        const [ax, az] = brim[i];
        const [bx, bz] = brim[(i + 1) % brim.length];
        push(bx0, 0.07, bz0, sand);
        push(bx, 0.09, bz, sandEdge);
        push(ax, 0.09, az, sandEdge);
      }
    }
    return { cx, cz, flag };
  });

  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(position, 3));
  geometry.setAttribute('color', new Float32BufferAttribute(colours, 3));
  const turf = new Mesh(
    geometry,
    new MeshBasicMaterial({
      vertexColors: true,
      side: DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -6,
    })
  );
  turf.renderOrder = -3;
  return { turf, placed };
}

export const FLAG_HEIGHT_M = 4.2;
export const FLAG_CLOTH = Object.freeze({ widthM: 1.9, heightM: 1.15, offsetM: 1.05, dropM: 0.58 });
const FLAG_WAVE_RAD_PER_S = 5.5;

function buildFlags(placed: PlacedGreen[]) {
  const sticks = new Mesh(
    mergeParts(
      placed.map(({ cx, cz }) => ({
        geometry: new CylinderGeometry(0.11, 0.11, FLAG_HEIGHT_M, 6).translate(cx, FLAG_HEIGHT_M / 2, cz),
        tint: color('#f5f5f0'),
      }))
    ),
    new MeshLambertMaterial({ vertexColors: true })
  );

  const flagPosition: number[] = [];
  const flagU: number[] = [];
  const flagColour: number[] = [];
  const flagIndex: number[] = [];
  for (const { cx, cz, flag } of placed) {
    const plane = new PlaneGeometry(FLAG_CLOTH.widthM, FLAG_CLOTH.heightM, 8, 2).translate(
      FLAG_CLOTH.offsetM + cx,
      FLAG_HEIGHT_M - FLAG_CLOTH.dropM,
      cz
    );
    const base = flagPosition.length / 3;
    const c = color(flag);
    const p = plane.getAttribute('position');
    for (let i = 0; i < p.count; i++) {
      flagPosition.push(p.getX(i), p.getY(i), p.getZ(i));
      flagU.push((p.getX(i) - cx) / 2.0);
      flagColour.push(c.r, c.g, c.b);
    }
    for (const i of plane.index?.array ?? []) flagIndex.push(i + base);
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(flagPosition, 3));
  geometry.setAttribute('aU', new Float32BufferAttribute(flagU, 1));
  geometry.setAttribute('aColor', new Float32BufferAttribute(flagColour, 3));
  geometry.setIndex(flagIndex);
  const wave = { value: 0 };
  const flags = new Mesh(
    geometry,
    fogShader({
      uniforms: { uWave: wave },
      side: DoubleSide,
      vertex: `attribute float aU; attribute vec3 aColor; uniform float uWave;
        varying vec3 vColor; varying float vShade;
        void main() {
          vec3 p = position;
          float phase = uWave - aU * 5.0 + position.z * 0.07 + position.x * 0.11;
          p.z += sin(phase) * 0.22 * aU;
          p.y -= aU * aU * 0.1;
          vShade = 0.8 + 0.2 * cos(phase);
          vColor = aColor;
          vec4 mvPosition = modelViewMatrix * vec4(p, 1.0);
          gl_Position = projectionMatrix * mvPosition;
          #include <fog_vertex>
        }`,
      fragment: `varying vec3 vColor; varying float vShade;
        void main() {
          gl_FragColor = vec4(vColor * vShade, 1.0);
          #include <colorspace_fragment>
          #include <fog_fragment>
        }`,
    })
  );
  return { sticks, flags, wave };
}

export interface GreenSign {
  readonly id: string;
  readonly sign: Sign;
  size: number;
  priority: number;
}

export interface Greens {
  waveFlags(seconds: number): void;
  readonly signs: readonly GreenSign[];
  pick(raycaster: Raycaster): number | null;
  nearest(camera: Camera, x: number, y: number, width: number, height: number, withinPx: number): number | null;
  highlight(index: number | null): void;
}

/** `rand` continues the probe's stream right after buildEnvironment. */
const HIGHLIGHT_GAP_M = 0.6;
const SIGN_SIZE = 0.068;
const SELECTED_SIGN_SIZE = 0.085;

function buildHighlight(): Mesh {
  const ring = new Mesh(
    new RingGeometry(1, 1.06, 64).rotateX(-Math.PI / 2),
    new MeshBasicMaterial({
      color: '#ffffff',
      transparent: true,
      opacity: 0.85,
      depthWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: -4,
      polygonOffsetUnits: -8,
    })
  );
  ring.visible = false;
  return ring;
}

export function buildGreens(parent: Object3D, rand: Random, unitSystem: UnitSystem): Greens {
  const { turf, placed } = buildTurf(rand);
  const { sticks, flags, wave } = buildFlags(placed);
  const ring = buildHighlight();
  parent.add(turf, sticks, flags, ring);

  const labels = GREENS.map(({ distanceM }) => distanceLabel(distanceM, unitSystem));
  const signs = GREENS.map(({ sideM, flag }, index): GreenSign => {
    const sign = createSign(256, 112);
    sign.draw(labels[index], flag);
    sign.sprite.position.set(placed[index].cx, 5.0, placed[index].cz);
    // Each sign sits on its flag's outer side, away from the target line.
    sign.sprite.center.set(sideM < 0 ? 0.92 : 0.08, 0);
    sign.sprite.material.opacity = 0.85;
    parent.add(sign.sprite);
    return { id: `green-${index}`, sign, size: SIGN_SIZE, priority: SIGN_PRIORITY.green };
  });
  const local = new Vector3();
  return {
    signs,
    waveFlags(seconds) {
      // Phase taken modulo 2π in double precision: float32 in the shader would wobble after hours of uptime.
      wave.value = (seconds * FLAG_WAVE_RAD_PER_S) % (Math.PI * 2);
    },
    pick(raycaster) {
      const [hit] = raycaster.intersectObjects([turf, sticks, flags], false);
      if (!hit) return null;
      turf.worldToLocal(local.copy(hit.point));
      let nearest = 0;
      let nearestRim = Infinity;
      placed.forEach(({ cx, cz }, index) => {
        const rim = rimDistance(local.x, local.z, cx, cz, greenRadiusM(GREENS[index].distanceM));
        if (rim < nearestRim) [nearest, nearestRim] = [index, rim];
      });
      return nearest;
    },
    nearest(camera, x, y, width, height, withinPx) {
      let nearest: number | null = null;
      let nearestPx = withinPx;
      placed.forEach(({ cx, cz }, index) => {
        turf.localToWorld(local.set(cx, 0, cz)).project(camera);
        if (local.z > 1) return;
        const px = Math.hypot(((local.x + 1) / 2) * width - x, ((1 - local.y) / 2) * height - y);
        if (px <= nearestPx) [nearest, nearestPx] = [index, px];
      });
      return nearest;
    },
    highlight(index) {
      signs.forEach((green, i) => {
        const selected = i === index;
        if ((green.priority === SIGN_PRIORITY.selectedGreen) === selected) return;
        green.priority = selected ? SIGN_PRIORITY.selectedGreen : SIGN_PRIORITY.green;
        green.size = selected ? SELECTED_SIGN_SIZE : SIGN_SIZE;
        green.sign.draw(labels[i], GREENS[i].flag, selected);
        green.sign.sprite.material.opacity = selected ? 1 : 0.85;
      });
      ring.visible = index !== null;
      if (index === null) return;
      const { cx, cz } = placed[index];
      const radius = (greenRadiusM(GREENS[index].distanceM) + FRINGE_M) * (1 + RIM_WOBBLE) + HIGHLIGHT_GAP_M;
      ring.position.set(cx, 0.1, cz);
      ring.scale.set(radius, 1, radius * 1.25);
    },
  };
}
