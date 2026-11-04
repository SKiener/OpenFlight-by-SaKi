import { Vector3, type PerspectiveCamera } from 'three';
import type { GreenSign } from './greens';
import { LABEL_MARGIN_PX, SIGN_PRIORITY, visibleLabels, type LabelBox } from './labelLayout';
import type { Sign } from './textures';

export const LANDING_SIGN_SIZE = 0.17;
export const TARGET_SIGN_SIZE = 0.12;

interface SignSlot {
  sign: Sign;
  size: number;
  box: LabelBox;
}

export interface SignLayout {
  /** `reserved`: canvas areas no sign may sit on, such as the strips under the app's chrome. */
  layout(
    camera: PerspectiveCamera,
    width: number,
    height: number,
    landingShown: boolean,
    targetShown: boolean,
    reserved: readonly LabelBox['rect'][]
  ): void;
}

function labelBox(id: string, priority: number): LabelBox {
  return { id, priority, distance: 0, behind: false, rect: [0, 0, 0, 0] };
}

export function createSignLayout(greenSigns: readonly GreenSign[], landingSign: Sign, targetSign: Sign): SignLayout {
  const projected = new Vector3();
  const seen = new Vector3();
  const landingSlot: SignSlot = {
    sign: landingSign,
    size: LANDING_SIGN_SIZE,
    box: labelBox('landing', SIGN_PRIORITY.landing),
  };
  const targetSlot: SignSlot = {
    sign: targetSign,
    size: TARGET_SIGN_SIZE,
    box: labelBox('target', SIGN_PRIORITY.target),
  };
  const greenSlots = greenSigns.map((green) => ({
    green,
    sign: green.sign,
    size: green.size,
    box: labelBox(green.id, green.priority),
  }));
  const boxes: LabelBox[] = [];

  function placeSign(
    { sign, size, box }: SignSlot,
    camera: PerspectiveCamera,
    pxPerUnit: number,
    width: number,
    height: number
  ): void {
    const { sprite, aspect } = sign;
    seen.setFromMatrixPosition(sprite.matrixWorld);
    const distance = camera.position.distanceTo(seen);
    sprite.scale.set(distance * size, distance * size * aspect, 1);
    projected.copy(seen).project(camera);
    const w = size * pxPerUnit;
    const x = ((projected.x + 1) / 2) * width;
    const y = ((1 - projected.y) / 2) * height;
    box.distance = distance;
    box.behind = projected.z > 1;
    box.rect[0] = x - sprite.center.x * w;
    box.rect[1] = y - (1 - sprite.center.y) * w * aspect;
    box.rect[2] = x + (1 - sprite.center.x) * w;
    box.rect[3] = y + sprite.center.y * w * aspect;
  }

  function placeIf(on: boolean, slot: SignSlot, camera: PerspectiveCamera, pxPerUnit: number, w: number, h: number) {
    if (!on) return;
    placeSign(slot, camera, pxPerUnit, w, h);
    boxes.push(slot.box);
  }

  return {
    layout(camera, width, height, landingShown, targetShown, reserved) {
      const pxPerUnit = height / 2 / Math.tan((camera.fov * Math.PI) / 360);
      boxes.length = 0;
      placeIf(landingShown, landingSlot, camera, pxPerUnit, width, height);
      placeIf(targetShown, targetSlot, camera, pxPerUnit, width, height);
      for (const slot of greenSlots) {
        slot.size = slot.green.size;
        slot.box.priority = slot.green.priority;
        placeIf(true, slot, camera, pxPerUnit, width, height);
      }
      const shown = visibleLabels(boxes, LABEL_MARGIN_PX, reserved);
      landingSlot.sign.sprite.visible = landingShown && shown.has(landingSlot.box.id);
      targetSlot.sign.sprite.visible = targetShown && shown.has(targetSlot.box.id);
      for (const { sign, box } of greenSlots) sign.sprite.visible = shown.has(box.id);
    },
  };
}
