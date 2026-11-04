import { useI18n } from '../i18n/useI18n';
import { isBallSpeedUncertain, type Shot } from '../types/shot';

/**
 * The note under the TI radar alert, in its band, while the latest shot's ball
 * speed is uncertain: the OPS ball-track guard kept a number that reads like the
 * club head's trailing track. Its tiles carry the estimated mark; the next shot
 * replaces the note.
 */
export function BallSpeedUncertainNote({ shot }: { shot: Shot | null }) {
  const { t } = useI18n();

  if (shot === null || !isBallSpeedUncertain(shot)) return null;
  return (
    <div className="iwr-alert ball-speed-note" role="status">
      <div>
        <span>{t('live.ballSpeedUncertainDetail')}</span>
      </div>
    </div>
  );
}
