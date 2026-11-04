import { afterEach, describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { setActiveLocale } from '../../i18n';
import { cliTiltStatus, TILT_NOW_MS, tiltStatus } from '../../test-utils/tiltStatus';
import { TiltSectionView, type TiltFlowActions, type TiltSectionViewProps } from './TiltSection';

const noop = () => {};
const actions: TiltFlowActions = { begin: noop, next: noop, confirm: noop, flip: noop, measure: noop, retry: noop };

function render(overrides: Partial<TiltSectionViewProps> = {}) {
  return renderToString(
    <TiltSectionView
      status={tiltStatus()}
      now={TILT_NOW_MS}
      kiosk={false}
      connected
      flow={{ step: 'idle', notice: null }}
      actions={actions}
      onReset={() => Promise.resolve()}
      {...overrides}
    />
  )
    .replace(/<!-- -->/g, '')
    .replace(/&#x27;/g, "'");
}

describe('TiltSection status', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows the phone measurement, what it found and the sensor difference', () => {
    setActiveLocale('de');
    const html = render();
    expect(html).toContain('menu-sheet__section-title">Radar-Neigung');
    expect(html).toContain('11,2° · per Handy gemessen vor 3 Tagen');
    expect(html).toContain('Gerät gemessen: vorne 1,2° höher');
    expect(html).toContain('Neigungssensor weicht 0,2° ab');
  });

  it('shows the fixed tilt without detail lines or reset', () => {
    setActiveLocale('de');
    const html = render({ status: cliTiltStatus() });
    expect(html).toContain('10,4° · fest (Startskript)');
    expect(html).not.toContain('Gerät gemessen');
    expect(html).toContain('>Einmessen</button>');
    expect(html).not.toContain('Zurücksetzen');
  });

  it('says when TI is off and offers no measurement', () => {
    setActiveLocale('de');
    const html = render({ status: tiltStatus({ enabled: false, tilt_source: null, configured_iwr_tilt_deg: null }) });
    expect(html).toContain('TI nicht aktiv');
    expect(html).not.toContain('Einmessen</button>');
    expect(html).not.toContain('Zurücksetzen');
  });

  it('has the title in every language', () => {
    expect(render()).toContain('>Radar tilt<');
    setActiveLocale('es');
    expect(render()).toContain('>Inclinación del radar<');
  });
});

describe('TiltSection kiosk vs phone', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows the kiosk only the status and where to calibrate', () => {
    setActiveLocale('de');
    const html = render({ kiosk: true, actions: null });
    expect(html).toContain('11,2° · per Handy gemessen vor 3 Tagen');
    expect(html).toContain('Einmessen am Handy im Menü');
    expect(html).not.toContain('<button');
  });

  it('offers calibrate and the two-step reset on the phone when the tilt is from the phone', () => {
    setActiveLocale('de');
    const html = render();
    expect(html).toContain('class="panel-action panel-action--primary">Einmessen</button>');
    expect(html).toMatch(/class="confirm-delete panel-action panel-action--secondary"[^>]*>Zurücksetzen<\/button>/);
    expect(html).not.toContain('Einmessen am Handy');
  });

  it('disables calibrate and reset while the Pi is not connected', () => {
    const html = render({ connected: false });
    expect(html).toMatch(/disabled=""[^>]*>Calibrate<\/button>/);
    expect(html).toMatch(/disabled=""[^>]*>Reset<\/button>/);
  });
});

describe('TiltSection steps', () => {
  afterEach(() => setActiveLocale('en'));

  it('1: tells where to lay the phone, then "next"', () => {
    setActiveLocale('de');
    const html = render({ flow: { step: 'intro', requesting: false } });
    expect(html).toContain(
      'Hülle ab. Handy flach mit dem Display nach oben auf die Oberseite des Geräts legen, lange Seite in Schussrichtung, Oberkante zum Ball.'
    );
    expect(html).toContain('>Weiter</button>');
    expect(html).not.toContain('>Einmessen</button>');
    expect(render({ flow: { step: 'intro', requesting: true } })).toMatch(/disabled=""[^>]*>Weiter<\/button>/);
  });

  it('explains how to allow a denied motion sensor', () => {
    setActiveLocale('de');
    const html = render({ flow: { step: 'idle', notice: { kind: 'denied' } } });
    expect(html).toContain('Bewegungssensor nicht erlaubt');
    expect(html).toContain('Seite neu laden');
    expect(html).toContain('>Einmessen</button>');
  });

  it('says when the browser has no motion data', () => {
    setActiveLocale('de');
    expect(render({ flow: { step: 'idle', notice: { kind: 'unsupported' } } })).toContain('OpenFlight-App');
  });

  it('2: shows the live ball side higher / lower / level with the value', () => {
    setActiveLocale('de');
    const higher = render({ flow: { step: 'live', convention: 'w3c', pitchDeg: 1.26 } });
    expect(higher).toContain('Ballseite höher 1,3°');
    expect(higher).toContain('Kurz die Ballseite anheben: Hier muss „höher“ stehen.');
    expect(higher).toContain('>Stimmt</button>');
    expect(higher).toContain('>Zeigt falsch herum</button>');
    expect(render({ flow: { step: 'live', convention: 'inverted', pitchDeg: -0.44 } })).toContain(
      'Ballseite tiefer 0,4°'
    );
    expect(render({ flow: { step: 'live', convention: 'w3c', pitchDeg: 0.01 } })).toContain('Ballseite gerade 0,0°');
  });

  it('2: waits for the first live value before the buttons work', () => {
    const html = render({ flow: { step: 'live', convention: 'w3c', pitchDeg: null } });
    expect(html).toContain('Waiting for the motion sensor…');
    expect(html).toMatch(/disabled=""[^>]*>Correct<\/button>/);
    expect(html).toMatch(/disabled=""[^>]*>Shows it the wrong way round<\/button>/);
  });

  it('3: measures position 1', () => {
    setActiveLocale('de');
    const ready = render({ flow: { step: 'ready', position: 'a' } });
    expect(ready).toContain('>Position 1 messen</button>');
    expect(ready).not.toContain('180°');
    const measuring = render({ flow: { step: 'measuring', position: 'a', remainingS: 2, unstable: false } });
    expect(measuring).toContain('Position 1 wird gemessen…');
    expect(measuring).toContain('noch 2 s');
    expect(measuring).toContain('progress-indicator--inline');
    expect(measuring).not.toContain('Bitte nicht berühren');
    expect(render({ flow: { step: 'measuring', position: 'a', remainingS: 3, unstable: true } })).toContain(
      'Bitte nicht berühren'
    );
  });

  it('4: asks to turn the phone, then measures position 2', () => {
    setActiveLocale('de');
    const ready = render({ flow: { step: 'ready', position: 'b' } });
    expect(ready).toContain('Handy um 180° drehen (Oberkante zeigt jetzt weg vom Ball)');
    expect(ready).toContain('>Position 2 messen</button>');
    expect(render({ flow: { step: 'measuring', position: 'b', remainingS: 1, unstable: false } })).toContain(
      'Position 2 wird gemessen…'
    );
  });

  it('5: shows the result and that it was applied', () => {
    setActiveLocale('de');
    expect(render({ flow: { step: 'submitting' } })).toContain('Wird übernommen…');
    const html = render({
      flow: { step: 'idle', notice: { kind: 'applied', caseTiltDeg: 1.24, radarTiltDeg: 11.24 } },
    });
    expect(html).toContain('Gerät vorne 1,2° höher → Radar-Neigung 11,2°');
    expect(html).toContain('Übernommen');
  });

  it('5: shows a plain sentence for a refused measurement and "again"', () => {
    setActiveLocale('de');
    const html = render({ flow: { step: 'failed', code: 'phone_bias_out_of_range' } });
    expect(html).toContain('Die zwei Messungen passen nicht zusammen – Handy wirklich um 180° gedreht?');
    expect(html).toContain('>Nochmal</button>');
    expect(render({ flow: { step: 'failed', code: 'inclinometer_unstable' } })).toContain(
      'Der Neigungssensor im Gerät ist unruhig'
    );
    expect(render({ flow: { step: 'failed', code: 'pairing_required' } })).toContain('nicht gekoppelt');
  });

  it('hides the reset while a measurement runs', () => {
    expect(render({ flow: { step: 'ready', position: 'a' } })).not.toContain('>Reset</button>');
  });
});
