import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installFakeBrowser, installFakeSpeech } from '../test-utils/fakeBrowser';
import { fakeNative, fakeNativeSpeech } from '../test-utils/nativeBridge';

describe('speech', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('reports no support and does nothing without the Web Speech API', async () => {
    vi.stubGlobal('window', {});
    const speech = await import('./speech');

    expect(speech.isSpeechSupported()).toBe(false);
    expect(() => {
      speech.primeSpeech();
      speech.speak('Carry 150 meters', 'en-US');
      speech.speak('Carry 150 meters', 'en-US', { interrupt: true });
      speech.cancelSpeech();
    }).not.toThrow();
  });

  it('reports no support outside a browser', async () => {
    const speech = await import('./speech');

    expect(speech.isSpeechSupported()).toBe(false);
  });

  it('speaks with the language only, then resumes a queue the OS may have paused', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');

    expect(speech.isSpeechSupported()).toBe(true);
    speech.speak('Carry 151 Meter', 'de-DE');

    expect(synth.spoken).toHaveLength(1);
    expect(synth.spoken[0]).toMatchObject({ text: 'Carry 151 Meter', lang: 'de-DE' });
    expect(synth.spoken[0]).not.toHaveProperty('voice');
    expect(synth.log).toEqual(['speak:Carry 151 Meter', 'resume']);
  });

  it('queues a callout behind one that is still speaking', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');
    synth.speaking = true;

    speech.speak('Carry 160 meters', 'en-US');

    expect(synth.log).toEqual(['speak:Carry 160 meters', 'resume']);
  });

  it('interrupts what is speaking and speaks after the queue settles', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');
    synth.speaking = true;

    speech.speak('Carry 160 meters', 'en-US', { interrupt: true });

    expect(synth.log).toEqual(['cancel']);
    vi.advanceTimersByTime(speech.CANCEL_SETTLE_MS);
    expect(synth.log).toEqual(['cancel', 'speak:Carry 160 meters', 'resume']);
  });

  it('interrupts at once when nothing is speaking', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');

    speech.speak('Carry 160 meters', 'en-US', { interrupt: true });

    expect(synth.log).toEqual(['speak:Carry 160 meters', 'resume']);
  });

  it('keeps only the newest of two interrupting callouts', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');
    synth.speaking = true;

    speech.speak('Carry 160 meters', 'en-US', { interrupt: true });
    synth.speaking = true;
    speech.speak('Carry 170 meters', 'en-US', { interrupt: true });
    vi.advanceTimersByTime(speech.CANCEL_SETTLE_MS);

    expect(synth.spoken.map((utterance) => utterance.text)).toEqual(['Carry 170 meters']);
  });

  it('cancel drops an interrupting callout that waits for the queue to settle', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');
    synth.pending = true;

    speech.speak('Carry 160 meters', 'en-US', { interrupt: true });
    speech.cancelSpeech();
    vi.advanceTimersByTime(speech.CANCEL_SETTLE_MS);

    expect(synth.spoken).toHaveLength(0);
    expect(synth.log).toEqual(['cancel', 'cancel']);
  });

  it('primes with a silent utterance and retries on every gesture until the engine confirms it', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');

    speech.primeSpeech();
    speech.primeSpeech();
    expect(synth.spoken.map((utterance) => utterance.volume)).toEqual([0, 0]);

    synth.spoken[1].onend?.();
    speech.primeSpeech();
    expect(synth.spoken).toHaveLength(2);
  });

  it('counts a started prime utterance as confirmed', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');

    speech.primeSpeech();
    synth.spoken[0].onstart?.();
    speech.primeSpeech();

    expect(synth.spoken).toHaveLength(1);
  });

  it('tells whether priming took, once, to every listener', async () => {
    const synth = installFakeSpeech();
    const speech = await import('./speech');
    const heard = vi.fn();
    const unsubscribe = speech.subscribeSpeechPrimed(heard);

    expect(speech.isSpeechPrimed()).toBe(false);
    speech.primeSpeech();
    expect(speech.isSpeechPrimed(), 'not before the engine confirms').toBe(false);
    synth.spoken[0].onstart?.();
    synth.spoken[0].onend?.();

    expect(speech.isSpeechPrimed()).toBe(true);
    expect(heard).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});

describe('speech inside the iOS companion', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function companion() {
    const nativeSpeech = fakeNativeSpeech();
    const { synth } = installFakeBrowser({
      phone: true,
      native: fakeNative({ capabilities: { speech: true }, speech: nativeSpeech }),
    });
    return { synth, nativeSpeech };
  }

  it('speaks natively, never through Web Speech', async () => {
    const { synth, nativeSpeech } = companion();
    const speech = await import('./speech');

    speech.speak('Carry 151 Meter', 'de-DE');
    synth.speaking = true;
    speech.speak('Carry 160 Meter', 'de-DE', { interrupt: true });
    vi.advanceTimersByTime(speech.CANCEL_SETTLE_MS);
    speech.cancelSpeech();

    expect(nativeSpeech.speak.mock.calls).toEqual([
      [{ text: 'Carry 151 Meter', lang: 'de-DE', interrupt: false }],
      [{ text: 'Carry 160 Meter', lang: 'de-DE', interrupt: true }],
    ]);
    expect(nativeSpeech.cancel).toHaveBeenCalledTimes(1);
    expect(synth.log).toEqual([]);
  });

  it('needs no priming tap', async () => {
    const { synth } = companion();
    const speech = await import('./speech');

    expect(speech.isSpeechSupported()).toBe(true);
    expect(speech.isSpeechPrimed()).toBe(true);
    speech.primeSpeech();
    expect(synth.spoken).toEqual([]);
  });

  it('works without Web Speech in the web view', async () => {
    const nativeSpeech = fakeNativeSpeech();
    installFakeBrowser({
      phone: true,
      speech: false,
      native: fakeNative({ capabilities: { speech: true }, speech: nativeSpeech }),
    });
    const speech = await import('./speech');

    expect(speech.isSpeechSupported()).toBe(true);
    speech.speak('Carry 151 Meter', 'de-DE');
    expect(nativeSpeech.speak).toHaveBeenCalledTimes(1);
  });

  it('skips a callout the app could not speak, without an unhandled rejection', async () => {
    const nativeSpeech = fakeNativeSpeech();
    nativeSpeech.speak.mockRejectedValue(new Error('busy'));
    nativeSpeech.cancel.mockRejectedValue(new Error('gone'));
    installFakeBrowser({ phone: true, native: fakeNative({ capabilities: { speech: true }, speech: nativeSpeech }) });
    const speech = await import('./speech');

    speech.speak('Carry 151 Meter', 'de-DE');
    speech.cancelSpeech();
    await vi.runAllTimersAsync();

    expect(nativeSpeech.speak).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['an app build from before native speech', {}],
    ['a bridge that says speech but lacks it', { capabilities: { speech: true } }],
  ])('falls back to Web Speech in %s', async (_name, overrides) => {
    const { synth } = installFakeBrowser({ phone: true, native: fakeNative(overrides) });
    const speech = await import('./speech');

    speech.speak('Carry 151 Meter', 'de-DE');

    expect(synth.log).toEqual(['speak:Carry 151 Meter', 'resume']);
    expect(speech.isSpeechPrimed()).toBe(false);
  });
});
