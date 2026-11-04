import { describe, expect, it } from 'vitest';
import { isKioskHost } from './kioskHost';

describe('isKioskHost', () => {
  it('is true for the machine itself, with or without a port', () => {
    for (const host of [
      'localhost',
      'localhost:5173',
      'LocalHost:5173',
      '127.0.0.1',
      '127.0.0.1:8080',
      '[::1]',
      '[::1]:8080',
    ]) {
      expect(isKioskHost(host), host).toBe(true);
    }
  });

  it('is false for every host a phone or another browser comes over the network', () => {
    const hosts = [
      'openflight.local',
      'openflight.local:8080',
      '192.168.1.20',
      '192.168.1.20:8080',
      'device.example.ts.net',
      '',
    ];
    for (const host of hosts) expect(isKioskHost(host), host).toBe(false);
  });

  it('does not take a name that only starts or ends like a local one', () => {
    for (const host of ['localhost.example.com', 'notlocalhost', '127.0.0.1.nip.io', '127.0.0.2', '127.0.0.10:8080']) {
      expect(isKioskHost(host), host).toBe(false);
    }
  });
});
