/* Orb component. Builds the layers once, then only changes data-state / --level / size.
   Jarvis.orb.create(host, size, {pin}) -> element
   Jarvis.orb.setState(state)           -> every unpinned orb
   Jarvis.orb.setLevel(0..1)            -> audio level, drives ripples, bars and core
   Jarvis.orb.autoSize(host, orbEl, {min, max, pad})  -> keeps the orb sized to its container

   Motion: a small animation loop eases the spin speeds and the core pulse toward the values
   of the current state, so the orb speeds up and slows down smoothly instead of jumping.
   Colours, glows and opacities are faded by the CSS (@property + transition in orb.css). */
(function () {
  window.Jarvis = window.Jarvis || {};

  var LABELS = {
    idle: 'Jarvis is idle', listening: 'Jarvis is listening', thinking: 'Jarvis is thinking',
    speaking: 'Jarvis is speaking', paused: 'Microphone is muted', error: 'Jarvis has an error'
  };
  var MIC_OFF = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 11.5 5.3M19 11a7 7 0 0 1-.4 2.3M12 18v3M3 3l18 18"/></svg>';
  var ALERT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 6v8M12 18h.01"/></svg>';

  /* Per state: spin speeds in degrees per second, pulse in beats per second, core scale range. */
  var TARGET = {
    idle:      { outer: 360 / 70, ticks: -360 / 80, arc: 360 / 45,  pulse: 0.2,  lo: 0.78, hi: 0.84 },
    listening: { outer: 360 / 14, ticks: -360 / 16, arc: 360 / 6,   pulse: 0.83, lo: 0.9,  hi: 1.02 },
    thinking:  { outer: 360 / 12, ticks: -360 / 10, arc: 360 / 1.5, pulse: 0.91, lo: 0.86, hi: 0.96 },
    speaking:  { outer: 360 / 22, ticks: -360 / 22, arc: 360 / 9,   pulse: 1.11, lo: 0.86, hi: 1.06 },
    paused:    { outer: 0,        ticks: 0,         arc: 0,         pulse: 0,    lo: 0.82, hi: 0.82 },
    error:     { outer: 0,        ticks: 0,         arc: 0,         pulse: 0.4,  lo: 0.9,  hi: 0.9 }
  };
  var EASE_SECONDS = 0.55;   // how long a speed change takes (roughly)

  var reduceMotion = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var orbs = [];       // { el, m } where m is that orb's current motion
  var current = 'idle';
  var level = 0;

  function barRing() {   // 48 bars around the core, each with its own phase and speed
    var html = '';
    for (var i = 0; i < 48; i++) {
      var a = (i * 7.5).toFixed(1);
      var d = (-(((i * 7) % 48) / 48) * 0.9).toFixed(3);
      var t = (0.55 + ((i * 5) % 7) * 0.045).toFixed(3);
      html += '<span class="sb" style="--a:' + a + 'deg;--d:' + d + 's;--t:' + t + 's"><i></i></span>';
    }
    return html;
  }

  function setSize(el, size) {
    el.style.setProperty('--size', size + 'px');
    el.style.setProperty('--k', (size / 200).toFixed(4));
  }

  function create(host, size, opts) {
    var el = document.createElement('div');
    el.className = 'orb';
    el.setAttribute('role', 'img');
    var state = (opts && opts.pin) || current;
    el.dataset.state = state;
    if (opts && opts.pin) el.dataset.pinned = '1';
    el.setAttribute('aria-label', LABELS[state]);
    el.innerHTML =
      '<div class="orb-stage">' +
        '<div class="o-halo"></div>' +
        '<div class="o-outer"></div>' +
        '<div class="o-ticks"></div>' +
        '<div class="o-arc"></div>' +
        '<div class="o-inner"></div>' +
        '<div class="o-sweep"></div>' +
        '<div class="o-orbit"><i></i></div>' +
        '<div class="o-ripples in"><i></i><i></i><i></i></div>' +
        '<div class="o-ripples out"><i></i><i></i><i></i></div>' +
        '<div class="o-bars">' + barRing() + '</div>' +
        '<div class="o-core"></div>' +
        '<div class="o-icon o-mic">' + MIC_OFF + '</div>' +
        '<div class="o-icon o-alert">' + ALERT + '</div>' +
      '</div>';
    setSize(el, size || 200);
    host.appendChild(el);
    var t = TARGET[state];
    orbs.push({ el: el, m: {
      outer: t.outer, ticks: t.ticks, arc: t.arc, pulse: t.pulse, lo: t.lo, hi: t.hi,
      a1: 0, a2: 0, a3: 0, ph: 0, lvl: 0
    } });
    return el;
  }

  function setState(state) {
    if (!LABELS[state]) return;
    current = state;
    orbs.forEach(function (o) {
      if (o.el.dataset.pinned) return;
      o.el.dataset.state = state;
      o.el.setAttribute('aria-label', LABELS[state]);
    });
  }

  function setLevel(v) {
    level = Math.max(0, Math.min(1, v || 0));
    var s = level.toFixed(3);
    orbs.forEach(function (o) { o.el.style.setProperty('--level', s); });
  }

  /* ---- motion loop ---- */
  var last = 0;
  function frame(now) {
    var dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    var k = 1 - Math.exp(-dt / EASE_SECONDS);
    for (var i = 0; i < orbs.length; i++) {
      var o = orbs[i], m = o.m;
      if (!o.el.offsetParent) continue;                       // orb is in a hidden view
      var tg = TARGET[o.el.dataset.state] || TARGET.idle;
      for (var key in tg) m[key] += (tg[key] - m[key]) * k;   // ease toward this state
      if (!reduceMotion) {
        m.a1 = (m.a1 + m.outer * dt) % 360;
        m.a2 = (m.a2 + m.ticks * dt) % 360;
        m.a3 = (m.a3 + m.arc * dt) % 360;
      }
      m.ph += m.pulse * dt * 6.28318;
      m.lvl += (level - m.lvl) * Math.min(1, dt * 10);
      var p = 0.5 - 0.5 * Math.cos(m.ph);                     // 0..1 pulse
      var s = m.lo + (m.hi + m.lvl * 0.12 - m.lo) * p;
      var st = o.el.style;
      st.setProperty('--rot-outer', m.a1.toFixed(2) + 'deg');
      st.setProperty('--rot-ticks', m.a2.toFixed(2) + 'deg');
      st.setProperty('--rot-arc', m.a3.toFixed(2) + 'deg');
      st.setProperty('--core-s', s.toFixed(4));
      st.setProperty('--halo-s', (1 + 0.1 * p).toFixed(4));
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  function autoSize(host, el, o) {
    o = o || {};
    var min = o.min || 90, max = o.max || 300, pad = o.pad == null ? 40 : o.pad;
    function apply() {
      var w = host.clientWidth, h = host.clientHeight;
      if (!w || !h) return;
      setSize(el, Math.round(Math.max(min, Math.min(max, Math.min(w, h) - pad))));
    }
    if (window.ResizeObserver) new ResizeObserver(apply).observe(host);
    window.addEventListener('resize', apply);
    apply();
  }

  window.Jarvis.orb = { create: create, setState: setState, setLevel: setLevel, setSize: setSize, autoSize: autoSize };
})();