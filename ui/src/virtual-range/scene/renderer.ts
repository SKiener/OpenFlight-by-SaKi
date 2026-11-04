import {
  InstancedMesh,
  Mesh,
  SRGBColorSpace,
  ShaderMaterial,
  Sprite,
  Texture,
  WebGLRenderer,
  type BufferGeometry,
  type Material,
  type Scene,
} from 'three';
import { FOG_COLOR } from './parts';

export function createRenderer(antialias: boolean): WebGLRenderer {
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'display: block; width: 100%; height: 100%';
  const context = canvas.getContext('webgl2', {
    alpha: true,
    antialias,
    depth: true,
    stencil: false,
    powerPreference: 'high-performance',
  });
  if (!context) throw new Error('webgl-unavailable');
  try {
    const renderer = new WebGLRenderer({
      canvas,
      context,
      antialias,
      powerPreference: 'high-performance',
      stencil: false,
    });
    renderer.setPixelRatio(1);
    renderer.outputColorSpace = SRGBColorSpace;
    renderer.setClearColor(FOG_COLOR);
    return renderer;
  } catch {
    context.getExtension('WEBGL_lose_context')?.loseContext();
    throw new Error('webgl-unavailable');
  }
}

export function destroyRenderer(renderer: WebGLRenderer): void {
  renderer.dispose();
  if (!renderer.getContext().isContextLost()) renderer.forceContextLoss();
  renderer.domElement.remove();
}

export function disposeResources(scene: Scene): void {
  const geometries = new Set<BufferGeometry>();
  const materials = new Set<Material>();
  const textures = new Set<Texture>();
  scene.traverse((object) => {
    if (object instanceof InstancedMesh) object.dispose();
    if (object instanceof Mesh || object instanceof Sprite) {
      geometries.add(object.geometry);
      for (const material of [object.material].flat()) materials.add(material);
    }
  });
  for (const material of materials) {
    if ('map' in material && material.map instanceof Texture) textures.add(material.map);
    if (material instanceof ShaderMaterial) {
      for (const { value } of Object.values(material.uniforms)) if (value instanceof Texture) textures.add(value);
    }
  }
  for (const resource of [...geometries, ...materials, ...textures]) resource.dispose();
}
