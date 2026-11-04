import { create } from 'zustand';
import type { PowerStatus } from '../types/power';
import type { DemoState, SimShotInfo, SimStatus } from '../types/socket';
import type { CalibrationStatus } from '../types/shot';

interface SystemState {
  connected: boolean;
  mockMode: boolean;
  debugMode: boolean;
  demoActive: boolean;
  demoUnavailable: NonNullable<DemoState['unavailable']> | null;
  cloudUploadState: 'idle' | 'running' | 'complete' | 'error';
  cloudUploadMessage: string;
  simStatuses: Record<string, SimStatus>;
  latestSimShots: Record<string, SimShotInfo>;
  serverClub: string | null;
  powerStatus: PowerStatus | null;
  calibration: CalibrationStatus | null;
  setConnected: (connected: boolean) => void;
  setMockMode: (mockMode: boolean) => void;
  setDebugMode: (debugMode: boolean) => void;
  setDemoState: (state: DemoState) => void;
  setDemoActive: (demoActive: boolean) => void;
  setCloudUploadStatus: (state: SystemState['cloudUploadState'], message: string) => void;
  setSimStatus: (status: SimStatus) => void;
  setLatestSimShot: (shot: SimShotInfo) => void;
  setServerClub: (club: string | null) => void;
  setPowerStatus: (status: PowerStatus) => void;
  setCalibration: (calibration: CalibrationStatus) => void;
}

export const useSystemStore = create<SystemState>((set) => ({
  connected: false,
  mockMode: false,
  debugMode: false,
  demoActive: false,
  demoUnavailable: null,
  cloudUploadState: 'idle',
  cloudUploadMessage: '',
  simStatuses: {},
  latestSimShots: {},
  serverClub: null,
  powerStatus: null,
  calibration: null,
  setConnected: (connected) => set({ connected }),
  setMockMode: (mockMode) => set({ mockMode }),
  setDebugMode: (debugMode) => set({ debugMode }),
  setDemoState: ({ active, unavailable }) => set({ demoActive: active, demoUnavailable: unavailable ?? null }),
  setDemoActive: (demoActive) => set({ demoActive }),
  setCloudUploadStatus: (cloudUploadState, cloudUploadMessage) => set({ cloudUploadState, cloudUploadMessage }),
  setSimStatus: (status) =>
    set((state) => ({
      simStatuses: { ...state.simStatuses, [status.target]: status },
    })),
  setLatestSimShot: (shot) =>
    set((state) => ({
      latestSimShots: { ...state.latestSimShots, [shot.target]: shot },
    })),
  setServerClub: (serverClub) => set({ serverClub }),
  setPowerStatus: (status) => set({ powerStatus: status }),
  setCalibration: (calibration) => set({ calibration }),
}));

/** Whether the TI start direction's phase zero is calibrated; false until the server says so. */
export function useDirectionCalibrated(): boolean {
  return useSystemStore((state) => state.calibration?.horizontal_phase_reference === true);
}
