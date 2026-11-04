import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  CircleGeometry,
  InstancedMesh,
  Matrix4,
  Mesh,
  MeshBasicMaterial,
  Object3D,
  PlaneGeometry,
  RingGeometry,
  ShaderMaterial,
  SphereGeometry,
  Sprite,
  SpriteMaterial,
  Vector3,
} from 'three';
import type { GroundPoint } from '../aim';
import { landingPoint, restingPoint, usableFlight, usableRoll, type RangeShot, type ScenePoint } from '../flightPath';
import type { Vec3 } from './cameraPath';
import { turfHeightAt } from './greens';
import { TARGET_LABEL_CANVAS } from './labelLayout';
import { createSign, radialTexture, type Sign } from './textures';

export const onTurf = ({ x, z }: GroundPoint): Vec3 => ({ x, y: turfHeightAt(x, z), z });

export const landingOf = (shot: RangeShot): Vec3 => onTurf(landingPoint(shot));

export const restOf = (shot: RangeShot): Vec3 => onTurf(restingPoint(shot));

export function shotDrawKey(shot: RangeShot): string {
  const landing = landingOf(shot);
  const rest = restOf(shot);
  const lengths = `${usableFlight(shot)?.length ?? 0}|${usableRoll(shot)?.length ?? 0}`;
  return `${shot.id}|${shot.carryM}|${landing.x}|${landing.z}|${rest.x}|${rest.z}|${lengths}|${shot.totalM ?? null}`;
}

export const SAMPLE_STEP_S = 0.025;
const RADIAL_SEGMENTS = 6;
/** Marks lie this far above the ground or turf under them: above sand too, and clear of z-fighting. */
export const MARK_LIFT_M = 0.12;
const ROLL_LINE_HALF_WIDTH_M = 0.12;

/** A tube along the samples; `aTime` is each ring's flight time, so the shader can grow the tracer. */
export function buildTracerGeometry(samples: readonly ScenePoint[]): BufferGeometry {
  const n = samples.length;
  const position = new Float32Array(n * RADIAL_SEGMENTS * 3);
  const normal = new Float32Array(n * RADIAL_SEGMENTS * 3);
  const time = new Float32Array(n * RADIAL_SEGMENTS);
  const tangent = new Vector3();
  const side = new Vector3();
  const up = new Vector3();
  const direction = new Vector3();
  for (let i = 0; i < n; i++) {
    const before = samples[Math.max(i - 1, 0)];
    const after = samples[Math.min(i + 1, n - 1)];
    tangent.set(after.x - before.x, after.y - before.y, after.z - before.z).normalize();
    side.crossVectors(tangent, Object3D.DEFAULT_UP).normalize();
    if (side.lengthSq() < 1e-6) side.set(1, 0, 0);
    up.crossVectors(side, tangent).normalize();
    for (let k = 0; k < RADIAL_SEGMENTS; k++) {
      const angle = (k / RADIAL_SEGMENTS) * Math.PI * 2;
      direction.copy(side).multiplyScalar(Math.cos(angle)).addScaledVector(up, Math.sin(angle));
      const vertex = i * RADIAL_SEGMENTS + k;
      position[vertex * 3] = samples[i].x;
      position[vertex * 3 + 1] = samples[i].y;
      position[vertex * 3 + 2] = samples[i].z;
      normal[vertex * 3] = direction.x;
      normal[vertex * 3 + 1] = direction.y;
      normal[vertex * 3 + 2] = direction.z;
      time[vertex] = samples[i].t - samples[0].t;
    }
  }
  const index: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    for (let k = 0; k < RADIAL_SEGMENTS; k++) {
      const a = i * RADIAL_SEGMENTS + k;
      const b = i * RADIAL_SEGMENTS + ((k + 1) % RADIAL_SEGMENTS);
      index.push(a, a + RADIAL_SEGMENTS, b, b, a + RADIAL_SEGMENTS, b + RADIAL_SEGMENTS);
    }
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new BufferAttribute(position, 3));
  geometry.setAttribute('normal', new BufferAttribute(normal, 3));
  geometry.setAttribute('aTime', new BufferAttribute(time, 1));
  geometry.setIndex(index);
  return geometry;
}

const TRACER_VERTEX = `attribute float aTime; uniform float uScale; varying float vTime; varying vec3 vNormal; varying vec3 vView;
  void main() {
    vTime = aTime;
    vNormal = normalize(normalMatrix * normal);
    float r = clamp(distance(cameraPosition, (modelMatrix * vec4(position, 1.0)).xyz) * 0.0029, 0.02, 6.0) * uScale;
    vec4 mv = modelViewMatrix * vec4(position + normal * r, 1.0);
    vView = -mv.xyz;
    gl_Position = projectionMatrix * mv;
  }`;

const CORE_FRAGMENT = `uniform float uTime; uniform float uFade; varying float vTime; varying vec3 vNormal; varying vec3 vView;
  void main() {
    if (vTime > uTime) discard;
    float head = smoothstep(uTime - 0.45, uTime, vTime);
    float facing = abs(dot(normalize(vNormal), normalize(vView)));
    vec3 c = mix(vec3(1.0, 0.33, 0.12), vec3(1.0, 0.9, 0.72), head * 0.85);
    gl_FragColor = vec4(c * (0.8 + 0.2 * facing), uFade);
    #include <colorspace_fragment>
  }`;

const GLOW_FRAGMENT = `uniform float uTime; uniform float uFade; varying float vTime; varying vec3 vNormal; varying vec3 vView;
  void main() {
    if (vTime > uTime) discard;
    float facing = abs(dot(normalize(vNormal), normalize(vView)));
    float a = pow(facing, 2.0) * 0.55 * uFade;
    gl_FragColor = vec4(vec3(1.0, 0.32, 0.1) * a, 1.0);
  }`;

export interface TracerSlot {
  show(geometry: BufferGeometry | null, fade: number): void;
  growTo(flightS: number): void;
}

function createTracerSlot(scene: Object3D, empty: BufferGeometry): TracerSlot {
  const uTime = { value: 0 };
  const uFade = { value: 1 };
  const layer = (fragmentShader: string, scale: number, additive: boolean, renderOrder: number) => {
    const mesh = new Mesh(
      empty,
      new ShaderMaterial({
        uniforms: { uTime, uFade, uScale: { value: scale } },
        transparent: true,
        depthWrite: false,
        ...(additive ? { blending: AdditiveBlending } : {}),
        vertexShader: TRACER_VERTEX,
        fragmentShader,
      })
    );
    mesh.renderOrder = renderOrder;
    mesh.frustumCulled = false;
    mesh.visible = false;
    scene.add(mesh);
    return mesh;
  };
  const core = layer(CORE_FRAGMENT, 1, false, 5);
  const glow = layer(GLOW_FRAGMENT, 3.6, true, 6);
  return {
    show(geometry, fade) {
      core.geometry = glow.geometry = geometry ?? empty;
      core.visible = glow.visible = geometry !== null;
      uFade.value = fade;
    },
    growTo(flightS) {
      uTime.value = flightS;
    },
  };
}

export interface LandingDots {
  /** The current shot's dot comes last; y is the ground there. */
  place(points: readonly Vec3[]): void;
  showLast(visible: boolean): void;
}

export function groundMarkMaterial(color: string, opacity: number): MeshBasicMaterial {
  return new MeshBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: -4,
    polygonOffsetUnits: -8,
  });
}

function createLandingDots(scene: Object3D): LandingDots {
  const geometry = new CircleGeometry(0.9, 20).rotateX(-Math.PI / 2);
  const material = groundMarkMaterial('#ffe2b0', 0.9);
  const matrix = new Matrix4();
  const attach = (next: InstancedMesh) => {
    next.frustumCulled = false;
    next.count = 0;
    scene.add(next);
    return next;
  };
  let mesh = attach(new InstancedMesh(geometry, material, 64));
  let placed = 0;
  return {
    place(points) {
      if (points.length > mesh.instanceMatrix.count) {
        scene.remove(mesh);
        mesh.dispose();
        mesh = attach(new InstancedMesh(geometry, material, Math.max(points.length, mesh.instanceMatrix.count * 2)));
      }
      points.forEach(({ x, y, z }, i) => mesh.setMatrixAt(i, matrix.makeTranslation(x, y + MARK_LIFT_M, z)));
      mesh.instanceMatrix.needsUpdate = true;
      placed = points.length;
      mesh.count = placed;
    },
    showLast(visible) {
      mesh.count = visible ? placed : Math.max(0, placed - 1);
    },
  };
}

export const upNormals = (vertices: number): BufferAttribute =>
  new BufferAttribute(
    new Float32Array(vertices * 3).map((_, i) => (i % 3 === 1 ? 1 : 0)),
    3
  );

function writeGroundStrip(position: Float32Array, points: readonly GroundPoint[], offset = 0): void {
  const n = points.length;
  let sideX = ROLL_LINE_HALF_WIDTH_M;
  let sideZ = 0;
  for (let i = 0; i < n; i++) {
    const before = points[Math.max(i - 1, 0)];
    const after = points[Math.min(i + 1, n - 1)];
    const length = Math.hypot(after.x - before.x, after.z - before.z);
    if (length > 1e-6) {
      const scale = ROLL_LINE_HALF_WIDTH_M / length;
      sideX = (before.z - after.z) * scale;
      sideZ = (after.x - before.x) * scale;
    }
    const { x, z } = points[i];
    const y = turfHeightAt(x, z) + MARK_LIFT_M;
    const at = offset + i * 6;
    position[at] = x + sideX;
    position[at + 1] = y;
    position[at + 2] = z + sideZ;
    position[at + 3] = x - sideX;
    position[at + 4] = y;
    position[at + 5] = z - sideZ;
  }
}

function groundStrip(points: number): BufferGeometry {
  const index: number[] = [];
  for (let a = 0; a < 2 * (points - 1); a += 2) index.push(a, a + 2, a + 1, a + 1, a + 2, a + 3);
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new BufferAttribute(new Float32Array(points * 6), 3));
  geometry.setAttribute('normal', upNormals(points * 2));
  geometry.setIndex(index);
  return geometry;
}

function rollLineGeometry(samples: readonly ScenePoint[]): BufferGeometry {
  const geometry = groundStrip(samples.length);
  writeGroundStrip(geometry.getAttribute('position').array as Float32Array, samples);
  return geometry;
}

function samplesPassed(samples: readonly ScenePoint[], rolledS: number): number {
  const until = samples[0].t + rolledS;
  let lo = 0;
  let hi = samples.length;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (samples[mid].t <= until) lo = mid;
    else hi = mid;
  }
  return lo;
}

export interface RollLine {
  set(samples: readonly ScenePoint[] | null): void;
  show(rolledS: number | null): void;
}

export function buildRollLine(scene: Object3D): RollLine {
  const mesh = new Mesh(rollLineGeometry([]), groundMarkMaterial('#fff3dc', 0.8));
  mesh.visible = false;
  scene.add(mesh);
  let given: readonly ScenePoint[] | null = null;
  let line: readonly ScenePoint[] | null = null;
  return {
    set(samples) {
      if (samples === given) return;
      given = samples;
      line = samples !== null && samples.length >= 2 ? samples : null;
      if (line === null) return;
      mesh.geometry.dispose();
      mesh.geometry = rollLineGeometry(line);
    },
    show(rolledS) {
      const segments = line !== null && rolledS !== null ? samplesPassed(line, rolledS) : 0;
      mesh.geometry.setDrawRange(0, segments * 6);
      mesh.visible = segments > 0;
    },
  };
}

const DASH_M = 2;
const DASH_GAP_M = 1.5;
const MAX_DASHES = 160;

export interface TargetLine {
  place(from: GroundPoint, to: GroundPoint): void;
  show(visible: boolean): void;
}

/** One mesh of dash quads, so it reads apart from the solid roll line at one draw call. */
export function buildTargetLine(scene: Object3D): TargetLine {
  const index: number[] = [];
  for (let a = 0; a < 4 * MAX_DASHES; a += 4) index.push(a, a + 2, a + 1, a + 1, a + 2, a + 3);
  const geometry = new BufferGeometry();
  const position = new BufferAttribute(new Float32Array(MAX_DASHES * 12), 3);
  geometry.setAttribute('position', position);
  geometry.setAttribute('normal', upNormals(MAX_DASHES * 4));
  geometry.setIndex(index);
  geometry.setDrawRange(0, 0);
  const ends = [
    { x: 0, z: 0 },
    { x: 0, z: 0 },
  ];
  const mesh = new Mesh(geometry, groundMarkMaterial('#ffffff', 0.9));
  mesh.name = 'target-line';
  mesh.visible = false;
  scene.add(mesh);
  return {
    place(from, to) {
      const length = Math.hypot(to.x - from.x, to.z - from.z);
      const alongTheLine = (end: GroundPoint, metres: number) => {
        const k = length > 0 ? metres / length : 0;
        end.x = from.x + (to.x - from.x) * k;
        end.z = from.z + (to.z - from.z) * k;
      };
      const dashes = Math.min(MAX_DASHES, Math.max(1, Math.round((length + DASH_GAP_M) / (DASH_M + DASH_GAP_M))));
      const scale = length / (dashes * DASH_M + (dashes - 1) * DASH_GAP_M);
      for (let d = 0; d < dashes; d++) {
        const start = d * (DASH_M + DASH_GAP_M) * scale;
        const stop = start + DASH_M * scale;
        alongTheLine(ends[0], start);
        alongTheLine(ends[1], stop);
        writeGroundStrip(position.array as Float32Array, ends, d * 12);
      }
      geometry.setDrawRange(0, dashes * 6);
      position.needsUpdate = true;
      geometry.computeBoundingSphere();
    },
    show(visible) {
      mesh.visible = visible;
    },
  };
}

export interface FlightMarks {
  readonly tracers: readonly TracerSlot[];
  readonly dots: LandingDots;
  readonly rollLine: RollLine;
  readonly targetLine: TargetLine;
  readonly targetSign: Sign;
  readonly landingSign: Sign;
  setBall(position: Vec3 | null, cameraDistance: number): void;
  setBallShadow(position: Vec3 | null, groundY: number): void;
  setImpact(position: Vec3 | null, progress: number): void;
  setLandingMark(position: Vec3 | null): void;
}

export function buildFlightMarks(scene: Object3D, tracerCount: number): FlightMarks {
  const empty = buildTracerGeometry([]);
  const tracers = Array.from({ length: tracerCount }, () => createTracerSlot(scene, empty));
  const glowTexture = radialTexture([
    [0, 'rgba(255,255,255,1)'],
    [0.25, 'rgba(255,220,170,0.6)'],
    [1, 'rgba(255,140,60,0)'],
  ]);
  const shadowTexture = radialTexture([
    [0, 'rgba(255,255,255,1)'],
    [1, 'rgba(255,255,255,0)'],
  ]);
  const ball = new Mesh(new SphereGeometry(1, 14, 10), new MeshBasicMaterial({ color: '#ffffff', fog: false }));
  const ballGlow = new Sprite(
    new SpriteMaterial({
      map: glowTexture,
      transparent: true,
      depthWrite: false,
      blending: AdditiveBlending,
      fog: false,
    })
  );
  ballGlow.renderOrder = 7;
  const shadowMaterial = new MeshBasicMaterial({
    map: shadowTexture,
    color: '#000000',
    transparent: true,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: -3,
    polygonOffsetUnits: -8,
  });
  const ballShadow = new Mesh(new PlaneGeometry(1, 1).rotateX(-Math.PI / 2), shadowMaterial);
  const impactMaterial = new MeshBasicMaterial({
    color: '#ffffff',
    transparent: true,
    blending: AdditiveBlending,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: -4,
    polygonOffsetUnits: -8,
  });
  const impact = new Mesh(new RingGeometry(0.82, 1.0, 48).rotateX(-Math.PI / 2), impactMaterial);
  const landingMark = new Mesh(
    new RingGeometry(0.28, 0.46, 32).rotateX(-Math.PI / 2),
    groundMarkMaterial('#ffffff', 0.9)
  );
  const landingSign = createSign(300, 132);
  landingSign.sprite.center.set(0.5, 0);
  const targetSign = createSign(TARGET_LABEL_CANVAS.width, TARGET_LABEL_CANVAS.height);
  targetSign.sprite.center.set(0.5, 1);
  targetSign.sprite.material.depthTest = false;
  targetSign.sprite.renderOrder = 8;
  for (const mark of [ball, ballGlow, ballShadow, impact, landingMark, landingSign.sprite, targetSign.sprite]) {
    mark.visible = false;
    scene.add(mark);
  }

  return {
    tracers,
    dots: createLandingDots(scene),
    rollLine: buildRollLine(scene),
    targetLine: buildTargetLine(scene),
    targetSign,
    landingSign,
    setBall(position, cameraDistance) {
      ball.visible = ballGlow.visible = position !== null;
      if (!position) return;
      ball.position.set(position.x, position.y, position.z);
      ball.scale.setScalar(Math.max(0.03, cameraDistance * 0.0021));
      ballGlow.position.copy(ball.position);
      ballGlow.scale.setScalar(Math.max(0.4, cameraDistance * 0.024));
    },
    setBallShadow(position, groundY) {
      ballShadow.visible = position !== null;
      if (!position) return;
      const height = position.y - groundY;
      ballShadow.position.set(position.x, groundY + MARK_LIFT_M, position.z);
      ballShadow.scale.setScalar(0.8 + height * 0.09);
      shadowMaterial.opacity = 0.5 * (1 - Math.min(1, height / 45));
    },
    setImpact(position, progress) {
      impact.visible = position !== null;
      if (!position) return;
      impact.position.set(position.x, position.y + MARK_LIFT_M, position.z);
      impact.scale.setScalar(0.5 + progress * 7);
      impactMaterial.opacity = 1 - progress;
    },
    setLandingMark(position) {
      landingMark.visible = position !== null;
      if (position) landingMark.position.set(position.x, position.y + MARK_LIFT_M, position.z);
    },
  };
}
