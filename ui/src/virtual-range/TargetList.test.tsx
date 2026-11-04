import { isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setActiveLocale } from '../i18n';
import { fakeStorage } from '../test-utils/fakeBrowser';
import type { UnitSystem } from '../utils/units';
import { TARGET_KEY } from './aim';
import { TRAINING_KEY } from './game/training';
import { TargetList, TargetRows } from './TargetList';
import { VirtualRangeView, type VirtualRangeViewProps } from './VirtualRangeView';
import { watchTargetList } from './watchTargetList';

const preference = vi.hoisted(() => ({ unitSystem: 'metric' as UnitSystem }));
vi.mock('../state/useUnitPreference', () => ({ useUnitPreference: () => preference }));

const text = (html: string): string => html.replace(/<!-- -->/g, '');

const rows = (selected: number | null, unitSystem: UnitSystem): string =>
  text(
    renderToString(
      <ul>
        <TargetRows selected={selected} unitSystem={unitSystem} onSelect={() => {}} onClose={() => {}} />
      </ul>
    )
  );

const rowTexts = (html: string): string[] =>
  [...html.matchAll(/<button[^>]*>(.*?)<\/button>/g)].map(([, inner]) => inner.replace(/<[^>]+>/g, ''));

const buttons = (node: ReactNode): ReactElement<{ onClick(): void }>[] => {
  if (Array.isArray(node)) return node.flatMap(buttons);
  if (!isValidElement<{ children?: ReactNode; onClick?(): void }>(node)) return [];
  if (node.type === 'button') return [node as ReactElement<{ onClick(): void }>];
  return buttons(node.props.children);
};

function renderView(overrides: Partial<VirtualRangeViewProps> = {}): string {
  return text(
    renderToString(
      <VirtualRangeView
        shots={[]}
        club="driver"
        profileId="p1"
        sessionGeneration={0}
        source={{ kind: 'live' }}
        processingPhase={null}
        demoActive={false}
        sessionDemo={null}
        onBackToLive={() => {}}
        {...overrides}
      />
    )
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  setActiveLocale('en');
  preference.unitSystem = 'metric';
});

describe('TargetList', () => {
  it('lists the greens nearest first in the profile unit', () => {
    expect(rowTexts(rows(null, 'metric'))).toEqual([
      '●50 m',
      '●75 m',
      '●100 m',
      '●125 m',
      '●150 m',
      '●175 m',
      '●200 m',
      '●230 m',
      'No target✓',
    ]);
    const imperial = rowTexts(rows(null, 'imperial'));
    expect(imperial[0]).toBe('●55 yds');
    expect(imperial.at(-2)).toBe('●252 yds');
    expect(rows(null, 'metric')).toContain('style="color:#ff4b3e"');
  });

  it('marks the selected green, or No target, and offers No target last', () => {
    setActiveLocale('de');
    const html = rows(4, 'metric');
    const texts = rowTexts(html);

    expect(texts[4]).toBe('●150 m✓');
    expect(texts.filter((row) => row.includes('✓'))).toHaveLength(1);
    expect(texts.at(-1)).toBe('Kein Ziel');
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(1);

    const none = rowTexts(rows(null, 'metric'));
    expect(
      none.filter((row) => row.includes('✓')),
      'nothing selected'
    ).toEqual(['Kein Ziel✓']);
  });

  it('selects and closes; outside and Escape close without a change', () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    const all = buttons(TargetRows({ selected: 2, unitSystem: 'metric', onSelect, onClose }));

    all[4].props.onClick();
    expect(onSelect).toHaveBeenLastCalledWith(4);
    all.at(-1)!.props.onClick();
    expect(onSelect).toHaveBeenLastCalledWith(null);
    expect(onClose).toHaveBeenCalledTimes(2);

    const screen = new EventTarget();
    const close = vi.fn();
    const stop = watchTargetList(screen, (event) => (event as Event & { inside?: boolean }).inside === true, close);
    screen.dispatchEvent(Object.assign(new Event('pointerdown'), { inside: true }));
    const key = (name: string) => screen.dispatchEvent(Object.assign(new Event('keydown'), { key: name }));
    key('Enter');
    expect(close, 'a tap in the list and another key').not.toHaveBeenCalled();
    screen.dispatchEvent(new Event('pointerdown'));
    expect(close, 'a tap outside').toHaveBeenCalledOnce();
    key('Escape');
    expect(close, 'Escape').toHaveBeenCalledTimes(2);
    stop();
    key('Escape');
    screen.dispatchEvent(new Event('pointerdown'));
    expect(close, 'no longer watching').toHaveBeenCalledTimes(2);
    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it('shows the selected distance on the button', () => {
    setActiveLocale('de');
    const button = (selected: number | null, unitSystem: UnitSystem) =>
      text(renderToString(<TargetList selected={selected} unitSystem={unitSystem} onSelect={() => {}} />));

    expect(button(null, 'metric')).toMatch(/class="virtual-range__target-button"[^>]*>Ziel<\/button>/);
    expect(button(2, 'metric')).toMatch(/>Ziel 100 m<\/button>/);
    expect(button(2, 'imperial')).toMatch(/>Ziel 109 yds<\/button>/);
    expect(button(2, 'metric'), 'closed').not.toContain('virtual-range__targets');
    setActiveLocale('en');
    expect(button(4, 'metric')).toMatch(/>Target 150 m<\/button>/);
  });

  it('is hidden in training, saved sessions and the range demo', () => {
    const live = (session: Record<string, string> = {}, local: Record<string, string> = {}, search = '') =>
      vi.stubGlobal('window', {
        location: { search },
        localStorage: fakeStorage(local).localStorage,
        sessionStorage: fakeStorage(session).localStorage,
      });

    live({ [TARGET_KEY]: '4' });
    expect(renderView()).toMatch(
      /<div class="virtual-range__top virtual-range__top--live">.*<button type="button" class="virtual-range__target-button"[^>]*>Target 150 m<\/button>/
    );
    live({}, { [TRAINING_KEY]: JSON.stringify({ p1: { targetM: 137, recent: [] } }) });
    expect(renderView(), 'training').not.toContain('virtual-range__target-button');
    live();
    expect(renderView({ source: { kind: 'saved', label: 'Oct 7', shots: [] } }), 'saved').not.toContain(
      'virtual-range__target-button'
    );
    live({}, {}, '?rangeDemo=1');
    expect(renderView(), 'demo').not.toContain('virtual-range__target-button');
  });
});
