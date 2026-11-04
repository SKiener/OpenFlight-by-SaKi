import {
  BufferAttribute,
  BufferGeometry,
  CylinderGeometry,
  Mesh,
  MeshLambertMaterial,
  PlaneGeometry,
  type Object3D,
} from 'three';
import { ZONES, zoneHalfWidthM } from '../game/training';
import { FLAG_CLOTH, FLAG_HEIGHT_M, turfHeightAt } from './greens';
import { color, fairwayHalfWidth, mergeParts } from './parts';
import { groundMarkMaterial, MARK_LIFT_M, upNormals } from './tracer';

const FLAG_COLOUR = '#00ff78';
const ZONE_COLOUR = '#ffffff';
const ZONE_OPACITY = 0.16;
const ZONE_CELL_M = 3;

export interface TrainingTarget {
  place(distanceM: number | null): void;
}

function buildFlag(): Mesh {
  const cloth = (turn: number) =>
    new PlaneGeometry(FLAG_CLOTH.widthM, FLAG_CLOTH.heightM)
      .rotateY(turn)
      .translate(FLAG_CLOTH.offsetM, FLAG_HEIGHT_M - FLAG_CLOTH.dropM, 0);
  const flag = new Mesh(
    mergeParts([
      {
        geometry: new CylinderGeometry(0.11, 0.11, FLAG_HEIGHT_M, 6).translate(0, FLAG_HEIGHT_M / 2, 0),
        tint: color('#f5f5f0'),
      },
      { geometry: cloth(0), tint: color(FLAG_COLOUR) },
      { geometry: cloth(Math.PI), tint: color(FLAG_COLOUR) },
    ]),
    new MeshLambertMaterial({ vertexColors: true })
  );
  flag.name = 'training-flag';
  return flag;
}

/** The zones lie on the turf, nested, so the inner ones read brighter where they overlap. */
export function zoneBandsGeometry(distanceM: number): BufferGeometry {
  const halfWidth = fairwayHalfWidth(distanceM);
  const columns = Math.ceil((2 * halfWidth) / ZONE_CELL_M);
  const position: number[] = [];
  const index: number[] = [];
  for (const { share } of ZONES) {
    const half = zoneHalfWidthM(share, distanceM);
    const rows = Math.max(1, Math.ceil((2 * half) / ZONE_CELL_M));
    const base = position.length / 3;
    for (let row = 0; row <= rows; row++) {
      const z = -(distanceM - half + (2 * half * row) / rows);
      for (let column = 0; column <= columns; column++) {
        const x = -halfWidth + (2 * halfWidth * column) / columns;
        position.push(x, turfHeightAt(x, z) + MARK_LIFT_M, z);
      }
    }
    for (let row = 0; row < rows; row++) {
      for (let column = 0; column < columns; column++) {
        const a = base + row * (columns + 1) + column;
        const b = a + columns + 1;
        index.push(a, a + 1, b, a + 1, b + 1, b);
      }
    }
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new BufferAttribute(new Float32Array(position), 3));
  geometry.setAttribute('normal', upNormals(position.length / 3));
  geometry.setIndex(index);
  return geometry;
}

export function buildTrainingTarget(scene: Object3D): TrainingTarget {
  const flag = buildFlag();
  const bands = new Mesh(new BufferGeometry(), groundMarkMaterial(ZONE_COLOUR, ZONE_OPACITY));
  bands.name = 'training-zones';
  bands.renderOrder = -1;
  flag.visible = bands.visible = false;
  scene.add(flag, bands);
  let placedM: number | null = null;
  return {
    place(distanceM) {
      flag.visible = bands.visible = distanceM !== null;
      if (distanceM === null || distanceM === placedM) return;
      placedM = distanceM;
      flag.position.set(0, turfHeightAt(0, -distanceM), -distanceM);
      bands.geometry.dispose();
      bands.geometry = zoneBandsGeometry(distanceM);
    },
  };
}
