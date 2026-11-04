/** `?rangeDemo=1`, the Pi's render measurement. Apart from the demo itself, so the app can ask without building it. */
export function isRangeDemo(search: string): boolean {
  return new URLSearchParams(search).get('rangeDemo') === '1';
}
