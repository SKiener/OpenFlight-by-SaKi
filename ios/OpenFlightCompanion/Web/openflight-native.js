// window.openflightNative: the OpenFlight iOS companion's bridge (docs/ios-companion/design.md §7).
// Injected at document start into the page's own content world, before any page script runs,
// so the web UI can feature-detect with `typeof window.openflightNative !== 'undefined'`.
// The native side templates __OPENFLIGHT_NATIVE_CONFIG__ with a JSON object.
(function () {
  'use strict';
  if (typeof window.openflightNative !== 'undefined') return;

  var CONFIG = __OPENFLIGHT_NATIVE_CONFIG__;
  var handlers = window.webkit && window.webkit.messageHandlers;
  var handler = handlers && handlers[CONFIG.handler];

  function bridgeError(code, message) {
    var error = new Error(message || code);
    error.name = 'OpenFlightNativeError';
    error.code = code;
    return error;
  }

  // Every reply is an envelope: { ok: true, result } or { ok: false, error: { code, message } }.
  function call(cmd, args) {
    if (!handler || typeof handler.postMessage !== 'function') {
      return Promise.reject(bridgeError('unavailable', 'native bridge missing'));
    }
    var body = { v: CONFIG.version, cmd: cmd, args: args === undefined || args === null ? {} : args };
    return Promise.resolve(handler.postMessage(body)).then(function (envelope) {
      if (envelope && envelope.ok === true) return envelope.result;
      var error = (envelope && envelope.error) || {};
      throw bridgeError(error.code || 'internal', error.message || 'malformed native reply');
    });
  }

  var api = {
    version: CONFIG.version,
    app_version: CONFIG.app_version,
    platform: CONFIG.platform,
    capabilities: Object.freeze(CONFIG.capabilities),
    getSensors: function (opts) { return call('getSensors', opts); },
    fetchWeather: function (req) { return call('fetchWeather', req); },
    setKeepAwake: function (on) { return call('setKeepAwake', { on: on }); },
    audio: Object.freeze({
      start: function (opts) { return call('audio.start', opts); },
      stop: function () { return call('audio.stop'); },
      status: function () { return call('audio.status'); },
      onsets: function (req) { return call('audio.onsets', req); },
      exportCaptures: function () { return call('audio.exportCaptures'); }
    }),
    motion: Object.freeze({
      start: function () { return call('motion.start'); },
      samples: function (req) { return call('motion.samples', req); },
      stop: function () { return call('motion.stop'); }
    }),
    speech: Object.freeze({
      speak: function (req) { return call('speech.speak', req); },
      cancel: function () { return call('speech.cancel'); }
    })
  };

  Object.defineProperty(window, 'openflightNative', {
    value: Object.freeze(api),
    writable: false,
    configurable: false,
    enumerable: true
  });
})();
