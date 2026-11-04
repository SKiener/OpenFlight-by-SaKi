import {
  BackSide,
  BoxGeometry,
  BufferGeometry,
  DirectionalLight,
  Float32BufferAttribute,
  Fog,
  HemisphereLight,
  InstancedMesh,
  Matrix4,
  Mesh,
  MeshLambertMaterial,
  OctahedronGeometry,
  PlaneGeometry,
  ShaderMaterial,
  SphereGeometry,
  Vector3,
  type Scene,
  type Texture,
} from 'three';
import { FAIRWAY, FOG_COLOR, clamp01, color, fairwayHalfWidth, fogShader, mergeParts, type Spot } from './parts';
import { between, seededRandom, type Random } from './textures';
import { plantVegetation } from './vegetation';

const SUN = new Vector3(-0.45, 0.78, 0.43).normalize();
const DETAIL_SEED = 20261008;
const matrix = new Matrix4();
const glslFloat = (value: number): string => value.toFixed(1);

function buildSky(noise: Texture): Mesh {
  const sky = new Mesh(
    new SphereGeometry(10, 32, 16),
    new ShaderMaterial({
      side: BackSide,
      depthWrite: false,
      uniforms: {
        uTop: { value: color('#2f6db5') },
        uMid: { value: color('#7fb3e3') },
        uHorizon: { value: color('#dbe8ee') },
        uNoise: { value: noise },
      },
      vertexShader: `varying vec3 vDir;
        void main() {
          vDir = position;
          vec4 p = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          gl_Position = p.xyww;
        }`,
      fragmentShader: `uniform vec3 uTop; uniform vec3 uMid; uniform vec3 uHorizon; uniform sampler2D uNoise;
        varying vec3 vDir;
        void main() {
          vec3 d = normalize(vDir);
          float h = d.y;
          vec3 c = mix(uHorizon, uMid, smoothstep(0.0, 0.2, h));
          c = mix(c, uTop, smoothstep(0.2, 0.75, h));
          vec2 veilUv = d.xz / (h + 0.3) * vec2(0.09, 0.3) + vec2(0.62, 0.17);
          float veil = smoothstep(0.42, 0.8, texture2D(uNoise, veilUv).g) * smoothstep(0.02, 0.2, h);
          c = mix(c, vec3(1.0), veil * 0.3);
          vec2 uv = d.xz / (h + 0.22) * 0.32 + vec2(0.13, 0.41);
          float n = texture2D(uNoise, uv).r * 0.7 + texture2D(uNoise, uv * 3.1).g * 0.3;
          float cloud = smoothstep(0.52, 0.74, n) * smoothstep(0.02, 0.28, h);
          c = mix(c, vec3(1.0), cloud * 0.7);
          c = mix(uHorizon, c, smoothstep(-0.03, 0.01, h));
          gl_FragColor = vec4(c, 1.0);
          #include <colorspace_fragment>
        }`,
    })
  );
  sky.renderOrder = -10;
  sky.frustumCulled = false;
  return sky;
}

function buildGround(noise: Texture): Mesh {
  const ground = new Mesh(
    new PlaneGeometry(3800, 3800).rotateX(-Math.PI / 2),
    fogShader({
      uniforms: {
        uNoise: { value: noise },
        uFairA: { value: color('#6cae4c') },
        uFairB: { value: color('#5b9a3f') },
        uRough: { value: color('#4f8236') },
        uRoughDark: { value: color('#3c672b') },
      },
      vertex: `varying vec3 vGround;
        void main() {
          vGround = position;
          vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
          gl_Position = projectionMatrix * mvPosition;
          #include <fog_vertex>
        }`,
      fragment: `uniform sampler2D uNoise; uniform vec3 uFairA; uniform vec3 uFairB; uniform vec3 uRough; uniform vec3 uRoughDark;
        varying vec3 vGround;
        void main() {
          vec2 p = vGround.xz;
          float down = -p.y;
          float n1 = texture2D(uNoise, p * 0.0035).r;
          float n2 = texture2D(uNoise, p * 0.045).g;
          float n3 = texture2D(uNoise, p * 0.55).b;
          float widen = clamp(down / ${glslFloat(FAIRWAY.fullWidthAtM)}, 0.0, 1.0);
          float halfW = ${glslFloat(FAIRWAY.halfM)} + ${glslFloat(FAIRWAY.widenM)} * widen + (n1 - 0.5) * ${glslFloat(2 * FAIRWAY.wobbleM)};
          float fair = 1.0 - smoothstep(halfW - ${glslFloat(FAIRWAY.blendM)}, halfW + ${glslFloat(FAIRWAY.blendM)}, abs(p.x));
          fair *= smoothstep(${glslFloat(FAIRWAY.startM)}, 2.0, down) * (1.0 - smoothstep(292.0, ${glslFloat(FAIRWAY.endM)}, down));
          float s = down / 9.0;
          float fw = fwidth(s);
          float tri = abs(fract(s) * 2.0 - 1.0);
          float stripe = smoothstep(0.5 - fw, 0.5 + fw, tri);
          stripe = mix(stripe, 0.5, smoothstep(0.12, 0.45, fw));
          vec3 c = mix(mix(uRoughDark, uRough, n2), mix(uFairA, uFairB, stripe), fair);
          float grain = 1.0 - smoothstep(0.03, 0.25, fwidth(p.x * 0.55));
          c *= 1.0 + (n3 - 0.5) * 0.28 * grain;
          c *= 0.9 + 0.2 * n1;
          c *= 1.0 + (n2 - 0.5) * 0.14 * fair;
          gl_FragColor = vec4(c, 1.0);
          #include <colorspace_fragment>
          #include <fog_fragment>
        }`,
    })
  );
  ground.renderOrder = -5;
  return ground;
}

function buildHills(rand: Random): Mesh {
  const position: number[] = [];
  const colours: number[] = [];
  const rings = [
    { r: 620, h: [40, 105], tint: color('#46703f') },
    { r: 980, h: [70, 190], tint: color('#5d806f') },
    { r: 1450, h: [120, 300], tint: color('#86a3a8') },
  ];
  for (const ring of rings) {
    const segments = 150;
    const phases = [between(rand, 0, 6), between(rand, 0, 6), between(rand, 0, 6)];
    const heightAt = (a: number) => {
      const k =
        0.45 +
        0.3 * Math.sin(3 * a + phases[0]) +
        0.17 * Math.sin(7 * a + phases[1]) +
        0.08 * Math.sin(17 * a + phases[2]);
      return ring.h[0] + (ring.h[1] - ring.h[0]) * clamp01(k);
    };
    for (let i = 0; i < segments; i++) {
      const a0 = (i / segments) * Math.PI * 2;
      const a1 = ((i + 1) / segments) * Math.PI * 2;
      const h0 = heightAt(a0);
      const h1 = heightAt(a1);
      const b0 = [Math.cos(a0) * ring.r, -2, Math.sin(a0) * ring.r];
      const b1 = [Math.cos(a1) * ring.r, -2, Math.sin(a1) * ring.r];
      const t0 = [Math.cos(a0) * (ring.r + h0 * 0.8), h0, Math.sin(a0) * (ring.r + h0 * 0.8)];
      const t1 = [Math.cos(a1) * (ring.r + h1 * 0.8), h1, Math.sin(a1) * (ring.r + h1 * 0.8)];
      position.push(...b0, ...t0, ...b1, ...b1, ...t0, ...t1);
      const lift = 1.0 + between(rand, -0.06, 0.06);
      for (let k = 0; k < 6; k++) colours.push(ring.tint.r * lift, ring.tint.g * lift, ring.tint.b * lift);
    }
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(position, 3));
  geometry.setAttribute('color', new Float32BufferAttribute(colours, 3));
  geometry.computeVertexNormals();
  return new Mesh(geometry, new MeshLambertMaterial({ vertexColors: true, flatShading: true }));
}

function buildPosts(): InstancedMesh {
  const post = mergeParts([
    { geometry: new BoxGeometry(0.3, 1.2, 0.3).translate(0, 0.6, 0), tint: color('#f2f2f2') },
    { geometry: new BoxGeometry(0.34, 0.4, 0.34).translate(0, 1.4, 0), tint: color('#e0483a') },
  ]);
  const spots: Spot[] = [];
  for (let down = 50; down <= 250; down += 50) {
    for (const side of [-1, 1]) spots.push([side * (fairwayHalfWidth(down) - 3), -down]);
  }
  const mesh = new InstancedMesh(post, new MeshLambertMaterial({ vertexColors: true }), spots.length);
  spots.forEach(([x, z], i) => mesh.setMatrixAt(i, matrix.makeTranslation(x, 0, z)));
  return mesh;
}

function buildMat(): Mesh {
  return new Mesh(
    mergeParts([
      { geometry: new BoxGeometry(3.6, 0.2, 2.6).translate(0, -0.06, 0.8), tint: color('#8c8f88') },
      { geometry: new BoxGeometry(1.5, 0.06, 1.5).translate(0, 0.06, 0.5), tint: color('#2b5d34') },
      { geometry: new BoxGeometry(0.4, 0.07, 1.5).translate(0.35, 0.065, 0.5), tint: color('#3a7445') },
    ]),
    new MeshLambertMaterial({ vertexColors: true })
  );
}

// Four times a real ball, so the balls still show as dots of a few pixels from the landing views.
const BALL_RADIUS_M = 0.09;

function buildRangeBalls(rand: Random, count = 420): InstancedMesh {
  const ball = mergeParts([{ geometry: new OctahedronGeometry(BALL_RADIUS_M, 0), tint: color('#eeeeea') }]);
  // Every face lit like the top one: a few pixels of white, not grey facets.
  const normal = ball.getAttribute('normal');
  for (let i = 0; i < normal.count; i++) normal.setXYZ(i, 0, 1, 0);
  const mesh = new InstancedMesh(ball, new MeshLambertMaterial({ vertexColors: true }), count);
  for (let i = 0; i < count; i++) {
    const down = 25 + 105 * (rand() + rand());
    const across = (rand() + rand() - 1) * fairwayHalfWidth(down) * 0.8;
    mesh.setMatrixAt(i, matrix.makeTranslation(across, BALL_RADIUS_M, -down));
  }
  return mesh;
}

export interface Environment {
  readonly sky: Mesh;
  setVegetationHalved(halved: boolean): void;
}

/** `rand` must be the probe's stream, read in the probe's order, or the approved range comes out different. */
export function buildEnvironment(scene: Scene, rand: Random, noise: Texture, vegetationHalved: boolean): Environment {
  scene.fog = new Fog(FOG_COLOR, 170, 2300);
  scene.add(new HemisphereLight(color('#d8ecff'), color('#4d6b3c'), 1.25));
  const sun = new DirectionalLight(color('#fff1d6'), 1.9);
  sun.position.copy(SUN).multiplyScalar(100);
  scene.add(sun);

  const sky = buildSky(noise);
  scene.add(sky, buildGround(noise), buildHills(rand));

  const detail = seededRandom(DETAIL_SEED);
  const vegetation = plantVegetation(scene, rand, detail);
  scene.add(buildPosts(), buildMat(), buildRangeBalls(detail));
  vegetation.setHalved(vegetationHalved);
  return { sky, setVegetationHalved: (halved) => vegetation.setHalved(halved) };
}
