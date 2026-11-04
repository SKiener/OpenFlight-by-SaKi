import type { ReactNode } from 'react';
import type { ShotProcessingPhase } from '../stores/useShotStore';
import { ProgressIndicator } from './ProgressIndicator';
import { useI18n, type MessageKey } from '../i18n/useI18n';
import './ShotProcessingArea.css';

interface ShotProcessingAreaProps {
  phase: ShotProcessingPhase | null;
  children: ReactNode;
}

const ENRICHMENT_DETAILS: Partial<Record<ShotProcessingPhase, MessageKey>> = {
  iwr_dump: 'processing.iwrDump',
  camera_processing: 'processing.camera',
  hardware_enrichment: 'processing.enrichment',
};

/** The status while the IWR radar or the camera still works on a shot whose OPS metrics are in. */
export function EnrichmentProgress({ phase }: { phase: ShotProcessingPhase | null }) {
  const { t } = useI18n();
  const detail = phase ? ENRICHMENT_DETAILS[phase] : undefined;
  return <ProgressIndicator variant="inline" title={t('processing.opsReady')} detail={detail && t(detail)} />;
}

export function ShotProcessingArea({ phase, children }: ShotProcessingAreaProps) {
  const { t } = useI18n();

  if (phase && ENRICHMENT_DETAILS[phase]) {
    return (
      <>
        <div className="shot-processing-status">
          <EnrichmentProgress phase={phase} />
        </div>
        {children}
      </>
    );
  }

  return (
    <>
      {phase ? (
        <div className="shot-processing-overlay">
          <div className="shot-processing-card">
            <ProgressIndicator
              variant="dialog"
              title={phase === 'capturing' ? t('processing.impact') : t('processing.captured')}
              detail={phase === 'capturing' ? t('processing.capture') : t('processing.calculate')}
            />
          </div>
        </div>
      ) : null}
      {children}
    </>
  );
}
