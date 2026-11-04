import { renderToString } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActiveLocale } from '../i18n';
import { ConfirmDeleteButton } from './ConfirmDeleteButton';
import { CONFIRM_MS, confirmDeleteText, createConfirmDelete, type ConfirmPhase } from './confirmDelete';

const settle = async (): Promise<void> => {
  await Promise.resolve();
  await Promise.resolve();
};

function rig() {
  const phases: ConfirmPhase[] = [];
  const control = createConfirmDelete((phase) => phases.push(phase));
  return { control, phases, phase: () => phases.at(-1) ?? 'idle' };
}

beforeEach(() => {
  vi.useFakeTimers();
  setActiveLocale('de');
});

afterEach(() => {
  vi.useRealTimers();
  setActiveLocale('en');
});

describe('ConfirmDeleteButton', () => {
  it('needs a second tap within 4 s', async () => {
    const onConfirm = vi.fn(async () => {});
    const { control, phase } = rig();

    control.tap(onConfirm);
    expect(phase()).toBe('confirming');
    expect(confirmDeleteText(phase(), 'Letzten Schlag löschen')).toBe('Wirklich löschen?');
    expect(onConfirm).not.toHaveBeenCalled();

    vi.advanceTimersByTime(CONFIRM_MS - 1);
    control.tap(onConfirm);
    expect(onConfirm).toHaveBeenCalledOnce();
    await settle();
    expect(phase()).toBe('idle');

    control.tap(onConfirm);
    vi.advanceTimersByTime(CONFIRM_MS);
    expect(phase(), 'no second tap: back to normal').toBe('idle');
    expect(confirmDeleteText(phase(), 'Letzten Schlag löschen')).toBe('Letzten Schlag löschen');
    control.tap(onConfirm);
    expect(phase()).toBe('confirming');
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(CONFIRM_MS).toBe(4000);
  });

  it('deletes once on a double confirm', async () => {
    let finish!: () => void;
    const onConfirm = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    const { control, phase } = rig();

    control.tap(onConfirm);
    control.tap(onConfirm);
    control.tap(onConfirm);
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(phase()).toBe('busy');
    vi.advanceTimersByTime(CONFIRM_MS * 3);
    expect(phase(), 'busy, so disabled, until the delete settles').toBe('busy');

    finish();
    await settle();
    expect(phase()).toBe('idle');
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('shows the failure for 4 s', async () => {
    const onConfirm = vi.fn(async () => {
      throw new Error('404');
    });
    const { control, phase } = rig();

    control.tap(onConfirm);
    control.tap(onConfirm);
    await settle();
    expect(phase()).toBe('failed');
    expect(confirmDeleteText(phase(), 'Löschen')).toBe('Löschen fehlgeschlagen');
    vi.advanceTimersByTime(CONFIRM_MS - 1);
    expect(phase()).toBe('failed');
    vi.advanceTimersByTime(1);
    expect(phase()).toBe('idle');

    const throwing = vi.fn(() => {
      throw new Error('offline');
    });
    control.tap(throwing);
    control.tap(throwing);
    await settle();
    expect(phase(), 'a confirm that throws at once fails the same way').toBe('failed');
  });

  it('stops its timers and drops a late answer once disposed', async () => {
    let fail!: (error: Error) => void;
    const { control, phases } = rig();
    const onConfirm = () => new Promise<void>((_, reject) => (fail = reject));
    control.tap(onConfirm);
    control.tap(onConfirm);
    const before = phases.length;

    control.dispose();
    fail(new Error('late'));
    await settle();
    vi.advanceTimersByTime(CONFIRM_MS * 2);
    expect(phases).toHaveLength(before);
  });

  it('renders the label, the disabled state and the class names', () => {
    const html = renderToString(
      <ConfirmDeleteButton label="Letzten Schlag löschen" onConfirm={() => {}} disabled className="range-delete" />
    );

    expect(html).toContain('>Letzten Schlag löschen<');
    expect(html).toContain('disabled');
    expect(html).toMatch(/class="confirm-delete range-delete"/);
    expect(html).toContain('type="button"');
  });
});
