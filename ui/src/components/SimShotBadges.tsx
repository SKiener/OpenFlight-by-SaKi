import './SimShotBadges.css';
import type { MessageKey } from '../i18n/en';
import { useI18n } from '../i18n/useI18n';
import type { SimShotInfo } from '../types/socket';

const DISPLAY_NAMES: Record<string, string> = {
  gspro: 'GSPro',
  opengolfsim: 'OpenGolfSim',
  partee: 'PAR-TEE',
};

// Human labels for the logical field keys shared by all connectors.
const FIELD_LABELS: Record<string, string> = {
  ball_speed: 'Ball',
  vla: 'VLA',
  hla: 'HLA',
  total_spin: 'Spin',
  back_spin: 'Backspin',
  carry: 'Carry',
  club_speed: 'Club',
  club_path: 'Path',
};

// Still exported to the simulator, but not shown: the screens hide the spin
// axis and side spin because this hardware cannot measure them.
const HIDDEN_FIELDS = new Set(['spin_axis', 'side_spin']);

function formatValue(value: number | null): string {
  if (value === null || value === undefined) return '—';
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** Marker per provenance: M, E, or the word for a calculated value (D-plane axis, side spin). */
function provenanceBadge(prov: string, t: (key: MessageKey) => string): { text: string; title: string } {
  if (prov === 'measured') return { text: 'M', title: t('sim.measured') };
  if (prov === 'calculated') return { text: t('metric.calculated'), title: t('metric.calculated') };
  return { text: 'E', title: t('sim.estimated') };
}

interface SimShotBadgesProps {
  latestSimShots: Record<string, SimShotInfo>;
}

export function SimShotBadges({ latestSimShots }: SimShotBadgesProps) {
  const { t } = useI18n();
  const sims = Object.values(latestSimShots);
  if (sims.length === 0) {
    return null;
  }
  return (
    <div className="sim-shot-badges">
      {sims.map((sim) => {
        const fields = sim.fields.filter((f) => f in sim.values && !HIDDEN_FIELDS.has(f));
        const measured = fields.filter((f) => sim.provenance[f] === 'measured').length;
        const calculated = fields.filter((f) => sim.provenance[f] === 'calculated').length;
        const estimated = fields.length - measured - calculated;
        return (
          <div key={sim.target} className="sim-shot-badges__card">
            <div className="sim-shot-badges__header">
              <span className="sim-shot-badges__title">
                {t('sim.sentTo', { name: DISPLAY_NAMES[sim.target] ?? sim.target })}
              </span>
              <span className="sim-shot-badges__shot">{`#${sim.shot_number}`}</span>
            </div>
            <div className="sim-shot-badges__grid">
              {fields.map((f) => {
                const prov = sim.provenance[f] ?? 'estimated';
                const badge = provenanceBadge(prov, t);
                return (
                  <div key={f} className="sim-shot-badges__item">
                    <span className="sim-shot-badges__label">{FIELD_LABELS[f] ?? f}</span>
                    <span className="sim-shot-badges__value">{formatValue(sim.values[f])}</span>
                    <span
                      className={`sim-shot-badges__badge sim-shot-badges__badge--${prov}`}
                      title={badge.title}
                    >
                      {badge.text}
                    </span>
                  </div>
                );
              })}
            </div>
            <div className="sim-shot-badges__summary">
              {calculated > 0
                ? t('sim.summaryCalculated', { measured, calculated, estimated })
                : t('sim.summary', { measured, estimated })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
