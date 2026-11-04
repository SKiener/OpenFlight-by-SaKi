import { useEffect, useEffectEvent, useRef, useState, type ReactElement } from 'react';
import { t } from '../i18n';
import { useI18n } from '../i18n/useI18n';
import type { UnitSystem } from '../utils/units';
import { distanceLabel } from './distance';
import { GREENS } from './layout';
import { watchTargetList } from './watchTargetList';

export interface TargetListProps {
  selected: number | null;
  unitSystem: UnitSystem;
  onSelect(index: number | null): void;
  onOpenChange?(open: boolean): void;
}

interface TargetRowsProps {
  selected: number | null;
  unitSystem: UnitSystem;
  onSelect(index: number | null): void;
  onClose(): void;
}

const NEAREST_FIRST = GREENS.map((green, index) => ({ ...green, index })).sort((a, b) => a.distanceM - b.distanceM);

export function TargetRows({ selected, unitSystem, onSelect, onClose }: TargetRowsProps): ReactElement[] {
  const pick = (index: number | null): void => {
    onSelect(index);
    onClose();
  };
  const cells = [
    ...NEAREST_FIRST.map(({ index, distanceM, flag }) => ({
      index,
      flag,
      label: distanceLabel(distanceM, unitSystem),
    })),
    { index: null, flag: null, label: t('virtualRange.noTarget') },
  ];
  return cells.map(({ index, flag, label }) => (
    <li key={index ?? 'none'}>
      <button
        type="button"
        className="virtual-range__target-row"
        aria-pressed={index === selected}
        onClick={() => pick(index)}
      >
        {flag === null ? null : (
          <span className="virtual-range__target-dot" style={{ color: flag }} aria-hidden="true">
            ●
          </span>
        )}
        <span className="virtual-range__target-label">{label}</span>
        {index === selected ? (
          <span className="virtual-range__target-check" aria-hidden="true">
            ✓
          </span>
        ) : null}
      </button>
    </li>
  ));
}

export function TargetList({ selected, unitSystem, onSelect, onOpenChange }: TargetListProps): ReactElement {
  useI18n();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const green = selected === null ? undefined : GREENS[selected];

  const toggle = (next: boolean): void => {
    setOpen(next);
    onOpenChange?.(next);
  };

  const close = useEffectEvent(() => toggle(false));

  useEffect(() => {
    if (!open) return;
    return watchTargetList(
      document,
      (event) => rootRef.current?.contains(event.target as Node) === true,
      () => close()
    );
  }, [open]);

  return (
    <div className="virtual-range__target" ref={rootRef}>
      <button type="button" className="virtual-range__target-button" aria-expanded={open} onClick={() => toggle(!open)}>
        {green
          ? t('virtualRange.targetAt', { distance: distanceLabel(green.distanceM, unitSystem) })
          : t('virtualRange.target')}
      </button>
      {open ? (
        <ul className="virtual-range__targets">
          <TargetRows selected={selected} unitSystem={unitSystem} onSelect={onSelect} onClose={() => toggle(false)} />
        </ul>
      ) : null}
    </div>
  );
}
