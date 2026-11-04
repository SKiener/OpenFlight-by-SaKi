/** `sideM` is positive to the right of the target line, negative to the left. */
export interface Green {
  readonly distanceM: number;
  readonly sideM: number;
  readonly flag: string;
}

const green = (distanceM: number, sideM: number, flag: string): Green => Object.freeze({ distanceM, sideM, flag });

export const GREENS: readonly Green[] = Object.freeze([
  green(50, -9, '#ff4b3e'),
  green(75, 8, '#ffcf33'),
  green(100, -5, '#3da5ff'),
  green(125, 9, '#f4f4f4'),
  green(150, -8, '#ff8a2a'),
  green(175, 5, '#b56cff'),
  green(200, -3, '#2fe0c8'),
  green(230, 10, '#ff5fa8'),
]);
