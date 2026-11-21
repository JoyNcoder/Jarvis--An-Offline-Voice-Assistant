/* Help tab: everything Jarvis understands, as spoken phrases.
   Left: categories (with counts). Right: the phrases, flowing into up to three columns.
   A {curly} part of a phrase is a blank you replace with your own word or number.
   To add a phrase later, just add a line to the right category below. */
(function () {
  var J = window.Jarvis = window.Jarvis || {};

  var GROUPS = [
    { name: 'ON YOUR PC', cats: [
      ['Volume', [
        'Volume up', 'Volume down', 'Turn the volume up', 'Turn the volume down',
        'Make it louder', 'A bit quieter', 'Raise the volume', 'Lower the volume',
        'Set volume to {50} percent', 'Volume {30}', 'Mute', 'Unmute', 'Turn the sound off', 'Turn the sound back on']],
      ['Brightness', [
        'Brightness up', 'Brightness down', 'Brighten the screen', 'Dim the screen',
        'Make it brighter', 'A little dimmer', 'Raise the brightness', 'Lower the brightness',
        'Set brightness to {90} percent', 'Brightness {40}']],
      ['Apps', [
        'Open {Spotify}', 'Open {Notepad}', 'Open {Chrome}', 'Launch {calculator}', 'Open task manager',
        'Open {VS Code}', 'Open command prompt', 'Open PowerShell', 'Open terminal', 'Open paint',
        'Open snipping tool', 'Open control panel', 'Open device manager', 'Open Word', 'Open Excel',
        'Close {Chrome}', 'Quit {Spotify}', 'Rescan apps', 'Refresh the app list']],
      ['Folders & drives', [
        'Open downloads', 'Open documents', 'Open desktop', 'Open pictures', 'Open music',
        'Open videos', 'Open screenshots', 'Open my home folder', 'Open temp', 'Open appdata',
        'Open program files', 'Open {E} drive', 'Open {C} drive', 'Open recycle bin', 'Open this PC',
        'Open {your custom shortcut}']],
      ['Websites & search', [
        'Open YouTube', 'Open Gmail', 'Open GitHub', 'Open Google Maps', 'Open Reddit', 'Open Wikipedia',
        'Open Netflix', 'Open ChatGPT', 'Open Claude', 'Open WhatsApp', 'Open LinkedIn', 'Open Stack Overflow',
        'Search Google for {python tutorials}', 'Search YouTube for {lofi music}',
        'Search Wikipedia for {black holes}', 'Search maps for {pizza near me}',
        'Search the web for {weather}']],
      ['Windows', [
        'Minimize', 'Maximize', 'Restore window', 'Close this window', 'Snap left', 'Snap right',
        'Move window to the left', 'Move window to the right', 'Minimize all', 'Hide everything',
        'Restore windows', 'Switch window', 'Next window', 'Alt tab']],
      ['Screen & theme', [
        'Take a screenshot', 'Screenshot', 'Grab a screenshot of the screen', 'Screenshot this window',
        'Take a window screenshot', 'Dark mode', 'Light mode', 'Switch to dark mode', 'Switch to light mode',
        'Toggle theme']],
      ['Windows settings', [
        'Wi-Fi settings', 'Bluetooth settings', 'Display settings', 'Sound settings', 'Network settings',
        'Battery settings', 'Power settings', 'Night light settings', 'Windows update settings',
        'Default apps settings', 'Startup apps settings', 'Personalization settings', 'Background settings',
        'Taskbar settings', 'Privacy settings', 'Storage settings', 'Notifications settings',
        'Mouse settings', 'Keyboard settings', 'Printers settings', 'Date and time settings',
        'Language settings', 'VPN settings', 'Airplane mode settings', 'Windows security settings',
        'About settings']],
      ['Power', [
        'Lock the computer', 'Lock', 'Put the computer to sleep', 'Go to sleep',
        'Shut down the computer', 'Restart the computer', 'Shut down in {20} minutes',
        'Restart in {1} hour', 'Shut down in {90} seconds', 'Abort', 'Cancel the shutdown',
        'Yes  (when I ask “are you sure?”)', 'No  (to cancel)']],
      ['System stats', [
        'CPU usage', 'RAM usage', 'System stats', 'How much RAM am I using', 'How is my PC doing',
        'Full system report', 'Check everything', 'What is the CPU usage', 'Show me the memory usage',
        'Computer status']]
    ] },
    { name: 'ASK JARVIS', cats: [
      ['Time & date', [
        'What time is it', 'What is the date today', 'What day is it', 'What month is it', 'What year is it',
        'What day is it tomorrow', 'What was the date yesterday', 'What is the date next {Friday}',
        'What is the date in {7} days', 'What time will it be in {two} hours',
        'What was the date {3} days ago', 'What time is it in {Tokyo}', 'What time is it in {London}',
        'What time is it in {New York}']],
      ['Math', [
        'What is {two} plus {two}', 'What is {15} percent of {80}', '{7} times {8}', '{100} divided by {4}',
        'Square root of {144}', 'Cube root of {27}', '{12} squared', '{3} cubed', '{5} factorial',
        '{2} to the power of {10}', 'Half of {90}', 'Double {45}', '{20} percent off {50}',
        'Remainder of {17} divided by {5}', 'What is {9} minus {3}', 'Add {4} and {6}']],
      ['Unit conversion', [
        'Convert {5} miles to kilometers', 'How many minutes in {two} hours', '{30} celsius to fahrenheit',
        '{70} kilograms in pounds', 'How many gigabytes in a terabyte', '{5} feet {10} inches in centimeters',
        '{60} mph to km/h', '{3} liters in gallons', '{10} acres in hectares', 'How many days in {a} year',
        '{500} megabytes in gigabytes', 'How many ounces in a pound', '{100} meters in feet']],
      ['Just talk', [
        'Tell me a joke', 'Explain how {black holes} work', 'Give me ideas for {dinner}',
        'Help me write {an email}', 'What is the capital of {France}', 'Translate {hello} into {Spanish}',
        'Summarize this for me', 'Teach me something new', 'Let’s have a chat']]
    ] },
    { name: 'MEMORY', cats: [
      ['Remember', [
        'Remember that my name is {Rafiq}', 'Call me {Rafiq}', 'I live in {Chittagong}',
        'I work as a {developer}', 'I like {chess} and {gaming}', 'I hate {mornings}',
        'I am learning {Python}', 'I am building {a voice assistant}', 'My GPU is {RTX 4060}',
        'My favorite color is {blue}', 'My sister’s name is {Ayesha}',
        'Remember that the meeting is on {Friday}', 'Make a note that {…}', 'Keep in mind that {…}']],
      ['Recall & forget', [
        'What do you know about me', 'What do you remember about me', 'What is in your memory',
        'What is my {GPU}', 'What’s my {favorite color}', 'Do you remember my {name}',
        'Forget my {city}', 'Forget that', 'Undo that', 'Forget everything',
        'Wipe your memory', 'That’s wrong, I live in {Dhaka}', 'Actually, my name is {Rafiq}',
        'Correction, my {GPU} is {RTX 4070}']],
      ['Past talks', [
        'What did we talk about last time', 'Summarize our last conversation',
        'What have we talked about recently', 'Recap our last chat',
        'What did we talk about yesterday', 'What were we discussing before']]
    ] },
    { name: 'JARVIS', cats: [
      ['Voice', [
        'Change your voice', 'Switch voice', 'Next voice', 'Previous voice', 'Go back to the last voice',
        'Switch to a different voice', 'Change voice to {Bella}', 'Use {Adam} voice', 'Speak as {Emma}',
        'Use a {Spanish} voice', 'Switch to a {French} voice', 'Use {Santa Portuguese}']],
      ['Help', [
        'What can you do on the PC', 'What can you do with {windows}', 'Help with {apps}', 'Help with {volume}',
        'Help with {power}', 'Help with {screen}', 'Help with {stats}', 'List your PC commands',
        'Show me all your commands']]
    ] }
  ];

  function clean(p) { return p.replace(/\{|\}/g, ''); }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }
  function pretty(p) {   // {blank} -> highlighted span
    return esc(p).replace(/\{([^}]*)\}/g, '<b class="v">$1</b>');
  }


  /* Every voice pack. Prefix letter = language, second letter = f (female) / m (male). */
  var LANGS = { a: 'American', b: 'British', e: 'Spanish', f: 'French', h: 'Hindi',
                i: 'Italian', j: 'Japanese', p: 'Portuguese', z: 'Chinese' };
  var VOICE_IDS = ('af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky ' +
    'am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa ' +
    'bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis ' +
    'ef_dora em_alex em_santa ff_siwis hf_alpha hf_beta hm_omega hm_psi if_sara im_nicola ' +
    'jf_alpha jf_gongitsune jf_nezumi jf_tebukuro jm_kumo pf_dora pm_alex pm_santa ' +
    'zf_xiaobei zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang').split(' ');

  function voiceList() {   // same naming rule as tts.py: a repeated name gets its language added
    var seen = {};
    return VOICE_IDS.map(function (id) {
      var name = id.split('_')[1], lang = LANGS[id[0]];
      var say = seen[name] ? name + ' ' + lang.toLowerCase() : name;
      seen[name] = true;
      return { say: say, lang: lang, code: id.slice(0, 2), sex: id[1] === 'f' ? 'Female' : 'Male' };
    });
  }

  function voiceBlock() {
    var q = state.q.trim().toLowerCase();
    var list = voiceList().filter(function (v) {
      return !q || (v.say + ' ' + v.lang).toLowerCase().indexOf(q) !== -1;
    });
    var legend = Object.keys(LANGS).map(function (k) {
      return '<span><b>' + k + '</b> ' + LANGS[k] + '</span>';
    }).join('');
    return '<div class="help-head vh"><h2>All voices</h2><span>' + list.length + ' voices</span></div>' +
      '<div class="help-legend"><span class="lg">First letter = accent, second = voice type:</span>' + legend + '<span><b>f</b> female</span><span><b>m</b> male</span></div>' +
      '<ul class="help-voices">' + list.map(function (v) {
        return '<li><span class="vn">' + esc(v.say) + '</span><em title="' + v.lang + ', ' + v.sex.toLowerCase() + '">' +
               v.code + '</em></li>';
      }).join('') + '</ul>' +
      '<p class="help-tip">Say a name to switch, like “Use <b class="v">Nicole</b>”. If two languages share a name, add the language: “Use <b class="v">Santa Portuguese</b>”.</p>';
  }

  var state = { cat: 'Volume', q: '' };

  function matching(list) {
    var q = state.q.trim().toLowerCase();
    if (!q) return list;
    return list.filter(function (p) { return clean(p).toLowerCase().indexOf(q) !== -1; });
  }
  function total(cats) { return cats.reduce(function (n, c) { return n + c[1].length; }, 0); }

  function render(body) {
    var all = 0;
    GROUPS.forEach(function (g) { all += total(g.cats); });

    body.innerHTML =
      '<div class="help">' +
        '<div class="help-nav">' +
          '<div class="search"><input id="help-q" type="search" placeholder="Search ' + all + ' phrases" value="' + esc(state.q) + '"></div>' +
          '<div class="help-cats" id="help-cats"></div>' +
        '</div>' +
        '<div class="help-pane" id="help-pane"></div>' +
      '</div>';

    var q = body.querySelector('#help-q');
    q.addEventListener('input', function () { state.q = q.value; draw(body); });
    body.querySelector('#help-cats').addEventListener('click', function (e) {
      var b = e.target.closest('[data-cat]');
      if (b) { state.cat = b.dataset.cat; draw(body); }
    });
    draw(body);
  }

  function draw(body) {
    var catsEl = body.querySelector('#help-cats'), pane = body.querySelector('#help-pane');
    var html = '', firstHit = null, found = null;

    GROUPS.forEach(function (g) {
      var rows = '';
      g.cats.forEach(function (c) {
        var n = matching(c[1]).length;
        if (!n) return;
        if (!firstHit) firstHit = c;
        if (c[0] === state.cat) found = c;
        rows += '<button class="help-cat' + (c[0] === state.cat ? ' on' : '') + '" data-cat="' + esc(c[0]) + '">' +
                  '<span>' + esc(c[0]) + '</span><i>' + n + '</i></button>';
      });
      if (rows) html += '<div class="help-group">' + g.name + '</div>' + rows;
    });
    catsEl.innerHTML = html || '<div class="empty">No phrase matches.</div>';

    var cat = found || firstHit;
    if (!cat) { pane.innerHTML = '<div class="empty">Nothing matches “' + esc(state.q) + '”. Try a shorter word.</div>'; return; }
    if (!found) {   // the chosen category has no match: show the first one that does
      var b = catsEl.querySelector('[data-cat="' + cat[0].replace(/"/g, '\\"') + '"]');
      if (b) b.classList.add('on');
    }
    var list = matching(cat[1]);
    pane.scrollTop = 0;
    pane.innerHTML =
      '<div class="help-head"><h2>' + esc(cat[0]) + '</h2><span>' + list.length + (list.length === 1 ? ' phrase' : ' phrases') + '</span></div>' +
      '<ul class="help-phrases">' + list.map(function (p) { return '<li>' + pretty(p) + '</li>'; }).join('') + '</ul>' +
      '<p class="help-tip"><b class="v">Cyan</b> words are blanks. Swap them for your own app, number or name.</p>' +
      (cat[0] === 'Voice' ? '<div class="help-sep"></div>' + voiceBlock() : '');
  }

  J.help = { render: render };
})();
