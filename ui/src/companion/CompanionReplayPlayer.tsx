import { useRef, useState, type CSSProperties, type SyntheticEvent } from 'react';
import type { CameraReplay } from '../types/shot';
import { useI18n } from '../i18n/useI18n';
import { formatReplayTime, getReplayPlaybackRate, getReplayStartTime } from '../utils/replayTiming';
import { cssFrameAspect, frameAspect } from '../utils/frameAspect';
import { COMPANION_PLAYBACK_SPEEDS, DEFAULT_PLAYBACK_SPEED, type CompanionPlaybackSpeed } from './companionReplay';

interface CompanionReplayPlayerProps {
  replay: CameraReplay;
  videoUrl: string;
  onPlaybackError: () => void;
}

export function CompanionReplayPlayer({ replay, videoUrl, onPlaybackError }: CompanionReplayPlayerProps) {
  const { t } = useI18n();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [speed, setSpeed] = useState<CompanionPlaybackSpeed>(DEFAULT_PLAYBACK_SPEED);
  const [paused, setPaused] = useState(true);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(replay.duration_seconds);
  const [videoAspect, setVideoAspect] = useState(frameAspect());
  const speeds = COMPANION_PLAYBACK_SPEEDS.filter((option) => getReplayPlaybackRate(replay, option) >= 0.0625);

  const play = (video: HTMLVideoElement) => {
    void video.play().catch((error: unknown) => {
      if (error && typeof error === 'object' && 'name' in error && error.name === 'AbortError') return;
      // Autoplay can be refused (e.g. iOS low-power mode); the Play button still works.
      setPaused(true);
    });
  };

  const handleLoadedMetadata = (event: SyntheticEvent<HTMLVideoElement>) => {
    const video = event.currentTarget;
    setVideoAspect(frameAspect([video.videoWidth, video.videoHeight]));
    video.playbackRate = getReplayPlaybackRate(replay, speed);
    video.currentTime = getReplayStartTime(replay, speed);
    if (Number.isFinite(video.duration) && video.duration > 0) setDuration(video.duration);
    play(video);
  };

  const selectSpeed = (next: CompanionPlaybackSpeed) => {
    setSpeed(next);
    if (videoRef.current) videoRef.current.playbackRate = getReplayPlaybackRate(replay, next);
  };

  const togglePlayback = () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) play(video);
    else video.pause();
  };

  return (
    <div className="companion-replay">
      <div
        className="companion-replay__viewport"
        style={{ '--replay-aspect': cssFrameAspect(videoAspect) } as CSSProperties}
      >
        <video
          ref={videoRef}
          key={videoUrl}
          src={videoUrl}
          className={`companion-replay__video ${replay.display_mirror_horizontal ? 'companion-replay__video--mirrored' : ''}`}
          aria-label={t('replay.video')}
          muted
          playsInline
          loop
          autoPlay
          preload="auto"
          onClick={togglePlayback}
          onLoadedMetadata={handleLoadedMetadata}
          onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
          onPlay={() => setPaused(false)}
          onPause={() => setPaused(true)}
          onError={onPlaybackError}
        />
      </div>
      <input
        className="companion-replay__scrubber"
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
      <div className="companion-replay__controls">
        <button type="button" className="companion-button companion-button--primary" onClick={togglePlayback}>
          {paused ? t('replay.play') : t('replay.pause')}
        </button>
        <div className="companion-replay__speeds" role="group" aria-label={t('replay.speed')}>
          {speeds.map((option) => (
            <button
              key={option}
              type="button"
              className={`companion-button ${speed === option ? 'companion-button--active' : ''}`}
              aria-pressed={speed === option}
              onClick={() => selectSpeed(option)}
            >
              {`${option}×`}
            </button>
          ))}
        </div>
        <span className="companion-replay__time">
          {formatReplayTime(currentTime)} / {formatReplayTime(duration)}
        </span>
      </div>
    </div>
  );
}
