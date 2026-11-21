/* Transport: the only file that knows whether the UI talks to the real backend or to fake data.
   Jarvis.bus.on(event, fn) / Jarvis.bus.emit(event, data)
   Jarvis.transport.send(cmd, args)   UI -> backend command
   Jarvis.transport.get(path)         REST read, returns a Promise of JSON
   Jarvis.transport.isMock            true when running on sample data

   Mock mode is used when the page is opened as a file or from anywhere other than the local
   Jarvis server. Force it with ?mock, force the real backend with ?live. */
(function () {
  var J = window.Jarvis = window.Jarvis || {};
  var handlers = {};

  J.bus = {
    on: function (name, fn) { (handlers[name] = handlers[name] || []).push(fn); },
    emit: function (name, data) {
      (handlers[name] || []).forEach(function (fn) {
        try { fn(data); } catch (e) { console.error('[bus]', name, e); }
      });
    }
  };

  var q = location.search;
  var local = /^(localhost|127\.0\.0\.1)$/.test(location.hostname);
  var isMock = /[?&]mock\b/.test(q) || !(/[?&]live\b/.test(q) || (local && location.protocol === 'http:'));

  var ws = null, retry = 0;

  function connect() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.onopen = function () { retry = 0; J.bus.emit('connected', true); };
    ws.onmessage = function (m) {
      try { var p = JSON.parse(m.data); J.bus.emit(p.event, p.data); } catch (e) { console.error('[ws]', e); }
    };
    ws.onclose = function () {
      J.bus.emit('connected', false);
      setTimeout(connect, Math.min(5000, 400 * Math.pow(2, retry++)));
    };
  }

  J.transport = {
    isMock: isMock,
    start: function () {
      if (isMock) J.mock.start(); else connect();
    },
    send: function (cmd, args) {
      if (isMock) return J.mock.send(cmd, args || {});
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ cmd: cmd, args: args || {} }));
    },
    get: function (path) {
      if (isMock) return J.mock.get(path);
      return fetch(path).then(function (r) { return r.json(); });
    }
  };
})();
