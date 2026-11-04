import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DebugPanel, TriggerRow } from './DebugPanel';
import { setActiveLocale, t } from '../i18n';
import type { TriggerDiagnostic, TriggerStatus } from '../types/shot';

const status: TriggerStatus = { mode: 'rolling-buffer', trigger_type: 'sound', radar_connected: true,
  radar_port: '/dev/ttyAMA0', triggers_total: 4, triggers_accepted: 3, triggers_rejected: 1 };
const props = {
  enabled: false, readings: [], shotLogs: [], radarConfig: { min_speed: 10, max_speed: 220, min_magnitude: 0, transmit_power: 3 },
  mockMode: false, onToggle: vi.fn(), onUpdateConfig: vi.fn(), triggerDiagnostics: [], triggerStatus: status,
  connected: true, demoActive: false, onReinitializeRadar: vi.fn(),
};
afterEach(() => setActiveLocale('en'));

describe('manual OPS recovery', () => {
  it('has a German button and does not claim a successful test capture', () => {
    setActiveLocale('de');
    const html = renderToString(<DebugPanel {...props} />);
    expect(html).toContain('OPS-Radar wiederherstellen');
    expect(html).toContain('Währenddessen nicht schlagen');
    expect(t('debug.recovery.commands_sent')).toContain('Klatschen');
    expect(t('debug.recovery.commands_sent')).toContain('noch nicht bestätigt');
  });
  it.each(['speed', null])('does not offer recovery for unsupported trigger %s', (trigger_type) => {
    const html = renderToString(<DebugPanel {...props} triggerStatus={{ ...status, trigger_type }} />);
    expect(html).not.toContain('Restore OPS radar');
  });
  it('does not offer it in mock mode and disables it offline or in demo', () => {
    expect(renderToString(<DebugPanel {...props} mockMode />)).not.toContain('Restore OPS radar');
    for (const state of [{ connected: false }, { demoActive: true }]) {
      const html = renderToString(<DebugPanel {...props} {...state} />);
      expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Restore OPS radar<\/button>/);
    }
  });
});

describe('trigger rejection reasons', () => {
  const noBallTrack: TriggerDiagnostic = {
    timestamp: '2026-10-25T10:00:00Z',
    trigger_type: 'sound',
    accepted: false,
    reason: 'no_ball_track',
    response_bytes: 32768,
    total_readings: 3,
    outbound_readings: 3,
    inbound_readings: 0,
    peak_outbound_mph: 33.5,
    peak_inbound_mph: 0,
    all_outbound_speeds: [33.5, 17.8, 15.5],
    all_inbound_speeds: [],
    peak_outbound_magnitude: 0,
    peak_inbound_magnitude: 0,
    latency_ms: 12,
  };
  it.each([
    ['en', 'No ball detected after impact'],
    ['de', 'Kein Ball nach dem Treffmoment erkannt'],
  ] as const)('shows no_ball_track translated in %s', (locale, text) => {
    setActiveLocale(locale);
    const html = renderToString(<DebugPanel {...props} triggerDiagnostics={[noBallTrack]} />);
    expect(html).toContain(text);
    expect(html).not.toContain('no_ball_track');
  });
});

describe('TI status in trigger diagnostics', () => {
  const base: TriggerDiagnostic = {
    timestamp: '2026-10-25T10:00:00Z',
    trigger_type: 'sound',
    accepted: true,
    reason: 'accepted',
    response_bytes: 32768,
    total_readings: 3,
    outbound_readings: 3,
    inbound_readings: 0,
    peak_outbound_mph: 131.8,
    peak_inbound_mph: 0,
    all_outbound_speeds: [131.8],
    all_inbound_speeds: [],
    peak_outbound_magnitude: 0,
    peak_inbound_magnitude: 0,
    latency_ms: 12,
  };
  // Status tab (last-trigger card) plus the History tab row, which needs a
  // click to open and is therefore rendered on its own.
  const render = (iwr6843: TriggerDiagnostic['iwr6843']) => {
    const diag = { ...base, iwr6843 };
    return {
      card: renderToString(<DebugPanel {...props} enabled triggerDiagnostics={[diag]} />),
      row: renderToString(<TriggerRow diag={diag} />),
    };
  };

  it.each([
    ['en', 'TI launch angle implausible for club and speed'],
    ['de', 'TI-Abflugwinkel passt nicht zu Schläger und Tempo'],
  ] as const)('shows the angle and the reason of a rejected TI angle in %s', (locale, text) => {
    setActiveLocale(locale);
    const { card, row } = render({ state: 'rejected', reason: 'ti_launch_implausible', angle_deg: -0.47 });
    for (const html of [card, row]) {
      expect(html).toContain('-0.5°');
      expect(html).toContain(text);
      expect(html).not.toContain('ti_launch_implausible');
    }
  });

  it('shows only the angle for an accepted TI angle', () => {
    const { card, row } = render({ state: 'accepted', reason: 'accepted', angle_deg: 10.5 });
    expect(card).toMatch(/accepted<\/strong> at 10\.5°<\/div>/);
    expect(row).toMatch(/TI <!-- -->ACCEPTED<!-- --> 10\.5°<\/span>/);
  });

  it('keeps showing the reason of a rejection without an angle', () => {
    const { card, row } = render({ state: 'rejected', reason: 'capture_timebase_mismatch' });
    expect(card).toContain('— Inconsistent radar capture timing');
    expect(row).toContain(': Inconsistent radar capture timing');
  });
});

describe('clock offset line', () => {
  const RANGE_OFFSET_S = (5 * 24 + 17) * 3600 + 29 * 60;
  const withOffsets = (kiosk: number | null, phone: number | null) => (
    <DebugPanel {...props} triggerStatus={{ ...status, clock_offset_s: { kiosk, phone } }} />
  );

  it('shows the phone offset in German, English and Spanish', () => {
    setActiveLocale('de');
    expect(renderToString(withOffsets(0.4, RANGE_OFFSET_S))).toContain(
      'Uhrzeit-Abweichung zum Handy: +5 T 17 h 29 min',
    );
    setActiveLocale('en');
    expect(renderToString(withOffsets(0.4, RANGE_OFFSET_S))).toContain(
      'Clock difference to phone: +5 d 17 h 29 min',
    );
    setActiveLocale('es');
    expect(renderToString(withOffsets(null, -RANGE_OFFSET_S))).toContain(
      'Diferencia de hora con el móvil: -5 d 17 h 29 min',
    );
  });

  it('hides offsets up to two minutes and before any client reported', () => {
    for (const html of [
      renderToString(withOffsets(0.4, 119)),
      renderToString(withOffsets(null, null)),
      renderToString(<DebugPanel {...props} />),
    ]) {
      expect(html).not.toContain('Clock difference');
    }
  });

  it('shows a wrong kiosk clock too', () => {
    expect(renderToString(withOffsets(-600, 1))).toContain('Clock difference to kiosk: -10 min');
  });
});
