import { useEffect, useRef, useState, type CSSProperties, type SyntheticEvent } from 'react';
import type { CameraReplay } from '../types/shot';
import { useI18n } from '../i18n/useI18n';
import { ProgressIndicator } from './ProgressIndicator';
import { formatReplayTime, getReplayStartTime, getReplayPlaybackRate } from '../utils/replayTiming';
import { cssFrameAspect, frameAspect } from '../utils/frameAspect';
import { scheduleRefusedAutomaticReturn, startReplayPlayback } from '../utils/replayPlayback';
import './CameraReplayDialog.css';

export type CameraReplayDialogState =
  | { kind: 'preparing' }
  | { kind: 'ready'; videoUrl: string }
  | { kind: 'error'; stage?: 'preparation' | 'playback'; message?: string };

interface CameraReplayDialogProps {
  replay: CameraReplay;
  state: CameraReplayDialogState;
  onClose: () => void;
  onRetry: () => void;
  onPlaybackError?: () => void;
  automatic?: boolean;
}

const PLAYBACK_RATES = [1, 0.5, 0.25, 0.1, 0.05, 0.025] as const;

// The automatic replay after each shot plays this many passes, then returns to Live.
export const AUTOMATIC_REPLAY_PASSES = 2;

export function CameraReplayDialog({
  replay,
  state,
  onClose,
  onRetry,
  onPlaybackError,
  automatic = false,
}: CameraReplayDialogProps) {
  const { t } = useI18n();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(replay.duration_seconds);
  const [paused, setPaused] = useState(true);
  const [playbackRate, setPlaybackRate] = useState<(typeof PLAYBACK_RATES)[number]>(automatic ? 0.1 : 1);
  const completedPasses = useRef(0);
  const [pass, setPass] = useState(1);
  const [loopEnabled, setLoopEnabled] = useState(false);
  // The platform refused to start playback without a tap (iOS Low Power Mode, thermal limits).
  const [playBlocked, setPlayBlocked] = useState(false);
  // The viewport takes the frame's own shape once the video reports its size.
  const [videoAspect, setVideoAspect] = useState(frameAspect());
  const viewportStyle = { '--replay-aspect': cssFrameAspect(videoAspect) } as CSSProperties;
  const startTime = getReplayStartTime(replay, playbackRate);
  const impactPosition = replay.frame_count > 1 ? (replay.trigger_frame / (replay.frame_count - 1)) * 100 : 0;
  const timelineStyle = { '--replay-impact-position': `${impactPosition}%` } as CSSProperties;

  const play = (video: HTMLVideoElement) => {
    void startReplayPlayback(video).then((result) => {
      if (result === 'blocked') setPlayBlocked(true);
      else if (result === 'failed') onPlaybackError?.();
    });
  };

  useEffect(() => {
    if (!automatic || state.kind !== 'error') return;
    const timeout = setTimeout(onClose, 4000);
    return () => clearTimeout(timeout);
  }, [automatic, state.kind, onClose]);

  // A tap plays (onPlay clears playBlocked), which cancels this; the usual end-of-replay return applies.
  useEffect(
    () => scheduleRefusedAutomaticReturn({ automatic, blocked: playBlocked, paused }, onClose),
    [automatic, playBlocked, paused, onClose]
  );

  const togglePlayback = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      if (video.ended) video.currentTime = startTime;
      play(video);
    } else {
      video.pause();
    }
  };

  const restart = () => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = startTime;
    setCurrentTime(startTime);
    play(video);
  };

  const selectPlaybackRate = (rate: (typeof PLAYBACK_RATES)[number]) => {
    setPlaybackRate(rate);
    const video = videoRef.current;
    const nextStart = getReplayStartTime(replay, rate);
    setCurrentTime(nextStart);
    if (video) {
      const wasEnded = video.ended;
      video.playbackRate = getReplayPlaybackRate(replay, rate);
      video.currentTime = nextStart;
      if (wasEnded) play(video);
    }
  };

  const updateDuration = (event: SyntheticEvent<HTMLVideoElement>) => {
    setVideoAspect(frameAspect([event.currentTarget.videoWidth, event.currentTarget.videoHeight]));
    event.currentTarget.playbackRate = getReplayPlaybackRate(replay, playbackRate);
    event.currentTarget.currentTime = startTime;
    setCurrentTime(startTime);
    const videoDuration = event.currentTarget.duration;
    if (Number.isFinite(videoDuration) && videoDuration > 0) {
      setDuration(videoDuration);
    }
    // `autoPlay` fails silently where the platform needs a tap; asking explicitly tells
    // us so, and the frame at startTime stays on screen behind a play control.
    if (event.currentTarget.paused) play(event.currentTarget);
  };

  return (
    <div className="camera-replay" role="presentation">
      <section className="camera-replay__dialog" role="dialog" aria-modal="true" aria-label={t('replay.title')}>
        {state.kind === 'preparing' ? (
          <div className="camera-replay__status">
            <ProgressIndicator variant="dialog" title={t('replay.preparing')} detail={t('replay.preparingDetail')} />
            <button type="button" className="camera-replay__button" onClick={onClose}>
              {t('replay.close')}
            </button>
          </div>
        ) : null}

        {state.kind === 'error' ? (
          <div className="camera-replay__status">
            <h2>{state.stage === 'playback' ? t('replay.playbackError') : t('replay.error')}</h2>
            <p>
              {state.message ??
                (state.stage === 'playback' ? t('replay.playbackErrorDetail') : t('replay.errorDetail'))}
            </p>
            <div className="camera-replay__status-actions">
              <button type="button" className="camera-replay__button camera-replay__button--primary" onClick={onRetry}>
                {t('replay.tryAgain')}
              </button>
              <button type="button" className="camera-replay__button" onClick={onClose}>
                {t('replay.close')}
              </button>
            </div>
          </div>
        ) : null}

        {state.kind === 'ready' ? (
          <>
            <header className="camera-replay__header">
              <div>
                <span className="camera-replay__eyebrow">{t('replay.slowMotion')}</span>
                <h2>{t('replay.title')}</h2>
              </div>
              <button type="button" className="camera-replay__button" onClick={onClose}>
                {t('replay.close')}
              </button>
            </header>
            <div className="camera-replay__body">
              <div className="camera-replay__stage">
                <div className="camera-replay__viewport" style={viewportStyle}>
                  <video
                    ref={videoRef}
                    src={state.videoUrl}
                    className={`camera-replay__video ${replay.display_mirror_horizontal ? 'camera-replay__video--mirrored' : ''}`}
                    aria-label={t('replay.video')}
                    autoPlay
                    muted
                    playsInline
                    preload="auto"
                    onClick={togglePlayback}
                    onLoadedMetadata={updateDuration}
                    onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                    onPlay={() => {
                      setPaused(false);
                      setPlayBlocked(false);
                    }}
                    onPause={() => setPaused(true)}
                    onEnded={() => {
                      if (automatic) {
                        completedPasses.current += 1;
                        if (completedPasses.current >= AUTOMATIC_REPLAY_PASSES) onClose();
                        else {
                          setPass(completedPasses.current + 1);
                          restart();
                        }
                      } else if (loopEnabled) restart();
                      else setPaused(true);
                    }}
                    onError={() => onPlaybackError?.()}
                  />
                  {playBlocked && paused ? (
                    <div className="camera-replay__play-prompt">
                      <button
                        type="button"
                        className="camera-replay__button camera-replay__button--primary camera-replay__play-prompt-button"
                        onClick={togglePlayback}
                      >
                        {t('replay.play')}
                      </button>
                    </div>
                  ) : null}
                </div>
              </div>
              <div className="camera-replay__controls">
                <div className="camera-replay__timeline" style={timelineStyle}>
                  <input
                    className="camera-replay__scrubber"
                    type="range"
                    min="0"
                    max={Math.max(duration, 0.01)}
                    step="0.01"
                    value={Math.min(currentTime, duration)}
                    aria-label={t('replay.scrub')}
                    onChange={(event) => {
                      const nextTime = Number(event.target.value);
                      setCurrentTime(nextTime);
                      if (videoRef.current) videoRef.current.currentTime = nextTime;
                    }}
                  />
                  <span className="camera-replay__impact" aria-label={t('replay.impact')} />
                </div>
                <div className="camera-replay__playback-row">
                  <span className="camera-replay__playback-label">{t('replay.speed')}</span>
                  <div className="camera-replay__rate-options" role="group" aria-label={t('replay.speed')}>
                    {PLAYBACK_RATES.filter((rate) => getReplayPlaybackRate(replay, rate) >= 0.0625).map((rate) => (
                      <button
                        key={rate}
                        type="button"
                        className={`camera-replay__button camera-replay__rate ${playbackRate === rate ? 'camera-replay__button--active' : ''}`}
                        aria-pressed={playbackRate === rate}
                        onClick={() => selectPlaybackRate(rate)}
                      >
                        {`${rate}×`}
                      </button>
                    ))}
                  </div>
                  {automatic ? (
                    <p role="status">{t('replay.automaticPass', { n: pass, total: AUTOMATIC_REPLAY_PASSES })}</p>
                  ) : (
                    <button
                      type="button"
                      className={`camera-replay__button camera-replay__loop ${loopEnabled ? 'camera-replay__button--active' : ''}`}
                      aria-pressed={loopEnabled}
                      onClick={() => setLoopEnabled((enabled) => !enabled)}
                    >
                      {t('replay.loop')}
                    </button>
                  )}
                </div>
                <div className="camera-replay__control-row">
                  <button
                    type="button"
                    className="camera-replay__button camera-replay__button--primary"
                    onClick={togglePlayback}
                  >
                    {paused ? t('replay.play') : t('replay.pause')}
                  </button>
                  <button type="button" className="camera-replay__button" onClick={restart}>
                    {t('replay.restart')}
                  </button>
                  <span className="camera-replay__time">
                    {formatReplayTime(currentTime)} / {formatReplayTime(duration)}
                  </span>
                </div>
              </div>
            </div>
          </>
        ) : null}
      </section>
    </div>
  );
}
