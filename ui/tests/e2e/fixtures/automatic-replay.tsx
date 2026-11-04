import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { CameraReplayDialog } from '../../../src/components/CameraReplayDialog';
import { useCameraReplayController } from '../../../src/hooks/useCameraReplayController';
import { subscribeToAutomaticReplays } from '../../../src/services/automaticReplay';
import { useShotStore } from '../../../src/stores/useShotStore';
import { useLocaleStore } from '../../../src/stores/useLocaleStore';
import type { Shot, CameraReplay } from '../../../src/types/shot';

const replay: CameraReplay = {
  id: 'fixture-real-clip',
  frame_count: 60,
  trigger_frame: 44,
  playback_fps: 60,
  capture_fps: 287.88492410084757,
  duration_seconds: 1,
  display_mirror_horizontal: false,
};
const prepare = async () => ({ ...replay, videoUrl: '/fixture-replay.mp4' });
useLocaleStore.getState().setLocale('de');

function Fixture() {
  const { activeReplay, openReplay, closeReplay, reportPlaybackError } = useCameraReplayController(prepare);
  const [opens, setOpens] = useState(0);
  const [closes, setCloses] = useState(0);
  const [ends, setEnds] = useState(0);
  const lastShot = useRef<Shot | null>(null);
  useEffect(
    () =>
      subscribeToAutomaticReplays((clip) => {
        setOpens((count) => count + 1);
        openReplay(clip, true);
      }),
    [openReplay]
  );
  const close = () => {
    setCloses((count) => count + 1);
    closeReplay();
  };
  return (
    <>
      <h1>Live · Zeitlupentest ohne Radar</h1>
      <p>
        Geöffnet: {opens} · Geschlossen: {closes} · Abgespielte Durchläufe: {ends}
      </p>
      <button
        onClick={() => {
          const shot = { timestamp: new Date().toISOString(), camera_replay: replay } as Shot;
          lastShot.current = shot;
          useShotStore.getState().addShot({ ...shot, camera_replay: null });
          setTimeout(() => useShotStore.getState().updateShot(shot), 200);
        }}
      >
        Neuen Schlag mit Video simulieren
      </button>
      <button
        onClick={() => {
          if (lastShot.current) useShotStore.getState().updateShot({ ...lastShot.current });
        }}
      >
        Gleichen Schlag erneut ergänzen
      </button>
      <button onClick={() => useShotStore.getState().setShots([{ timestamp: 'old', camera_replay: replay } as Shot])}>
        Verlauf laden
      </button>
      <button onClick={() => openReplay(replay)}>Manuell öffnen</button>
      <div onEndedCapture={() => setEnds((count) => count + 1)}>
        {activeReplay ? (
          <CameraReplayDialog
            key={activeReplay.replay.id}
            replay={activeReplay.replay}
            state={activeReplay.state}
            automatic={activeReplay.automatic}
            onClose={close}
            onRetry={() => openReplay(activeReplay.replay, activeReplay.automatic)}
            onPlaybackError={reportPlaybackError}
          />
        ) : null}
      </div>
    </>
  );
}
createRoot(document.getElementById('root')!).render(<Fixture />);
