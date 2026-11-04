import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToString } from 'react-dom/server';
import { catalogs, setActiveLocale } from '../../i18n';
import { socketService } from '../../services/socketService';
import { activeBallType, isBallType } from '../../utils/ballType';
import { BallTypeSectionView, type BallTypeSectionViewProps } from './BallTypeSection';

function renderView(overrides: Partial<BallTypeSectionViewProps> = {}) {
  return renderToString(
    <BallTypeSectionView ballType="standard" disabled={false} failed={false} onChange={() => {}} {...overrides} />
  ).replace(/<!-- -->/g, '');
}

/** The `<button …>` opening tag whose label is `label`. */
function button(html: string, label: string): string {
  return new RegExp(`<button[^>]*>${label}</button>`).exec(html)?.[0] ?? '';
}

/** The `<fieldset …>` opening tag. */
function fieldset(html: string): string {
  return /<fieldset[^>]*>/.exec(html)?.[0] ?? '';
}

describe('BallTypeSectionView', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows the title, both balls and the hint in German', () => {
    setActiveLocale('de');
    const html = renderView();
    expect(html).toContain('aria-label="Ballart"');
    expect(html).toContain('menu-sheet__section-title">Ballart<');
    expect(button(html, 'Normaler Ball')).not.toBe('');
    expect(button(html, 'Rangeball')).not.toBe('');
    expect(html).toContain(
      'Bestimmt den typischen Spin, solange kein Spin gemessen wird. Rangeball: Spin aus deinen Toptracer-Sitzungen.'
    );
  });

  it('shows the English labels and hint', () => {
    const html = renderView();
    expect(button(html, 'Standard ball')).not.toBe('');
    expect(button(html, 'Range ball')).not.toBe('');
    expect(html).toContain('Sets the typical spin while no spin is measured. Range ball: spin from your Toptracer sessions.');
  });

  it('uses the shared segmented control', () => {
    expect(renderView()).toContain('class="segmented-control" role="group" aria-label="Ball type"');
  });

  it.each([
    ['standard', 'Standard ball', 'Range ball'],
    ['range', 'Range ball', 'Standard ball'],
  ] as const)('marks the effective %s ball as chosen', (ballType, chosen, other) => {
    const html = renderView({ ballType });
    expect(button(html, chosen)).toContain('aria-pressed="true"');
    expect(button(html, chosen)).toContain('segmented-control__button--active');
    expect(button(html, other)).toContain('aria-pressed="false"');
  });

  it('disables the choice while saving, offline or without a profile', () => {
    expect(fieldset(renderView({ disabled: true }))).toContain('disabled');
    expect(fieldset(renderView({ disabled: false }))).not.toContain('disabled');
  });

  it('says when saving failed, and only then', () => {
    expect(renderView({ failed: true })).toContain('role="alert">Ball type could not be saved<');
    expect(renderView()).not.toContain('role="alert"');
    setActiveLocale('de');
    expect(renderView({ failed: true })).toContain('role="alert">Ballart konnte nicht gespeichert werden<');
  });
});

describe('ballType i18n', () => {
  it('has every ballType key in every locale', () => {
    const keys = ['ballType.title', 'ballType.standard', 'ballType.range', 'ballType.hint', 'ballType.error'];
    for (const [id, messages] of Object.entries(catalogs)) {
      for (const key of keys) expect((messages as Record<string, string>)[key], `${id} ${key}`).toBeTruthy();
    }
    expect(catalogs.es['ballType.title']).toBe('Tipo de bola');
  });
});

describe('activeBallType', () => {
  const roster = (profile: Record<string, unknown>) => ({
    profiles: [
      { id: 'other', name: 'Other', created_at: '', settings: { ball_type: 'range' }, ball_type: 'range' as const },
      { id: 'me', name: 'Me', created_at: '', settings: {}, ...profile },
    ],
    activeProfileId: 'me',
  });

  it("shows the server's effective value, also the launcher default of an unset profile", () => {
    expect(activeBallType(roster({ ball_type: 'range' }))).toBe('range');
    expect(activeBallType(roster({ ball_type: 'standard', settings: {} }))).toBe('standard');
  });

  it("reads the active profile's value, not another's", () => {
    expect(activeBallType(roster({}))).toBe('standard');
  });

  it('falls back to the stored setting, then standard', () => {
    expect(activeBallType(roster({ settings: { ball_type: 'range' } }))).toBe('range');
    expect(activeBallType(roster({ ball_type: 'pro-v1', settings: { ball_type: 'x' } }))).toBe('standard');
    expect(activeBallType({ profiles: [], activeProfileId: '' })).toBe('standard');
  });

  it('knows only the two ball types', () => {
    expect(['standard', 'range'].every(isBallType)).toBe(true);
    expect(['tour', 'range-ball', '', null, 1].some(isBallType)).toBe(false);
  });
});

describe('socketService.setProfileBallType', () => {
  afterEach(() => vi.restoreAllMocks());

  it('emits the ball type for the profile', async () => {
    const emitWithAck = vi.fn(async () => ({ ok: true }));
    (socketService as unknown as { socket: unknown }).socket = { connected: true, timeout: () => ({ emitWithAck }) };
    try {
      await socketService.setProfileBallType('me', 'range');
    } finally {
      (socketService as unknown as { socket: unknown }).socket = null;
    }
    expect(emitWithAck).toHaveBeenCalledWith('set_profile_ball_type', { profile_id: 'me', ball_type: 'range' });
  });

  it('rejects when the server refused', async () => {
    const emitWithAck = vi.fn(async () => ({ ok: false }));
    (socketService as unknown as { socket: unknown }).socket = { connected: true, timeout: () => ({ emitWithAck }) };
    try {
      await expect(socketService.setProfileBallType('me', 'standard')).rejects.toThrow('Ball type could not be saved');
    } finally {
      (socketService as unknown as { socket: unknown }).socket = null;
    }
  });

  it('rejects while disconnected', async () => {
    (socketService as unknown as { socket: unknown }).socket = null;
    await expect(socketService.setProfileBallType('me', 'range')).rejects.toThrow('Disconnected');
  });
});
