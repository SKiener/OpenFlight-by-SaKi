import {
  BufferGeometry,
  Color,
  Float32BufferAttribute,
  ShaderMaterial,
  UniformsLib,
  UniformsUtils,
  type IUniform,
  type ShaderMaterialParameters,
} from 'three';

export const FOG_COLOR = '#c8d9e2';

export const clamp01 = (x: number): number => Math.min(1, Math.max(0, x));
export const color = (hex: string): Color => new Color(hex);

export type Spot = [x: number, z: number];

/** The mown fairway: the ground shader draws it from these numbers and the vegetation keeps clear of them. */
export const FAIRWAY = Object.freeze({
  halfM: 32,
  widenM: 43,
  fullWidthAtM: 260,
  wobbleM: 8,
  blendM: 2,
  startM: -6,
  endM: 306,
});

export const fairwayHalfWidth = (downM: number): number =>
  FAIRWAY.halfM + FAIRWAY.widenM * clamp01(downM / FAIRWAY.fullWidthAtM);

export const fairwayEdgeM = (downM: number): number => fairwayHalfWidth(downM) + FAIRWAY.wobbleM + FAIRWAY.blendM;

export interface Part {
  geometry: BufferGeometry;
  tint: Color;
  shade?: (tint: Color, y: number) => Color;
}

export function mergeParts(parts: Part[]): BufferGeometry {
  const position: number[] = [];
  const normal: number[] = [];
  const colours: number[] = [];
  for (const { geometry, tint, shade } of parts) {
    const flat = geometry.index ? geometry.toNonIndexed() : geometry;
    flat.computeVertexNormals();
    const p = flat.getAttribute('position');
    const n = flat.getAttribute('normal');
    for (let i = 0; i < p.count; i++) {
      position.push(p.getX(i), p.getY(i), p.getZ(i));
      normal.push(n.getX(i), n.getY(i), n.getZ(i));
      const c = shade ? shade(tint, p.getY(i)) : tint;
      colours.push(c.r, c.g, c.b);
    }
  }
  const merged = new BufferGeometry();
  merged.setAttribute('position', new Float32BufferAttribute(position, 3));
  merged.setAttribute('normal', new Float32BufferAttribute(normal, 3));
  merged.setAttribute('color', new Float32BufferAttribute(colours, 3));
  return merged;
}

interface FogShaderParameters extends Omit<ShaderMaterialParameters, 'uniforms' | 'vertexShader' | 'fragmentShader'> {
  uniforms?: Record<string, IUniform>;
  vertex: string;
  fragment: string;
}

export function fogShader({ uniforms = {}, vertex, fragment, ...parameters }: FogShaderParameters): ShaderMaterial {
  return new ShaderMaterial({
    uniforms: { ...UniformsUtils.clone(UniformsLib.fog), ...uniforms },
    vertexShader: `#include <common>\n#include <fog_pars_vertex>\n${vertex}`,
    fragmentShader: `#include <common>\n#include <fog_pars_fragment>\n${fragment}`,
    fog: true,
    ...parameters,
  });
}
