/** Px of the range, at its top and its bottom edge, that the app's chrome lies over. */
export interface RangeInsets {
  readonly top: number;
  readonly bottom: number;
}

export const NO_INSETS: RangeInsets = Object.freeze({ top: 0, bottom: 0 });
