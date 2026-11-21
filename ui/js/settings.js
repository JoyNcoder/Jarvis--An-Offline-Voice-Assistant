/* Settings view. STEP 4: tabs + Conversations tab. Other tabs are filled in later steps. */
(function () {
  var J = window.Jarvis = window.Jarvis || {};
  var T = J.transport;

  var TABS = [['help', 'HELP'], ['conversations', 'CONVERSATIONS'], ['memory', 'MEMORY'], ['log', 'ACTIVITY LOG'],
  ['prefs', 'PREFERENCES'], ['system', 'SYSTEM']];
  var tab = 'help', tabsEl, body;
  var S = { settings: {}, convQ: '', convId: null, factStatus: 'active', factQ: '', editing: null, facts: [] };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* "TODAY 21:04" -> "TODAY 9:04 PM" (also handles 21:04:12 and full dates) */
  function to12(s) {
    return String(s == null ? '' : s).replace(/\b([01]?\d|2[0-3]):([0-5]\d)(:[0-5]\d)?\b/g, function (_, h, m, sec) {
      h = +h;
      var ap = h >= 12 ? 'PM' : 'AM';
      h = h % 12 || 12;
      return h + ':' + m + (sec || '') + ' ' + ap;
    });
  }

  function init() {
    tabsEl = document.getElementById('tabs');
    body = document.getElementById('set-body');
    tabsEl.innerHTML = TABS.map(function (t) {
      return '<button class="tab" role="tab" data-tab="' + t[0] + '">' + t[1] + '</button>';
    }).join('');
    tabsEl.addEventListener('click', function (e) {
      var b = e.target.closest('[data-tab]');
      if (b) { tab = b.dataset.tab; render(); }
    });
    body.addEventListener('click', function (e) {
      var open = e.target.closest('.dd');
      document.querySelectorAll('.dd-list').forEach(function (l) {
        if (!open || l.parentNode !== open) l.hidden = true;     // close the others
      });
      if (open) {
        var list = open.querySelector('.dd-list');
        var opt = e.target.closest('.dd-opt');
        if (opt) {
          setPref(open.dataset.dd, opt.dataset.id);
          pickDD(open.dataset.dd, opt.dataset.id);
          list.hidden = true;
        } else if (e.target.closest('.dd-btn')) {
          list.hidden = !list.hidden;
          var on = list.querySelector('.on');
          if (!list.hidden && on) list.scrollTop = on.offsetTop - 60;
        }
      }
      var c = e.target.closest('[data-conv]');
      if (c) { S.convId = +c.dataset.conv; loadConvs(); }
      var ch = e.target.closest('[data-fstatus]');
      if (ch) { S.factStatus = ch.dataset.fstatus; S.editing = null; memoryView(); }
      var a = e.target.closest('[data-act]');
      if (a) factAction(a.dataset.act, +a.dataset.id);
      var sg = e.target.closest('[data-val]');
      if (sg) { setPref(sg.dataset.key, sg.dataset.val); prefsView(); }
      if (e.target.closest('#btn-preview')) T.send('preview_voice', { voice: S.settings.voice });
      var dc = e.target.closest('[data-delconv]');
      if (dc) {
        if (dc.dataset.armed) {
          T.send('delete_conversation', { id: +dc.dataset.delconv });
          S.convId = null;
        } else {
          dc.dataset.armed = '1';
          dc.textContent = 'Click again to delete';
          dc.classList.add('accent');
          setTimeout(function () {
            if (!dc.isConnected) return;
            delete dc.dataset.armed;
            dc.textContent = 'Delete';
            dc.classList.remove('accent');
          }, 3000);
        }
      }
    });
    body.addEventListener('change', function (e) {
      var k = e.target.dataset && e.target.dataset.key;
      if (!k) return;
      var t = e.target;
      setPref(k, t.type === 'checkbox' ? t.checked : t.type === 'range' ? +t.value : t.value);
    });
    body.addEventListener('input', function (e) {
      if (e.target.id === 'conv-q') { S.convQ = e.target.value; S.convId = null; loadConvs(); }
      if (e.target.id === 'fact-q') { S.factQ = e.target.value; renderFacts(); }
      if (e.target.type === 'range') {
        var o = e.target.parentNode.querySelector('output');
        if (o) o.textContent = fmt(e.target.dataset.key, e.target.value);
        e.target.style.setProperty('--p', ((e.target.value - e.target.min) / (e.target.max - e.target.min) * 100) + '%');
      }
    });
  }

  function render() {
    tabsEl.querySelectorAll('.tab').forEach(function (b) {
      var on = b.dataset.tab === tab;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    body.classList.toggle('fill', tab === 'help' || tab === 'conversations' || tab === 'system');
    if (tab === 'help') J.help.render(body);
    else if (tab === 'conversations') convView();
    else if (tab === 'memory') memoryView();
    else if (tab === 'log') logView();
    else if (tab === 'system') systemView();
    else if (tab === 'prefs') prefsView();
    else body.innerHTML = '<div class="empty">This tab is built in a later step.</div>';
  }

  /* ---- conversations ---- */
  function convView() {
    body.innerHTML =
      '<div class="conv"><div class="conv-list">' +
      '<div class="search"><input id="conv-q" type="search" placeholder="Search conversations" value="' + esc(S.convQ) + '"></div>' +
      '<div id="conv-items" class="conv-items" style="display:flex;flex-direction:column;gap:10px"></div>' +
      '</div><div class="conv-pane" id="conv-pane"></div></div>';
    loadConvs();
  }

  function loadConvs() {
    T.get('/api/conversations?q=' + encodeURIComponent(S.convQ)).then(function (list) {
      var items = document.getElementById('conv-items');
      if (!items) return;
      if (!list.length) {
        items.innerHTML = '<div class="empty">No conversations found.</div>';
        document.getElementById('conv-pane').innerHTML = '';
        return;
      }
      if (!S.convId || !list.some(function (c) { return c.id === S.convId; })) S.convId = list[0].id;
      items.innerHTML = list.map(function (c) {
        return '<button class="conv-item' + (c.id === S.convId ? ' on' : '') + '" data-conv="' + c.id + '">' +
          '<div class="w">' + esc(to12(c.when)) + '</div><div class="ti">' + esc(c.title) + '</div>' +
          '<div class="tg">' + esc(c.tags) + '</div></button>';
      }).join('');
      T.get('/api/conversations/' + S.convId).then(function (c) {
        var pane = document.getElementById('conv-pane');
        if (!pane || !c) return;
        pane.innerHTML =
          '<div class="conv-head"><div><h2>' + esc(c.title) + '</h2><p class="sub">' + esc(to12(c.when)) + '</p></div>' +
          '<button class="btn-s" data-delconv="' + c.id + '">Delete</button></div>' +
          c.messages.map(function (m) {
            return '<div class="msg ' + (m.role === 'jarvis' ? 'j' : '') + '"><b>' +
              (m.role === 'jarvis' ? 'JARVIS' : 'YOU') + '</b><span>' + esc(m.text) + '</span></div>';
          }).join('');
      });
    });
  }

  /* ---- memory ---- */
  function memoryView() {
    var chips = [['active', 'Active'], ['forgotten', 'Forgotten'], ['all', 'All']].map(function (c) {
      return '<button class="chip' + (S.factStatus === c[0] ? ' on' : '') + '" data-fstatus="' + c[0] + '">' + c[1] + '</button>';
    }).join('');
    body.innerHTML =
      '<div class="toolbar"><div class="chips">' + chips + '</div>' +
      '<div class="search"><input id="fact-q" type="search" placeholder="Search facts" value="' + esc(S.factQ) + '"></div></div>' +
      '<div class="table" id="facts-table"></div>';
    T.get('/api/facts').then(function (list) { S.facts = list; renderFacts(); });
  }

  function renderFacts() {
    var el = document.getElementById('facts-table');
    if (!el) return;
    var q = S.factQ.toLowerCase();
    var rows = S.facts.filter(function (f) {
      return (S.factStatus === 'all' || f.status === S.factStatus) &&
        (!q || f.key.toLowerCase().indexOf(q) > -1 || f.value.toLowerCase().indexOf(q) > -1);
    });
    var html = '<div class="tr th facts-row"><span>FACT</span><span>VALUE</span><span>SEEN</span><span>CONF</span><span>STATUS</span><span></span></div>';
    if (!rows.length) html += '<div class="empty">No facts here.</div>';
    html += rows.map(function (f) {
      var on = f.status === 'active', ed = S.editing === f.id;
      var val = ed ? '<input class="edit-input" id="edit-val" value="' + esc(f.value) + '">' : '<span>' + esc(f.value) + '</span>';
      var acts = ed
        ? '<button class="btn-s accent" data-act="save" data-id="' + f.id + '">Save</button><button class="btn-s" data-act="cancel" data-id="' + f.id + '">Cancel</button>'
        : (on ? '<button class="btn-s" data-act="edit" data-id="' + f.id + '">Edit</button><button class="btn-s" data-act="forget" data-id="' + f.id + '">Forget</button>'
          : '<button class="btn-s accent" data-act="restore" data-id="' + f.id + '">Restore</button>');
      return '<div class="tr facts-row' + (on ? '' : ' off') + '"><span class="k">' + esc(f.key) + '</span>' + val +
        '<span class="n">' + f.n + '</span><span class="n">' + f.conf.toFixed(2) + '</span>' +
        '<span class="st' + (on ? ' active' : '') + '">' + (on ? 'Active' : 'Forgotten') + '</span><div class="acts">' + acts + '</div></div>';
    }).join('');
    el.innerHTML = html;
    var inp = document.getElementById('edit-val');
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  }

  function factAction(act, id) {
    if (act === 'edit') { S.editing = id; renderFacts(); }
    else if (act === 'cancel') { S.editing = null; renderFacts(); }
    else if (act === 'save') {
      var v = document.getElementById('edit-val').value.trim();
      S.editing = null;
      if (v) T.send('edit_fact', { id: id, value: v }); else renderFacts();
    }
    else if (act === 'forget') T.send('forget_fact', { id: id });
    else if (act === 'restore') T.send('restore_fact', { id: id });
  }

  J.bus.on('facts_changed', function () {
    if (tab === 'memory' && document.getElementById('facts-table')) {
      T.get('/api/facts').then(function (list) { S.facts = list; renderFacts(); });
    }
  });

  J.bus.on('settings_changed', function (e) {
    if (e.key !== 'voice') return;
    S.settings.voice = e.value;
    pickDD('voice', e.value);
  });

  J.bus.on('conversations_changed', function () {
    if (tab === 'conversations' && document.getElementById('conv-items')) loadConvs();
  });

  /* ---- activity log ---- */
  function logView() {
    body.innerHTML = '<div class="table" id="log-table"></div>';
    T.get('/api/log?limit=100').then(function (rows) {
      var el = document.getElementById('log-table');
      if (!el) return;
      var html = '<div class="tr th log-row"><span>TIME</span><span>YOU SAID</span><span>ROUTE</span><span>SPEED</span></div>';
      if (!rows.length) html += '<div class="empty">Nothing logged yet.</div>';
      html += rows.map(function (r) {
        return '<div class="tr log-row"><span class="t">' + esc(r.t) + '</span><span>' + esc(r.text) +
          '</span><span class="route">' + esc(r.route) + '</span><span class="l">' + (r.ms ? r.ms + ' ms' : '-') + '</span></div>';
      }).join('');
      el.innerHTML = html;
    });
  }

  /* ---- system ---- */
  function systemView() {
    body.innerHTML = '<div class="grid2 sys"><div class="card sys-about"><h3 class="card-title">ABOUT</h3><div id="sys-items"></div></div>' +
      '<div class="card sys-log"><h3 class="card-title">RECENT LOG</h3><div class="logtail" id="sys-log"></div></div></div>';
    T.get('/api/system').then(function (d) {
      var a = document.getElementById('sys-items'), l = document.getElementById('sys-log');
      if (!a || !l) return;
      a.innerHTML = d.items.map(function (i) {
        return '<div class="kv" style="margin-bottom:12px"><small>' + esc(i.k) + '</small>' + esc(i.v) +
          (i.sub ? '<small>' + esc(i.sub) + '</small>' : '') + '</div>';
      }).join('');
      l.textContent = d.log;
    });
  }

  /* ---- preferences ---- */
  var VOICES = [];
  var MICS = ['Default microphone', 'USB Microphone', 'Headset (Bluetooth)'];
  var MODELS = ['qwen3:4b-instruct', 'qwen3:8b'];
  var ACCENTS = [
    '#22d3ee', // cyan (default)
    '#38bdf8', // sky
    '#7dd3fc', // ice blue
    '#818cf8', // periwinkle
    '#a78bfa', // violet
    '#e879f9', // neon magenta
    '#f472b6', // hot pink
    '#fb7185', // coral
    '#ff5a4d', // arc-reactor red
    '#fbbf24', // gold
    '#a3e635', // lime
    '#4ade80', // acid green
    '#34d399', // mint
    '#2dd4bf', // teal
    '#f59e0b', // amber
    '#353935'  //onyx
  ];


  function fmt(key, v) {
    v = +v;
    if (key === 'speech_speed') return v.toFixed(2) + 'x';
    if (key === 'silence_threshold') return v.toFixed(2) + ' s';
    if (key === 'speech_threshold') return Math.round(v * 100) + '%';
    if (key === 'max_history_messages') return v + ' messages';
    return v.toFixed(2);
  }
  function setPref(k, v) { S.settings[k] = v; T.send('set_setting', { key: k, value: v }); }

  function selectF(label, key, opts) {
    return '<label class="field">' + label + '<select data-key="' + key + '">' + opts.map(function (o) {
      return '<option' + (S.settings[key] === o ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('') + '</select></label>';
  }
  function ddF(label, key, opts) {          // opts: [{id, label, group?}]
    var groups = {}, order = [], cur = '';
    opts.forEach(function (o) {
      var g = o.group || '';
      if (!groups[g]) { groups[g] = []; order.push(g); }
      groups[g].push(o);
      if (o.id === S.settings[key]) cur = o.label;
    });
    return '<div class="field">' + label + '<div class="dd" data-dd="' + key + '">' +
      '<button type="button" class="dd-btn">' + esc(cur || 'Choose...') + '</button>' +
      '<div class="dd-list" hidden>' +
      order.map(function (g) {
        return (g ? '<div class="dd-group">' + esc(g) + '</div>' : '') + groups[g].map(function (o) {
          return '<button type="button" class="dd-opt' + (o.id === S.settings[key] ? ' on' : '') +
            '" data-id="' + esc(o.id) + '">' + esc(o.label) + '</button>';
        }).join('');
      }).join('') + '</div></div></div>';
  }

  function pickDD(key, id) {
    var dd = document.querySelector('.dd[data-dd="' + key + '"]');
    if (!dd) return;
    var btn = dd.querySelector('.dd-btn');
    dd.querySelectorAll('.dd-opt').forEach(function (b) {
      var on = b.dataset.id === id;
      b.classList.toggle('on', on);
      if (on) btn.textContent = b.textContent;
    });
  }

  function plain(list) {
    return list.map(function (s) { return { id: s, label: s }; });
  }

  function rangeF(label, key, min, max, step, lo, hi) {
    var pct = ((S.settings[key] - min) / (max - min) * 100).toFixed(1);
    return '<div class="field"><div>' + label + '<output>' + fmt(key, S.settings[key]) + '</output></div>' +
      '<input type="range" data-key="' + key + '" min="' + min + '" max="' + max + '" step="' + step + '" value="' + S.settings[key] + '" style="--p:' + pct + '%">' +
      '<div class="ends"><span>' + lo + '</span><span>' + hi + '</span></div></div>';
  }
  function switchF(label, key) {
    return '<label class="switch">' + label + '<input type="checkbox" data-key="' + key + '"' + (S.settings[key] ? ' checked' : '') + '></label>';
  }

  function prefsView() {
    Promise.all([T.get('/api/settings'), T.get('/api/mics'), T.get('/api/voices')]).then(function (r) {
      var st = r[0];
      MICS = r[1];
      VOICES = r[2];
      S.settings = st;
      if (!document.getElementById('set-body') || tab !== 'prefs') return;
      var len = ['short', 'normal', 'long'].map(function (v) {
        return '<button class="' + (st.reply_length === v ? 'on' : '') + '" data-key="reply_length" data-val="' + v + '">' + v[0].toUpperCase() + v.slice(1) + '</button>';
      }).join('');
      var sw = ACCENTS.map(function (c) {
        return '<button class="swatch' + (st.accent === c ? ' on' : '') + '" style="background:' + c + '" data-key="accent" data-val="' + c + '" aria-label="Accent ' + c + '"></button>';
      }).join('');
      body.innerHTML = '<div class="grid2">' +
        '<div class="card"><h3 class="card-title">VOICE</h3>' + ddF('Voice', 'voice', VOICES) +
        rangeF('Speaking speed', 'speech_speed', 0.7, 1.4, 0.05, 'Slower', 'Faster') +
        '<button class="btn-s" id="btn-preview">Preview voice</button></div>' +
        '<div class="card"><h3 class="card-title">MICROPHONE</h3>' + ddF('Input device', 'mic_device', plain(MICS)) +
        rangeF('Speech sensitivity', 'speech_threshold', 0.2, 0.9, 0.05, 'Picks up more', 'Picks up less') +
        rangeF('Pause before Jarvis answers', 'silence_threshold', 0.4, 1.6, 0.05, 'Quicker', 'More patient') + '</div>' +
        '<div class="card"><h3 class="card-title">BRAIN</h3>' + ddF('Language model', 'model_name', plain(MODELS)) +
        '<div class="field">Reply length<div class="seg">' + len + '</div></div>' +
        rangeF('Messages Jarvis remembers in a live chat', 'max_history_messages', 4, 30, 1, 'Fewer', 'More') + '</div>' +
        '<div class="card"><h3 class="card-title">APPEARANCE</h3><div class="field">Accent colour<div class="swatches">' + sw + '</div></div>' +
        switchF('Compact window stays on top', 'compact_always_on_top') +
        switchF('Start in compact mode', 'start_in_compact') + '</div></div>';
    });
  }

  function show() { render(); }

  J.settings = { init: init, show: show };
})();