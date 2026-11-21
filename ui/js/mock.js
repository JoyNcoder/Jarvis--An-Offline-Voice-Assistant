/* Fake backend for previewing the UI. Everything here is SAMPLE DATA, not real conversations.
   Emits the same events the real server will (see JARVIS_UI_PLAN.md, event contract). */
(function () {
  var J = window.Jarvis, bus = J.bus;

  var S = { state: 'idle', muted: false, demo: true, busy: false, timers: [], idleSince: Date.now(), turn: 0, text: '' };

  var settings = {
    voice: 'af_heart', speech_speed: 1.0, mic_device: 'Default microphone',
    speech_threshold: 0.5, silence_threshold: 0.8, interrupt_speech_threshold: 0.7,
    model_name: 'qwen3:8b', reply_length: 'short', max_history_messages: 12,
    accent: '#22d3ee', compact_always_on_top: true, start_in_compact: false
  };

  var SCRIPT = [
    { u: 'Open Notepad.', r: 'system', a: 'Opening Notepad now.' },
    { u: 'What do you remember about my project?', r: 'memory', a: 'You are building an offline voice assistant called Jarvis, and you wanted a proper interface for it.' },
    { u: 'Turn the volume down to forty percent.', r: 'system', a: 'Volume set to forty percent.' },
    { u: 'Tell me something interesting about octopuses.', r: 'llm', a: 'An octopus has three hearts. Two push blood through the gills, and the third feeds the rest of the body.' },
    { u: 'What time is it?', r: 'tools', a: 'It is a quarter past nine in the evening.' }
  ];
  var CHAT_REPLIES = [
    'Understood. I will keep that in mind.',
    'Sure. Give me a moment and I will take care of it.',
    'Done. Anything else?'
  ];

  var convs = [
    {
      id: 1, when: 'TODAY 21:04', title: 'Opened Notepad, volume, octopus facts', tags: '2 facts saved', messages: [
        { role: 'user', text: 'Open Notepad.' }, { role: 'jarvis', text: 'Opening Notepad now.' },
        { role: 'user', text: 'Tell me something interesting about octopuses.' },
        { role: 'jarvis', text: 'An octopus has three hearts. Two push blood through the gills, and the third feeds the rest of the body.' }]
    },
    {
      id: 2, when: 'YESTERDAY 18:30', title: 'Planning the interface for Jarvis', tags: '4 facts saved', messages: [
        { role: 'user', text: 'I want a futuristic interface for you.' },
        { role: 'jarvis', text: 'A web interface in a native window would suit that well.' }]
    },
    {
      id: 3, when: 'MON 09:12', title: 'Morning check-in', tags: 'no new facts', messages: [
        { role: 'user', text: 'Good morning.' }, { role: 'jarvis', text: 'Good morning. Ready when you are.' }]
    },
    {
      id: 4, when: 'SUN 22:47', title: 'Voice settings and speech speed', tags: '1 fact saved', messages: [
        { role: 'user', text: 'Use a calmer voice.' }, { role: 'jarvis', text: 'Switched to a calmer voice.' }]
    },
    {
      id: 5, when: 'SAT 14:05', title: 'Quick questions', tags: 'no new facts', messages: [
        { role: 'user', text: 'How many days until the weekend?' }, { role: 'jarvis', text: 'It is already the weekend.' }]
    }
  ];

  (function () {   // one long sample conversation, to try the scrolling
    var m = [];
    for (var i = 1; i <= 14; i++) {
      m.push({ role: 'user', text: 'Sample question number ' + i + ' in a long conversation.' });
      m.push({ role: 'jarvis', text: 'Sample answer number ' + i + '. This one is a little longer so that the line wraps and the pane has something to scroll through.' });
    }
    convs.splice(1, 0, { id: 6, when: 'FRI 20:15', title: 'A long conversation (sample)', tags: '5 facts saved', messages: m });
  })();

  var facts = [
    { id: 1, key: 'project.name', value: 'Offline voice assistant called Jarvis', n: 6, conf: 0.95, status: 'active' },
    { id: 2, key: 'project.stack', value: 'Python, faster-whisper, Kokoro, Ollama', n: 4, conf: 0.9, status: 'active' },
    { id: 3, key: 'preference.voice', value: 'Calm, not too fast', n: 2, conf: 0.8, status: 'active' },
    { id: 4, key: 'preference.reply_length', value: 'Short replies', n: 3, conf: 0.85, status: 'active' },
    { id: 5, key: 'ui.style', value: 'Cyan HUD orb on frosted glass', n: 2, conf: 0.9, status: 'active' },
    { id: 6, key: 'habit.evening', value: 'Usually works late in the evening', n: 1, conf: 0.55, status: 'active' },
    { id: 7, key: 'sample.old_fact', value: 'An outdated example fact', n: 1, conf: 0.4, status: 'forgotten' }
  ];

  var logs = [
    ['21:04:12', 'Open Notepad.', 'system', 310], ['21:04:40', 'Tell me something interesting about octopuses.', 'llm', 1840],
    ['21:05:22', 'Turn the volume down to forty percent.', 'system', 280], ['21:06:01', 'What do you remember about my project?', 'memory', 920],
    ['21:06:48', 'What time is it?', 'tools', 240], ['21:07:30', 'Stop.', 'stop', 90],
    ['21:08:15', 'Use a calmer voice.', 'voice', 410], ['21:09:02', 'Goodbye.', 'goodbye', 150]
  ].map(function (r) { return { t: r[0], text: r[1], route: r[2], ms: r[3] }; });

  function later(ms, fn) { var id = setTimeout(function () { S.timers = S.timers.filter(function (x) { return x !== id; }); fn(); }, ms); S.timers.push(id); }
  function cancel() { S.timers.forEach(clearTimeout); S.timers = []; }
  function setState(s) { S.state = s; S.idleSince = Date.now(); bus.emit('state', { state: s }); }
  function settle() { S.busy = false; setState(S.muted ? 'paused' : 'idle'); }

  function runTurn(user, route, reply, skipListen) {
    cancel(); S.busy = true;
    var t = 0;
    if (!skipListen) { setState('listening'); t = 1900; }
    later(t, function () {
      bus.emit('user_said', { text: user });
      later(250, function () {
        setState('thinking'); bus.emit('route', { route: route });
        later(1300, function () {
          setState('speaking');
          var words = reply.split(' '), i = 0;
          (function step() {
            i++; S.text = words.slice(0, i).join(' ');
            bus.emit('assistant_text', { text: S.text, done: i >= words.length });
            if (i < words.length) later(150, step); else later(500, function () { bus.emit('latency', { ms: 1400 }); settle(); });
          })();
        });
      });
    });
  }

  function stopSpeaking() {
    if (S.state !== 'speaking' && S.state !== 'thinking') return;
    cancel();
    if (S.text) bus.emit('assistant_text', { text: S.text, done: true });
    settle();
  }

  function demoTick() {
    if (!S.demo || S.busy || S.muted || S.state === 'error') return;
    if (Date.now() - S.idleSince < 2600) return;
    var s = SCRIPT[S.turn++ % SCRIPT.length];
    runTurn(s.u, s.r, s.a);
  }

  function levelTick() {
    var v = 0;
    if (S.state === 'listening') v = .2 + Math.random() * .7;
    else if (S.state === 'speaking') v = .35 + Math.random() * .6;
    bus.emit('level', { v: v });
  }

  function quitFlow() {
    cancel(); S.demo = false;
    bus.emit('shutdown_progress', { step: 'saving', pct: 8 });
    later(1200, function () {
      bus.emit('shutdown_progress', { step: 'extracting', pct: 38 });
      later(3500, function () {
        bus.emit('shutdown_progress', { step: 'summarizing', pct: 72 });
        later(11000, function () { bus.emit('shutdown_progress', { step: 'done', pct: 100, done: true }); });
      });
    });
  }

  var api = {
    start: function () {
      setInterval(demoTick, 800);
      setInterval(levelTick, 90);
      bus.emit('state', { state: 'idle' });
    },
    setDemo: function (on) { S.demo = on; },
    isDemo: function () { return S.demo; },
    force: function (state) {
      cancel(); S.busy = false; S.demo = false;
      if (state === 'paused') { S.muted = true; bus.emit('muted', { on: true }); }
      else if (S.muted) { S.muted = false; bus.emit('muted', { on: false }); }
      setState(state);
      if (state === 'error') bus.emit('error', { message: 'Speech engine stopped responding.' });
    },
    quit: quitFlow,
    send: function (cmd, a) {
      if (cmd === 'mute') {
        S.muted = !!a.on; bus.emit('muted', { on: S.muted });
        if (S.muted) { if (!S.busy) setState('paused'); } else if (!S.busy) setState('idle');
      } else if (cmd === 'stop') stopSpeaking();
      else if (cmd === 'send_text') runTurn(a.text, 'llm', CHAT_REPLIES[(S.turn++) % CHAT_REPLIES.length], true);
      else if (cmd === 'set_setting') { settings[a.key] = a.value; bus.emit('settings_changed', { key: a.key, value: a.value }); }
      else if (cmd === 'forget_fact' || cmd === 'restore_fact') {
        facts.forEach(function (f) { if (f.id === a.id) f.status = cmd === 'forget_fact' ? 'forgotten' : 'active'; });
        bus.emit('facts_changed', {});
      } else if (cmd === 'edit_fact') {
        facts.forEach(function (f) { if (f.id === a.id) f.value = a.value; });
        bus.emit('facts_changed', {});
      } else if (cmd === 'quit') quitFlow();
      else if (cmd === 'skip_shutdown') { cancel(); bus.emit('shutdown_progress', { step: 'done', pct: 100, done: true }); }
      else if (cmd === 'delete_conversation') {
        convs = convs.filter(function (c) { return c.id !== a.id; });
        bus.emit('conversations_changed', {});
      }
    },
    get: function (path) {
      var u = path.split('?'), p = u[0], qs = new URLSearchParams(u[1] || '');
      var out;
      if (p === '/api/conversations') {
        var q = (qs.get('q') || '').toLowerCase();
        out = convs.filter(function (c) {
          return !q || c.title.toLowerCase().indexOf(q) > -1 ||
            c.messages.some(function (m) { return m.text.toLowerCase().indexOf(q) > -1; });
        }).map(function (c) { return { id: c.id, when: c.when, title: c.title, tags: c.tags }; });
      } else if (/^\/api\/conversations\/\d+$/.test(p)) {
        var id = +p.split('/').pop();
        out = convs.filter(function (c) { return c.id === id; })[0] || null;
      } else if (p === '/api/facts') out = facts.slice();
      else if (p === '/api/log') out = logs.slice();
      else if (p === '/api/voices') out = [
        { id: 'af_heart', label: 'Heart (American, female)', group: 'American' },
        { id: 'am_adam', label: 'Adam (American, male)', group: 'American' },
        { id: 'bf_emma', label: 'Emma (British, female)', group: 'British' }
      ];
      else if (p === '/api/mics') out = ['Default microphone', 'USB Microphone', 'Headset (Bluetooth)'];
      else if (p === '/api/settings') out = Object.assign({}, settings);
      else if (p === '/api/system') out = {
        items: [
          { k: 'Jarvis', v: 'Version 0.1 (interface preview)' },
          { k: 'Language model', v: settings.model_name, sub: 'running through Ollama' },
          { k: 'Voice', v: 'Kokoro, ' + settings.voice },
          { k: 'Data', v: 'Sample data', sub: 'Shown only in this preview. Nothing here is a real conversation.' }
        ],
        log: '21:03:58  startup complete\n21:04:12  route=system  310 ms\n21:04:40  route=llm  1840 ms\n21:09:02  goodbye  session saved'
      };
      return Promise.resolve(JSON.parse(JSON.stringify(out)));
    }
  };

  J.mock = api;
})();
