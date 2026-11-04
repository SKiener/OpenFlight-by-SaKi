import { describe, expect, it } from 'vitest';
import { fakeNative } from '../test-utils/nativeBridge';
import { getOpenflightNative } from './openflightNative';

describe('getOpenflightNative', () => {
  it('finds the companion bridge', () => {
    const native = fakeNative();
    expect(getOpenflightNative({ openflightNative: native })).toBe(native);
  });

  it('is null in Safari, on the kiosk and without a window', () => {
    expect(getOpenflightNative({})).toBeNull();
    expect(getOpenflightNative(undefined)).toBeNull();
  });

  it('ignores another protocol version or an incomplete object', () => {
    expect(getOpenflightNative({ openflightNative: { ...fakeNative(), version: 2 } })).toBeNull();
    expect(getOpenflightNative({ openflightNative: { ...fakeNative(), getSensors: undefined } })).toBeNull();
    expect(getOpenflightNative({ openflightNative: { ...fakeNative(), fetchWeather: 'no' } })).toBeNull();
    expect(getOpenflightNative({ openflightNative: { ...fakeNative(), capabilities: null } })).toBeNull();
    expect(getOpenflightNative({ openflightNative: 'native' })).toBeNull();
  });
});
