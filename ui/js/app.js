/* App wiring. STEP 1: orb + state buttons + auto demo. Chat bubbles, mute, settings etc. come in later steps. */
(function () {
  var J = window.Jarvis;
  var $ = function (s) { return document.querySelector(s); };
  var app = $('#app');
  var preview = J.transport.isMock;
  if (preview) document.documentElement.classList.add('preview');

  /* ---- orb ---- */
  var chatOrb = J.orb.create($('#orb-chat'), 240);
  J.orb.autoSize($('#orb-chat'), chatOrb, { pad: 20, max: 320 });

  J.bus.on('state', function (e) { J.orb.setState(e.state); markState(e.state); });
  J.bus.on('level', function (e) { J.orb.setLevel(e.v); });

  /* ---- preview only: fixed window size, scaled to fit the browser ---- */
  function fit() {
    if (!preview) return;
    var compact = app.dataset.mode === 'compact';
    var quit = app.dataset.view === 'quit';
    var w = quit ? 400 : compact ? 300 : 1040, h = quit ? 360 : compact ? 350 : 700;
    app.style.width = w + 'px'; app.style.height = h + 'px';
    var s = Math.min(1, (window.innerWidth - 32) / w, (window.innerHeight - 100) / h);
    app.style.transform = 'scale(' + s.toFixed(4) + ')';
  }
  window.addEventListener('resize', fit);
  fit();

  /* ---- preview toolbar (state buttons + auto demo) ---- */
  function markState(s) {
    document.querySelectorAll('[data-dev-state]').forEach(function (b) {
      b.classList.toggle('on', b.dataset.devState === s);
    });
  }
  document.querySelectorAll('[data-dev-state]').forEach(function (b) {
    b.addEventListener('click', function () { J.mock.force(b.dataset.devState); setDemoBtn(false); });
  });
  var demoBtn = $('#dev-demo');
  function setDemoBtn(on) {
    J.mock.setDemo(on);
    demoBtn.classList.toggle('on', on);
    demoBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
  demoBtn.addEventListener('click', function () { setDemoBtn(!J.mock.isDemo()); });


  /* ---- STEP 2: chat bubbles, typing, stop, mute ---- */
  var tr = $('#transcript'), cur = null, muted = false;

  /* keep the chat pinned to the newest message unless the user scrolled up */
  var stick = true;
  tr.addEventListener('scroll', function () {
    stick = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 40;
  });
  J.bus.on('user_said', function () { stick = true; });

  function trim() {
    if (stick) requestAnimationFrame(function () { tr.scrollTop = tr.scrollHeight; });
  }

  function addBubble(role, text) {
    var b = document.createElement('div');
    b.className = 'bubble ' + role;
    b.textContent = text;
    tr.appendChild(b);
    trim();
    return b;
  }

  J.bus.on('user_said', function (e) { cur = null; addBubble('user', e.text); });
  J.bus.on('assistant_text', function (e) {
    if (!cur) cur = addBubble('jarvis', '');
    cur.textContent = e.text;
    if (e.done) cur = null;
    else { var c = document.createElement('span'); c.className = 'cursor'; cur.appendChild(c); }
    trim();
  });

  $('#form-send').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var inp = $('#input-text'), text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    J.transport.send('send_text', { text: text });
  });
  $('#btn-stop').addEventListener('click', function () { J.transport.send('stop'); });

  document.querySelectorAll('.js-mute').forEach(function (b) {
    b.addEventListener('click', function () { J.transport.send('mute', { on: !muted }); });
  });
  J.bus.on('muted', function (e) {
    muted = e.on;
    document.querySelectorAll('.js-mute').forEach(function (b) {
      b.setAttribute('aria-pressed', muted ? 'true' : 'false');
      b.setAttribute('aria-label', muted ? 'Unmute microphone' : 'Mute microphone');
      b.title = muted ? 'Unmute microphone' : 'Mute microphone';
    });
  });


  /* ---- new conversation button ---- */
  var newChatBtn = $('#btn-new-chat'), resetting = false;

  function setResetting(on) {
    resetting = on;
    newChatBtn.disabled = on;
    newChatBtn.classList.toggle('busy', on);
  }

  newChatBtn.addEventListener('click', function () {
    if (resetting) return;
    setResetting(true);
    J.transport.send('new_conversation');
  });

  J.bus.on('conversation_reset', function () {
    tr.innerHTML = '';
    cur = null;
    stick = true;
    reply.firstElementChild.textContent = 'Say something to Jarvis.';
    reply.classList.remove('dim');
    setResetting(false);
  });

  J.bus.on('conversation_busy', function () { setResetting(false); });


  /* ---- STEP 3: compact mode ---- */
  var compactHost = $('#orb-compact'), reply = $('#compact-reply');
  compactHost.style.margin = '2px 0 0';
  J.orb.create(compactHost, 190);

  function setMode(m) {
    app.dataset.mode = m;
    fit();
    document.querySelectorAll('[data-dev-mode]').forEach(function (b) {
      b.classList.toggle('on', b.dataset.devMode === m);
    });
    if (window.pywebview && window.pywebview.api) window.pywebview.api.set_mode(m);
  }
  $('#btn-compact').addEventListener('click', function () { setMode('compact'); });
  $('#btn-expand').addEventListener('click', function () { setMode('expanded'); });
  document.querySelectorAll('[data-dev-mode]').forEach(function (b) {
    b.addEventListener('click', function () { setMode(b.dataset.devMode); });
  });
  setMode('expanded');

  J.bus.on('user_said', function () { reply.classList.add('dim'); });
  J.bus.on('assistant_text', function (e) {
    reply.classList.remove('dim');
    reply.firstElementChild.textContent = e.text;
  });


  /* ---- STEP 4: settings view ---- */
  J.settings.init();
  J.orb.create($('#orb-settings'), 64);

  function setView(v) {
    app.dataset.view = v;
    $('#rail-chat').classList.toggle('on', v === 'chat');
    $('#rail-settings').classList.toggle('on', v === 'settings');
    if (v === 'settings') J.settings.show();
  }
  $('#rail-chat').addEventListener('click', function () { setView('chat'); });
  $('#rail-settings').addEventListener('click', function () { setView('settings'); });
  document.querySelectorAll('[data-dev-view]').forEach(function (b) {
    b.addEventListener('click', function () { setMode('expanded'); setView(b.dataset.devView); });
  });


  /* ---- STEP 7: live settings ---- */
  function applySetting(k, v) {
    if (k === 'accent') document.documentElement.style.setProperty('--accent', v);
  }
  J.bus.on('settings_changed', function (e) { applySetting(e.key, e.value); });
  J.transport.get('/api/settings').then(function (st) {
    applySetting('accent', st.accent);
    if (st.start_in_compact) {
      var go = function () { setMode('compact'); };
      if (window.pywebview && window.pywebview.api) go();
      else window.addEventListener('pywebviewready', go);   // API not injected yet
    }
  });



  /* ---- STEP 8: quit screen ("Saving your memories") ---- */
  var STEPS = ['saving', 'extracting', 'summarizing'], quitTimer = null;
  var quitNow = $('#btn-quit-now');
  $('#orb-quit').style.margin = '6px 0 0';
  J.orb.create($('#orb-quit'), 110, { pin: 'thinking' });

  function resetQuit() {
    clearTimeout(quitTimer);
    $('#quit-title').textContent = 'SAVING YOUR MEMORIES';
    $('#quit-sub').textContent = 'Jarvis will close as soon as this finishes.';
    document.querySelectorAll('#quit-steps li').forEach(function (li) { li.className = ''; });
    $('#quit-bar').style.width = '0';
    quitNow.hidden = true;
    setView('chat');
    fit();
  }

  J.bus.on('shutdown_progress', function (e) {
    if (app.dataset.view !== 'quit') {
      app.dataset.view = 'quit';
      fit();
      quitNow.hidden = true;
      quitTimer = setTimeout(function () { quitNow.hidden = false; }, preview ? 8000 : 20000);
    }
    var idx = STEPS.indexOf(e.step);
    document.querySelectorAll('#quit-steps li').forEach(function (li, i) {
      li.className = (e.done || i < idx) ? 'done' : (i === idx ? 'active' : '');
    });
    $('#quit-bar').style.width = e.pct + '%';
    $('.progress').setAttribute('aria-valuenow', e.pct);
    if (e.done) {
      clearTimeout(quitTimer);
      quitNow.hidden = true;
      $('#quit-title').textContent = 'ALL SAVED';
      $('#quit-sub').textContent = preview ? 'In the real app the window closes now. Returning to the preview...' : 'Closing...';
      if (preview) setTimeout(resetQuit, 2500);
    }
  });
  quitNow.addEventListener('click', function () { J.transport.send('skip_shutdown'); });
  $('#dev-quit').addEventListener('click', function () { setMode('expanded'); setDemoBtn(false); J.transport.send('quit'); });

  document.querySelectorAll('.win-min').forEach(function (b) {
    b.addEventListener('click', function () {
      if (window.pywebview && window.pywebview.api) window.pywebview.api.minimize();
    });
  });
  document.querySelectorAll('.win-close').forEach(function (b) {
    b.addEventListener('click', function () { J.transport.send('quit'); });
  });

  J.transport.start();
})();
