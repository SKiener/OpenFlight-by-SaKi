import { create } from 'zustand';
import type { TiltCalibrationEvent, TiltCalibrationStatus } from '../types/tiltCalibration';

interface TiltCalibrationStoreState {
  /** The TI tilt calibration status; null until the server first sent it. */
  status: TiltCalibrationStatus | null;
  applyStatus: (status: TiltCalibrationStatus | TiltCalibrationEvent) => void;
}

export const useTiltCalibrationStore = create<TiltCalibrationStoreState>((set) => ({
  status: null,
  applyStatus: (status) => {
    // The socket event and the REST replies wrap the status with `event` / `status`; keep the status only.
    const next: Record<string, unknown> = { ...status };
    delete next.event;
    delete next.status;
    set({ status: next as unknown as TiltCalibrationStatus });
  },
}));
