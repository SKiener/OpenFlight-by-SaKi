import { useState, type CSSProperties } from 'react';
import { useLabelFitFontSize } from '../../hooks/useFitFontSize';
import { clubGroupLabel } from '../../i18n';
import { useI18n } from '../../i18n/useI18n';
import { PanelAction } from './PanelAction';
import { initialPickerSection, pickerGridRows, type PickerSection } from './pickerSections';

interface PickerOverlayProps {
  title: string;
  selectedId: string;
  sections: ReadonlyArray<PickerSection>;
  onSelect: (id: string) => void;
  onClose: () => void;
  /** Word-length labels (training implements) use a slightly smaller type size. */
  wide?: boolean;
}

/** The smallest a label gets before it may take a third line (0.875rem). */
const LABEL_FLOOR_REM = 0.875;
/** Tabs keep to one line ("SUPERSPEED" shrinks with its row rather than clip). */
const TAB_LINES: readonly number[] = [1];

/**
 * Full-screen picker from design doc 6a (`clubsOpen6`): a titled sheet of
 * hairline-bordered option buttons grouped by tab. Four columns span the
 * overlay; row height is capped so irons stay on screen. The CSS sets each
 * label's ceiling; {@link useLabelFitFontSize} shrinks the open tab's labels
 * together until every one fits its tile.
 */
export function PickerOverlay({ title, selectedId, sections, onSelect, onClose, wide = false }: PickerOverlayProps) {
  const { t } = useI18n();
  const [sectionName, setSectionName] = useState(() => initialPickerSection(sections, selectedId));
  const activeSection = sections.find((section) => section.name === sectionName) ?? sections[0];
  const options = activeSection?.options ?? [];
  const rows = pickerGridRows(sections);
  // Phones show the wide (implement) picker two across; phone.css switches to this.
  const narrowRows = pickerGridRows(sections, 2);
  // Every label of the open tab shares one size: as large as the tiles allow, wrapping only at spaces.
  const gridRef = useLabelFitFontSize<HTMLDivElement>(
    `${activeSection?.name ?? ''}|${options.map((option) => option.id).join(',')}`,
    '.picker-overlay__option',
    LABEL_FLOOR_REM
  );
  // The tabs share one size too: the CSS size, unless the widest name needs less.
  const tabsRef = useLabelFitFontSize<HTMLDivElement>(
    sections.map((section) => section.name).join('|'),
    '.panel-action',
    LABEL_FLOOR_REM,
    TAB_LINES
  );

  return (
    <div
      className={`picker-overlay${wide ? ' picker-overlay--wide' : ''}`}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="picker-overlay__header">
        <span className="picker-overlay__title">{title}</span>
        <button
          type="button"
          className="picker-overlay__close"
          onClick={onClose}
          aria-label={t('picker.close', { title })}
        >
          ✕
        </button>
      </div>
      {sections.length > 1 ? (
        <div ref={tabsRef} className="picker-overlay__tabs" role="group" aria-label={t('picker.groups')}>
          {sections.map((section) => {
            const active = section.name === sectionName;
            return (
              <PanelAction
                key={section.name}
                variant={active ? 'primary' : 'secondary'}
                aria-pressed={active}
                onClick={() => setSectionName(section.name)}
              >
                {clubGroupLabel(section.name)}
              </PanelAction>
            );
          })}
        </div>
      ) : null}
      <div className="picker-overlay__body">
        <div
          ref={gridRef}
          className="picker-overlay__grid"
          style={{ '--picker-rows': String(rows), '--picker-rows-narrow': String(narrowRows) } as CSSProperties}
        >
          {options.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`picker-overlay__option${option.id === selectedId ? ' picker-overlay__option--selected' : ''}`}
              aria-pressed={option.id === selectedId}
              onClick={() => onSelect(option.id)}
            >
              <span className="picker-overlay__label">{option.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
