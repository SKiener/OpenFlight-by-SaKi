import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToString } from 'react-dom/server';
import { setActiveLocale } from '../../i18n';
import { socketService } from '../../services/socketService';
import { activeDriverTeeHeightM } from '../../utils/driverTee';
import { DriverTeeSectionView, type DriverTeeSectionViewProps } from './DriverTeeSection';

function renderView(overrides: Partial<DriverTeeSectionViewProps> = {}) {
  return renderToString(
    <DriverTeeSectionView
      heightM={0.04}
      unitSystem="metric"
      disabled={false}
      failed={false}
      onChange={() => {}}
      {...overrides}
    />
  ).replace(/<!-- -->/g, '');
}

/** The `<button …>` opening tag with this aria-label. */
function button(html: string, label: string): string {
  return new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`).exec(html)?.[0] ?? '';
}

describe('DriverTeeSectionView', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows the title, 4,0 cm and the hint in German', () => {
    setActiveLocale('de');
    const html = renderView();
    expect(html).toContain('menu-sheet__section-title">Tee-Höhe Driver');
    expect(html).toContain('data-testid="driver-tee-value" aria-live="polite">4,0 cm<');
    expect(html).toContain('Ballunterkante über der Matte auf dem Gummitee. Gilt nur für den Driver.');
  });

  it('shows inches for the yard unit systems', () => {
    expect(renderView({ unitSystem: 'imperial' })).toContain('>1.6 in<');
    expect(renderView({ unitSystem: 'kmh-yards', heightM: 0.0508 })).toContain('>2.0 in<');
  });

  it('uses the kiosk-sized panel buttons for − and +', () => {
    const html = renderView();
    expect(button(html, 'Lower')).toContain('class="panel-action panel-action--secondary"');
    expect(button(html, 'Higher')).toContain('class="panel-action panel-action--secondary"');
  });

  it('disables − at 0 cm and + at 8 cm', () => {
    expect(button(renderView({ heightM: 0 }), 'Lower')).toContain('disabled');
    expect(button(renderView({ heightM: 0 }), 'Higher')).not.toContain('disabled');
    expect(button(renderView({ heightM: 0.08 }), 'Higher')).toContain('disabled');
    expect(button(renderView({ heightM: 0.08 }), 'Lower')).not.toContain('disabled');
  });

  it('disables both while saving or offline and says when saving failed', () => {
    const html = renderView({ disabled: true, failed: true });
    expect(button(html, 'Lower')).toContain('disabled');
    expect(button(html, 'Higher')).toContain('disabled');
    expect(html).toContain('role="alert">Tee height could not be saved<');
  });
});

describe('activeDriverTeeHeightM', () => {
  const roster = (settings: Record<string, unknown>) => ({
    profiles: [
      { id: 'other', name: 'Other', created_at: '', settings: { driver_tee_height_m: 0.07 } },
      { id: 'me', name: 'Me', created_at: '', settings },
    ],
    activeProfileId: 'me',
  });

  it('is 4 cm when the active profile stores none', () => {
    expect(activeDriverTeeHeightM(roster({ unit_system: 'metric' }))).toBe(0.04);
    expect(activeDriverTeeHeightM({ profiles: [], activeProfileId: '' })).toBe(0.04);
  });

  it("reads the active profile's value, not another's", () => {
    expect(activeDriverTeeHeightM(roster({ driver_tee_height_m: 0.055 }))).toBe(0.055);
  });
});

describe('socketService.setProfileDriverTee', () => {
  afterEach(() => vi.restoreAllMocks());

  it('emits the height in metres for the profile', async () => {
    const emitWithAck = vi.fn(async () => ({ ok: true }));
    const socket = { connected: true, timeout: () => ({ emitWithAck }) };
    (socketService as unknown as { socket: unknown }).socket = socket;
    try {
      await socketService.setProfileDriverTee('me', 0.045);
    } finally {
      (socketService as unknown as { socket: unknown }).socket = null;
    }
    expect(emitWithAck).toHaveBeenCalledWith('set_profile_driver_tee', {
      profile_id: 'me',
      driver_tee_height_m: 0.045,
    });
  });

  it('rejects when the server refused', async () => {
    const emitWithAck = vi.fn(async () => ({ ok: false }));
    (socketService as unknown as { socket: unknown }).socket = { connected: true, timeout: () => ({ emitWithAck }) };
    try {
      await expect(socketService.setProfileDriverTee('me', 0.2)).rejects.toThrow('Tee height could not be saved');
    } finally {
      (socketService as unknown as { socket: unknown }).socket = null;
    }
  });
});
