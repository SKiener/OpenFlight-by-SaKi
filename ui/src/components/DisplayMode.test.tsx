import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY } from '../test-utils/shot';
import type { CameraCaptureSettings } from '../stores/useCameraStore';
import type { Shot } from '../types/shot';
import { DisplayMode } from './DisplayMode';

const captureSettings: CameraCaptureSettings = {
  available: true,
  enabled: true,
  running: true,
};

const shot: Shot = {
  ball_speed_mph: 151.2,
  club_speed_mph: 101.1,
  smash_factor: 1.5,
  estimated_carry_yards: 254,
  carry_range: [244, 264],
  club: 'driver',
  timestamp: '2026-05-18T12:00:00Z',
  peak_magnitude: 42,
  launch_angle_vertical: 13.4,
  launch_angle_horizontal: -1.2,
  launch_angle_confidence: 0.82,
  angle_source: 'radar',
  club_angle_deg: 1.1,
  club_path_deg: 2.5,
  spin_axis_deg: -3.1,
  spin_rpm: 2450,
  spin_confidence: 0.8,
  spin_quality: 'high',
  spin_source: 'calculated',
  carry_spin_adjusted: 261,
};

describe('DisplayMode', () => {
  afterEach(() => setActiveLocale('en'));

  it('says that the demo runs, without a way out on this view-only page', () => {
    const render = (demoActive: boolean) =>
      renderToString(
        <DisplayMode
          connected
          captureSettings={captureSettings}
          latestShot={shot}
          shots={[shot]}
          demoActive={demoActive}
        />
      );

    const demo = render(true);
    expect(demo).toMatch(/<main class="display-mode"><div class="demo-band/);
    expect(demo).toContain('DEMO – real shots are ignored');
    expect(demo).not.toContain('<button');
    expect(render(false)).not.toContain('demo-band');
  });

  it.each(['en', 'de', 'es'] as const)(
    '%s: never shows the spin axis or any other value this hardware cannot measure',
    (locale) => {
      setActiveLocale(locale);
      const full = { ...shot, ...UNMEASURABLE_CLUB_DATA } as Shot;
      const html = renderToString(
        <DisplayMode connected captureSettings={captureSettings} latestShot={full} shots={[full]} demoActive={false} />
      ).replace(/<!-- -->/g, '');
      for (const word of [...UNMEASURABLE_COPY[locale], 'Fade']) expect(html, word).not.toContain(word);
      expect(html).not.toMatch(/-3[.,]1|12[.,]2|19[.,]1|mm/);
    }
  );

  it('renders latest shot metrics and recent shot strip', () => {
    const html = renderToString(
      <DisplayMode connected captureSettings={captureSettings} latestShot={shot} shots={[shot]} demoActive={false} />
    );

    expect(html).toContain('OpenFlight Display');
    expect(html).toContain('151.2');
    expect(html).toContain('261');
    expect(html).toContain('Socket connected');
    expect(html).toContain('display-shot-chip__number');
    expect(html).toContain('metric-card--emphasis');
    expect(html).not.toContain('display-metric');
  });

  it('shows the spin the carry used: measured with its quality, typical marked typical, none as --', () => {
    const spinCard = (spinShot: Shot) =>
      renderToString(
        <DisplayMode
          connected
          captureSettings={captureSettings}
          latestShot={spinShot}
          shots={[spinShot]}
          demoActive={false}
        />
      )
        .split('<div class="metric-card ')
        .find((card) => card.includes('>Spin<')) ?? '';
    const measured = {
      ...shot,
      spin_source: 'measured' as const,
      carry_spin_rpm: 2450,
      carry_spin_source: 'measured' as const,
    };
    const typical = {
      ...shot,
      spin_rpm: 1538,
      spin_quality: 'low' as const,
      spin_source: null,
      carry_spin_rpm: 7500,
      carry_spin_source: 'club_typical' as const,
    };

    expect(spinCard(measured)).toContain('>2,450<');
    expect(spinCard(measured)).toContain('>high<');
    expect(spinCard(typical)).toContain('>7,500<');
    expect(spinCard(typical)).toContain('>typical<');
    expect(spinCard(typical)).not.toContain('1,538');
    expect(spinCard(typical)).not.toContain('>low<');
    expect(spinCard(shot)).toContain('>--<');
    expect(spinCard(shot)).not.toContain('2,450');
  });

  it('does not request a preview when camera capture is unavailable', () => {
    const html = renderToString(
      <DisplayMode
        connected
        captureSettings={{ available: false }}
        latestShot={shot}
        shots={[shot]}
        demoActive={false}
      />
    );

    expect(html).not.toContain('/api/camera/preview.jpg');
    expect(html).toContain('Camera unavailable');
  });

  const clubCard = (clubShot: Shot, label: string) =>
    renderToString(
      <DisplayMode
        connected
        captureSettings={captureSettings}
        latestShot={clubShot}
        shots={[clubShot]}
        demoActive={false}
      />
    )
      .split('<div class="metric-card ')
      .find((card) => card.includes(`>${label}<`)) ?? '';

  it('keeps the club tiles empty for unaccepted TI candidates and the chained camera estimate', () => {
    const unaccepted: Shot = {
      ...shot,
      club_angle_deg: null,
      club_path_deg: null,
      experimental_attack_angle_deg: -33.9,
      experimental_attack_angle_status: 'candidate_out_of_bounds',
      experimental_club_path_deg: 30.0,
      experimental_club_path_status: 'rejected_azimuth_fit',
    };
    const chained: Shot = {
      ...shot,
      club_angle_deg: null,
      club_path_deg: null,
      experimental_fused_attack_angle_deg: -4.2,
      experimental_fused_club_path_deg: -19.7,
      experimental_fused_status: 'approach_mixed',
    };

    const path = clubCard(unaccepted, 'Club Path');
    expect(path).toContain('>--<');
    expect(path).not.toContain('30');
    expect(path).toContain('experimental · rejected: azimuth fit');
    const attack = clubCard(unaccepted, 'Attack angle');
    expect(attack).toContain('>--<');
    expect(attack).not.toContain('33.9');
    // Only a rejection captions an empty tile; a candidate caption would suggest a value.
    expect(attack).not.toContain('candidate');
    expect(attack).not.toContain('experimental');
    for (const label of ['Club Path', 'Attack angle']) {
      const card = clubCard(chained, label);
      expect(card).toContain('>--<');
      expect(card).not.toContain('19.7');
      expect(card).not.toContain('4.2');
      expect(card).not.toContain('camera fused');
      expect(card).not.toContain('experimental');
    }
  });

  it('captions no withheld TI candidate, with or without its status', () => {
    for (const status of [null, 'candidate_available', 'candidate_noisy_fit']) {
      const candidate: Shot = {
        ...shot,
        club_angle_deg: null,
        club_path_deg: null,
        experimental_club_path_deg: 12.0,
        experimental_club_path_status: status,
        experimental_attack_angle_deg: -33.9,
        experimental_attack_angle_status: status,
      };

      for (const label of ['Club Path', 'Attack angle']) {
        const card = clubCard(candidate, label);
        expect(card).toContain('>--<');
        expect(card).not.toContain('candidate');
        expect(card).not.toContain('experimental');
      }
    }
  });

  it('keeps showing an accepted TI club path', () => {
    const accepted: Shot = {
      ...shot,
      club_path_deg: null,
      experimental_club_path_deg: 2.4,
      experimental_club_path_status: 'accepted',
      experimental_fused_club_path_deg: -19.7,
      experimental_fused_status: 'approach_mixed',
    };

    const card = clubCard(accepted, 'Club Path');
    expect(card).toContain('>+2.4<');
    expect(card).toContain('experimental · not calibrated');
  });

  it('captions the radar club path and attack angle "not calibrated"', () => {
    for (const label of ['Club Path', 'Attack angle']) {
      const card = clubCard(shot, label);
      expect(card, label).toContain('not calibrated');
      expect(card, label).not.toContain('calculated');
      expect(card, label).not.toContain('experimental');
    }
    const empty = clubCard({ ...shot, club_path_deg: null }, 'Club Path');
    expect(empty).toContain('>--<');
    expect(empty).not.toContain('calibrated');
  });

  it('never shows the uncalibrated camera attack angle or low point, so it needs no ≈', () => {
    const camera: Shot = {
      ...shot,
      club_angle_deg: null,
      camera_attack_angle_deg: -10.4,
      camera_attack_angle_sigma_deg: 0.8,
      camera_low_point_cm: 23.1,
      camera_low_point_sigma_cm: 2.1,
    };
    const attack = clubCard(camera, 'Attack angle');
    expect(attack).toContain('>--<');
    expect(attack).not.toContain('10.4');
    const page = renderToString(
      <DisplayMode connected captureSettings={captureSettings} latestShot={camera} shots={[camera]} demoActive={false} />
    );
    expect(page).not.toContain('23.1');
    expect(page).not.toContain('Low point');
  });

  it('shows rejection details for status-only experimental club metrics', () => {
    const rejectedShot: Shot = {
      ...shot,
      club_angle_deg: null,
      club_path_deg: null,
      experimental_attack_angle_status: 'rejected_no_club_track',
      experimental_club_path_status: 'rejected_no_pre_impact_frames',
    };

    const html = renderToString(
      <DisplayMode
        connected
        captureSettings={captureSettings}
        latestShot={rejectedShot}
        shots={[rejectedShot]}
        demoActive={false}
      />
    );

    expect(html).toContain('experimental · rejected: no club track');
    expect(html).toContain('experimental · rejected: no pre impact frames');
  });
});
