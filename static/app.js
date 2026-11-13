/* Sentinel MK1 – Weboberfläche */
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'html') e.innerHTML = v;
    else if (k in e && typeof v !== 'string') e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
}
async function api(path, opts) {
  const o = opts || {};
  const r = await fetch(path, {method: o.method || (o.body ? 'POST' : 'GET'),
    headers: o.body ? {'Content-Type': 'application/json'} : {}, body: o.body ? JSON.stringify(o.body) : undefined});
  let d = null;
  try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) throw new Error((d && d.error) || ('HTTP ' + r.status));
  return d;
}
function toast(msg, cls, ms) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast show ' + (cls || '');
  clearTimeout(t._h); t._h = setTimeout(() => t.className = 'toast', ms || 2600);
}
const pad = n => String(n).padStart(2, '0');
function fmtTime(ts, withDate) {
  if (!ts) return '–';
  const d = new Date(ts * 1000), now = new Date();
  const t = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  if (!withDate && d.toDateString() === now.toDateString()) return t;
  return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + (withDate === 'full' ? d.getFullYear() + ' ' : ' ') + t;
}
function ago(ts) {
  if (!ts) return '–';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'vor ' + Math.max(1, Math.round(s)) + ' s';
  if (s < 3600) return 'vor ' + Math.round(s / 60) + ' min';
  if (s < 86400) return 'vor ' + Math.round(s / 3600) + ' h';
  return 'vor ' + Math.round(s / 86400) + ' Tagen';
}
const S = {tab: 'live', meta: null, state: null, cfg: null, cats: [], live: {}, liveMode: 'live', big: false,
  alarmSeq: null, gal: {items: [], last: null}, objs: [], objCache: {}};

/* ------------------------------------------------------------ Modal */
function openModal(content, narrow) {
  const b = $('#mbox'); b.className = 'box' + (narrow ? ' narrow' : ''); b.innerHTML = '';
  b.append(el('button', {class: 'btn-mini x', onclick: closeModal}, '✕ Schließen'), content);
  $('#modal').hidden = false; document.body.style.overflow = 'hidden';
}
function closeModal() { $('#modal').hidden = true; $('#mbox').innerHTML = ''; document.body.style.overflow = ''; S.modalObj = null; }
$('#modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#modal').hidden) closeModal(); });

/* ------------------------------------------------------------ Tabs */
function showTab(t) {
  S.tab = t;
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  $$('main > section').forEach(s => s.hidden = s.id !== 'tab-' + t);
  try { localStorage.setItem('sentinel_tab', t); } catch (e) {}
  if (t === 'live') renderLive(true);
  if (t === 'gallery') loadGallery(true);
  if (t === 'objects') loadObjects();
  if (t === 'alarms') loadAlarms();
  if (t === 'settings') loadSettings();
}
$$('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));

/* ------------------------------------------------------------ Status */
function groupOf(cls) { const c = S.meta && S.meta.classes.find(x => x.id === cls); return c ? c.group : 'other'; }
function groupColor(gid) { const g = S.meta.groups.find(x => x.id === gid); return g ? g.color : '#9aa4b2'; }
let beepCtx = null;
function beep() {
  try {
    beepCtx = beepCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t0 = beepCtx.currentTime;
    [0, .22, .44].forEach((d, i) => {
      const o = beepCtx.createOscillator(), g = beepCtx.createGain();
      o.type = 'square'; o.frequency.value = i === 1 ? 660 : 880;
      g.gain.setValueAtTime(.0001, t0 + d); g.gain.exponentialRampToValueAtTime(.12, t0 + d + .02);
      g.gain.exponentialRampToValueAtTime(.0001, t0 + d + .18);
      o.connect(g); g.connect(beepCtx.destination); o.start(t0 + d); o.stop(t0 + d + .2);
    });
  } catch (e) {}
}
document.addEventListener('click', () => { try { if (beepCtx && beepCtx.state === 'suspended') beepCtx.resume(); } catch (e) {} }, {once: false});

async function pollState() {
  try {
    const st = await api('/api/state');
    S.state = st;
    $('#ver').textContent = 'v' + st.version;
    const on = st.streams.filter(s => s.online).length;
    const h = $('#hstat'); h.innerHTML = '';
    h.append(el('span', null, el('span', {class: 'status-dot ' + (st.streams.length && on === st.streams.length ? 'online' : on ? 'warn' : 'idle')}),
      ' Streams ', el('b', null, on + '/' + st.streams.length)));
    h.append(el('span', null, el('span', {class: 'status-dot ' + (st.models_ready ? 'online' : 'warn')}),
      ' KI ', el('b', null, st.models_ready ? (st.det_ms != null ? st.det_ms + ' ms' : 'bereit') : 'lädt …')));
    if (st.sys && st.sys.gpu != null) h.append(el('span', null, 'GPU ', el('b', null, st.sys.gpu + ' %')));
    if (st.load) h.append(el('span', null, 'Last ', el('b', null, st.load[0])));
    if (st.sys && st.sys.temp != null) h.append(el('span', null, el('b', {style: {color: st.sys.temp >= 80 ? 'var(--danger)' : ''}}, st.sys.temp + ' °C')));
    if (st.sys && st.sys.mem_total) h.append(el('span', null, 'RAM ', el('b', null, (st.sys.mem_used / 1024).toFixed(1) + '/' + (st.sys.mem_total / 1024).toFixed(1) + ' GB')));
    h.append(el('span', {class: 'clock'}, el('b', null, st.time_text)));
    const pill = $('#pill'); pill.hidden = !st.unack; pill.textContent = st.unack;
    if (S.alarmSeq !== null && st.alarm_seq > S.alarmSeq) newAlarm();
    S.alarmSeq = st.alarm_seq;
    if (S.tab === 'live') renderLive(false);
    if (S.tab === 'settings') updateSettingsStatus();
  } catch (e) {
    $('#hstat').innerHTML = '';
    $('#hstat').append(el('span', null, el('span', {class: 'status-dot'}), ' Verbindung zum Gerät unterbrochen'));
  }
}
async function newAlarm() {
  try {
    const ev = await api('/api/events?limit=1');
    if (!ev.length) return;
    const t = $('#toast');
    t.textContent = '⚠ ALARM: ' + ev[0].text; t.className = 'toast show alarm';
    t.onclick = () => { t.className = 'toast'; showTab('alarms'); };
    clearTimeout(t._h); t._h = setTimeout(() => t.className = 'toast', 12000);
    const rules = await api('/api/alarms');
    const r = rules.find(x => x.id === ev[0].alarm_id);
    if (S.state.ui.sound && (!r || r.sound)) beep();
    if (S.tab === 'alarms') loadAlarms();
  } catch (e) {}
}

/* ------------------------------------------------------------ Live */
function renderLegend() {
  const lg = $('#legend'); lg.innerHTML = '';
  const act = (S.cfg && S.cfg.detection.groups) || [];
  S.meta.groups.filter(g => act.includes(g.id) || (g.id === 'other' && S.cfg && S.cfg.detection.extra_classes.length))
    .forEach(g => lg.append(el('span', null, el('i', {style: {borderColor: g.color}}), g.name)));
}
function renderLive(full) {
  const st = S.state; if (!st || !S.meta) return;
  const grid = $('#liveGrid');
  grid.classList.toggle('big', S.big);
  if (!st.streams.length) {
    grid.innerHTML = '';
    grid.append(el('div', {class: 'empty', style: {gridColumn: '1/-1'}}, el('b', null, 'Noch keine Kamera eingerichtet'),
      'Unter ', el('a', {href: '#', onclick: e => { e.preventDefault(); showTab('settings'); }}, 'Einstellungen → Streams'),
      ' die RTSP-Adressen der Kameras eintragen.'));
    return;
  }
  const ids = st.streams.map(s => s.id).join(',');
  if (full || grid.dataset.ids !== ids || grid.dataset.mode !== S.liveMode) {
    grid.innerHTML = ''; grid.dataset.ids = ids; grid.dataset.mode = S.liveMode;
    const token = S.liveToken = (S.liveToken || 0) + 1;
    st.streams.forEach(s => {
      const view = el('div', {class: 'cam-view', id: 'v-' + s.id});
      const img = el('img', {alt: '', id: 'img-' + s.id});
      view.append(img, el('div', {class: 'ov', id: 'ov-' + s.id}), el('div', {class: 'msg', id: 'msg-' + s.id}));
      view.ondblclick = () => { if (view.requestFullscreen) view.requestFullscreen(); };
      grid.append(el('div', {class: 'cam'},
        el('div', {class: 'cam-head'}, el('span', {class: 'status-dot', id: 'dot-' + s.id}), el('h3', null, s.name),
          el('span', {class: 'meta', id: 'meta-' + s.id}),
          el('button', {class: 'btn-mini', title: 'Vollbild', onclick: () => view.requestFullscreen && view.requestFullscreen()}, '⛶')),
        view, el('div', {class: 'cam-foot', id: 'foot-' + s.id})));
      if (S.liveMode === 'live') liveLoop(s.id, img, token);
    });
  }
  st.streams.forEach(s => {
    const dot = $('#dot-' + s.id), meta = $('#meta-' + s.id), msg = $('#msg-' + s.id), view = $('#v-' + s.id);
    if (!dot) return;
    dot.className = 'status-dot ' + (s.online ? 'online' : s.status === 'verbinde' ? 'warn' : '');
    meta.textContent = s.online ? (s.size ? s.size[0] + '×' + s.size[1] + ' · ' : '') + s.fps + ' fps' + (s.decoder ? ' · ' + s.decoder : '') : s.status;
    msg.textContent = s.online ? '' : (s.status === 'verbinde' ? 'Verbinde …' : (s.error || s.status));
    msg.style.display = s.online ? 'none' : 'flex';
    if (s.size) view.style.aspectRatio = s.size[0] + '/' + s.size[1];
  });
}
/* Live-Bild: jeweils das naechste JPEG abholen (kurze Anfragen statt MJPEG, damit
   auch bei vielen Kameras genug Browser-Verbindungen fuer die Oberflaeche frei bleiben) */
async function liveLoop(sid, img, token) {
  let last = -1, url = null;
  while (S.liveToken === token && S.liveMode === 'live') {
    if (S.tab !== 'live' || document.hidden) { await new Promise(r => setTimeout(r, 700)); continue; }
    try {
      const r = await fetch('/api/stream/' + sid + '.jpg?after=' + last, {cache: 'no-store'});
      if (!r.ok) throw new Error(r.status);
      last = +(r.headers.get('X-Frame') || last);
      const b = await r.blob();
      if (S.liveToken !== token) break;
      const u = URL.createObjectURL(b);
      img.src = u;
      if (url) URL.revokeObjectURL(url);
      url = u;
    } catch (e) { await new Promise(r => setTimeout(r, 2500)); }
  }
  if (url) URL.revokeObjectURL(url);
}
async function pollLive() {
  if (S.tab !== 'live' || !S.state) return;
  try {
    const live = await api('/api/live');
    S.live = live;
    for (const s of S.state.streams) {
      const ov = $('#ov-' + s.id), foot = $('#foot-' + s.id);
      if (!ov) continue;
      const L = live[s.id];
      if (S.liveMode === 'analyzed' && L && ov.dataset.ts !== String(L.ts)) {
        const img = $('#img-' + s.id), n = new Image();
        n.onload = () => { img.src = n.src; drawBoxes(ov, L); };
        n.src = '/api/stream/' + s.id + '/analyzed.jpg?' + L.ts;
        ov.dataset.ts = String(L.ts);
      } else if (S.liveMode === 'live') drawBoxes(ov, L);
      foot.innerHTML = '';
      const cnt = {};
      (L ? L.boxes : []).filter(b => b.confirmed && b.age <= 0.3).forEach(b => { const g = groupOf(b.cls); cnt[g] = (cnt[g] || 0) + 1; });
      const keys = Object.keys(cnt);
      if (!keys.length) foot.append(el('span', {class: 'muted mono small'}, L ? (L.idle ? 'keine Bewegung' : 'nichts erkannt') : 'warte auf Analyse …'));
      keys.forEach(g => { const gg = S.meta.groups.find(x => x.id === g); foot.append(el('span', {class: 'chip'}, el('i', {style: {background: gg.color}}), gg.name + ': ' + cnt[g])); });
      (L ? L.boxes : []).filter(b => b.named && b.age <= 0.3).forEach(b => foot.append(el('span', {class: 'chip', style: {borderColor: b.color}}, '★ ' + b.label)));
    }
  } catch (e) {}
}
function drawBoxes(ov, L) {
  ov.innerHTML = '';
  if (!L) return;
  L.boxes.forEach(b => {
    if (b.age > 0.3) return;  // nur Objekte, die in der letzten Analyse gefunden wurden
    const d = el('div', {class: 'bx' + (b.confirmed ? '' : ' tent') + (b.named ? ' named' : '') + (b.y1 < .06 ? ' low' : ''),
      style: {left: (b.x1 * 100) + '%', top: (b.y1 * 100) + '%', width: ((b.x2 - b.x1) * 100) + '%', height: ((b.y2 - b.y1) * 100) + '%',
        borderColor: b.color, color: b.color},
      title: b.label + ' (' + Math.round(b.score * 100) + ' %)',
      onclick: () => { if (b.obj) openObject(b.obj); }});
    d.append(el('span', {style: {background: b.color}}, (b.face ? '☺ ' : '') + b.label + (b.cat ? ' · ' + b.cat : '')));
    ov.append(d);
  });
}
$('#liveMode').onclick = () => {
  S.liveMode = S.liveMode === 'live' ? 'analyzed' : 'live';
  $('#liveMode').textContent = 'Ansicht: ' + (S.liveMode === 'live' ? 'Live' : 'Analysebild');
  $('#liveMode').classList.toggle('on', S.liveMode !== 'live');
  renderLive(true);
};
$('#liveBig').onclick = () => { S.big = !S.big; $('#liveBig').classList.toggle('on', S.big); renderLive(false); };

/* ------------------------------------------------------------ Galerie */
function fillSelects() {
  const gs = [['#gGroup', 'Alle Arten'], ['#oGroup', 'Alle Arten']];
  gs.forEach(([id, t]) => {
    const s = $(id), v = s.value; s.innerHTML = ''; s.append(el('option', {value: ''}, t));
    S.meta.groups.forEach(g => s.append(el('option', {value: g.id}, g.name))); s.value = v;
  });
  const st = $('#gStream'), v = st.value; st.innerHTML = ''; st.append(el('option', {value: ''}, 'Alle Streams'));
  (S.cfg ? S.cfg.streams : []).forEach(s => st.append(el('option', {value: s.id}, s.name))); st.value = v;
  [['#gCat', [['', 'Alle Kategorien']]], ['#oCat', [['', 'Alle Kategorien'], ['none', 'ohne Kategorie']]]].forEach(([id, base]) => {
    const s = $(id), v2 = s.value; s.innerHTML = '';
    base.forEach(([k, t]) => s.append(el('option', {value: k}, t)));
    S.cats.forEach(c => s.append(el('option', {value: c.id}, c.name))); s.value = v2;
  });
}
async function loadCats() { S.cats = await api('/api/categories'); fillSelects(); }
function galQuery(before) {
  const p = new URLSearchParams();
  if ($('#gStream').value) p.set('stream', $('#gStream').value);
  if ($('#gGroup').value) p.set('group', $('#gGroup').value);
  if ($('#gCat').value) p.set('category', $('#gCat').value);
  if ($('#gSight').checked) p.set('sightings', '1');
  if ($('#gNamed').checked) p.set('named', '1');
  if ($('#gDate').value) {
    const d = new Date($('#gDate').value + 'T00:00:00');
    p.set('from', d.getTime() / 1000); p.set('to', d.getTime() / 1000 + 86400);
  }
  if (before) p.set('before', before);
  p.set('limit', 96);
  return '/api/detections?' + p.toString();
}
function detCard(d, opts) {
  const o = opts || {};
  const c = el('div', {class: 'card', onclick: e => { if (e.target.type === 'checkbox') return; (o.onclick || openDetection)(d.id); }},
    el('div', {class: 'img', style: {backgroundImage: d.crop ? 'url(' + d.crop + ')' : 'none', borderTopColor: d.color}}),
    el('div', {class: 'badges'}, d.face ? el('span', {class: 'badge face', title: 'Gesicht erkannt'}, '☺') : null,
      d.manual ? el('span', {class: 'badge man', title: 'manuell zugeordnet'}, '✓') : null),
    el('div', {class: 'inf'}, el('div', {class: 't1', style: {color: d.named ? d.color : ''}}, d.object || d.cls_de),
      el('div', {class: 't2'}, fmtTime(d.ts) + ' · ' + d.cls_de + (d.category ? ' · ' + d.category : '')),
      el('div', {class: 't3'}, d.stream)));
  if (o.select) {
    const cb = el('input', {type: 'checkbox', class: 'sel', checked: o.select.has(d.id)});
    cb.onchange = () => { cb.checked ? o.select.add(d.id) : o.select.delete(d.id); o.onsel && o.onsel(); };
    c.append(cb);
  }
  return c;
}
async function loadGallery(reset) {
  if (reset) { S.gal = {items: [], last: null}; $('#gal').innerHTML = ''; }
  try {
    const r = await api(galQuery(reset ? null : S.gal.last));
    const g = $('#gal');
    if (reset && !r.items.length) g.append(el('div', {class: 'empty', style: {gridColumn: '1/-1'}}, el('b', null, 'Keine Bilder'), 'Sobald etwas erkannt wird, erscheint es hier.'));
    r.items.forEach(d => g.append(detCard(d)));
    S.gal.items = S.gal.items.concat(r.items);
    if (r.items.length) S.gal.last = r.items[r.items.length - 1].id;
    $('#gMore').hidden = S.gal.items.length >= r.total || !r.items.length;
    $('#gCount').textContent = r.total + ' Bilder';
  } catch (e) { toast(e.message); }
}
['#gStream', '#gGroup', '#gCat', '#gSight', '#gNamed', '#gDate'].forEach(id => $(id).onchange = () => loadGallery(true));
$('#gMore').onclick = () => loadGallery(false);

function shotWithBox(d) {
  const sh = el('div', {class: 'shot'});
  const img = el('img', {src: d.img || d.crop, alt: ''});
  sh.append(img);
  if (d.img) {
    const ov = el('div', {class: 'ov'});
    const [x1, y1, x2, y2] = d.box;
    ov.append(el('div', {class: 'bx named' + (y1 < .06 ? ' low' : ''), style: {left: x1 * 100 + '%', top: y1 * 100 + '%',
      width: (x2 - x1) * 100 + '%', height: (y2 - y1) * 100 + '%', borderColor: d.color, color: d.color, pointerEvents: 'none'}},
      el('span', {style: {background: d.color}}, (d.object || d.cls_de) + ' ' + Math.round(d.score * 100) + ' %')));
    sh.append(ov);
  }
  return sh;
}
async function objectOptions(sel, cls, current, includeNew) {
  const grp = groupOf(cls);
  const list = await api('/api/objects?group=' + encodeURIComponent(grp) + '&sort=name&limit=1000');
  sel.innerHTML = '';
  if (includeNew) sel.append(el('option', {value: 'new'}, '➕ Neues Objekt anlegen'));
  const named = list.filter(o => o.name), un = list.filter(o => !o.name);
  const add = (lab, arr) => { if (!arr.length) return; const og = el('optgroup', {label: lab});
    arr.forEach(o => og.append(el('option', {value: o.id, disabled: o.id === current},
      o.label + (o.category ? ' · ' + o.category : '') + ' (' + o.sightings + '×)' + (o.id === current ? ' – aktuell' : '')))); sel.append(og); };
  add('Benannt', named); add('Unbenannt', un);
}
async function openDetection(id) {
  let d;
  try { d = await api('/api/detection/' + id); } catch (e) { return toast(e.message); }
  const info = el('div', {class: 'kv'},
    el('span', null, 'Zeit'), el('span', null, fmtTime(d.ts, 'full')),
    el('span', null, 'Stream'), el('span', null, d.stream),
    el('span', null, 'Art'), el('span', null, el('span', {class: 'clsb'}, el('i', {style: {background: d.color}}), d.cls_de + ' (' + d.group + ')')),
    el('span', null, 'Sicherheit'), el('span', null, Math.round(d.score * 100) + ' %'),
    el('span', null, 'Objekt'), el('span', null, d.object_id ? el('a', {href: '#', onclick: e => { e.preventDefault(); openObject(d.object_id); }}, d.object) : '–',
      d.category ? el('span', {class: 'muted'}, ' · ' + d.category) : null),
    el('span', null, 'Zuordnung'), el('span', null, d.manual ? 'manuell korrigiert' : (d.match === 'neu' ? 'neues Objekt' :
      (d.match || '–') + (d.sim ? ' (' + Math.round(d.sim * 100) + ' %)' : ''))),
    el('span', null, 'Gesicht'), el('span', null, d.face ? 'ja' : 'nein'));
  const sel = el('select'); const nm = el('input', {type: 'text', placeholder: 'Name für das neue Objekt (optional)'});
  const whole = el('input', {type: 'checkbox', checked: d.track_count > 1});
  const fix = el('div', {class: 'fixbox', hidden: true},
    el('b', null, 'Falsch zugeordnet? Richtiges Objekt wählen'),
    el('span', {class: 'muted small'}, 'Das System merkt sich die Korrektur: Die Merkmale zählen künftig für das gewählte Objekt und als Gegenbeispiel für das bisherige.'),
    sel, nm, d.track_count > 1 ? el('label', {class: 'chk'}, whole, 'alle ' + d.track_count + ' Bilder dieser Sichtung verschieben') : null,
    el('div', {class: 'actions', style: {marginTop: 0}}, el('button', {class: 'btn', onclick: async () => {
      try {
        const r = await api('/api/detection/' + d.id + '/move', {body: {target: sel.value, whole_track: whole.checked, name: nm.value}});
        toast(r.moved + ' Bild(er) neu zugeordnet'); closeModal(); refreshCurrent();
      } catch (e) { toast(e.message); }
    }}, 'Zuordnung speichern')));
  sel.onchange = () => nm.style.display = sel.value === 'new' ? '' : 'none';
  openModal(el('div', null, el('h2', null, d.cls_de + ' · ', el('span', {class: 'acc'}, d.object || '')),
    el('div', {class: 'detail'}, shotWithBox(d), el('div', null, info,
      el('div', {class: 'actions'},
        el('button', {class: 'btn', onclick: async () => { fix.hidden = !fix.hidden; if (!fix.hidden) { await objectOptions(sel, d.cls, d.object_id, true); sel.onchange(); } }}, 'Zuordnung korrigieren'),
        d.object_id ? el('button', {class: 'btn btn-ghost', onclick: () => openObject(d.object_id)}, 'Objekt öffnen') : null,
        el('a', {class: 'btn btn-ghost', href: d.img || d.crop, download: '', style: {textDecoration: 'none'}}, 'Bild laden'),
        el('button', {class: 'btn btn-danger', onclick: async () => {
          if (!confirm('Dieses Bild löschen?')) return;
          await api('/api/detection/' + d.id, {method: 'DELETE'}); toast('Gelöscht'); closeModal(); refreshCurrent();
        }}, 'Löschen')), fix))));
}
function refreshCurrent() {
  if (S.tab === 'gallery') loadGallery(true);
  if (S.tab === 'objects') loadObjects();
  if (S.tab === 'alarms') loadAlarms();
}

/* ------------------------------------------------------------ Objekte */
function catSelect(value, onchange) {
  const s = el('select');
  s.append(el('option', {value: ''}, '– keine Kategorie –'));
  S.cats.forEach(c => s.append(el('option', {value: c.id}, c.name)));
  s.value = value || '';
  s.onchange = () => onchange(s.value);
  return s;
}
async function saveObj(id, body, quiet) {
  try { await api('/api/objects/' + id, {body}); if (!quiet) toast('Gespeichert'); } catch (e) { toast(e.message); }
}
async function loadObjects() {
  const p = new URLSearchParams();
  if ($('#oQ').value.trim()) p.set('q', $('#oQ').value.trim());
  if ($('#oGroup').value) p.set('group', $('#oGroup').value);
  if ($('#oCat').value) p.set('category', $('#oCat').value);
  p.set('sort', $('#oSort').value);
  if ($('#oMin').checked) p.set('min', (S.state && S.state.ui.repeat_list_min) || 2);
  let list;
  try { list = await api('/api/objects?' + p.toString()); } catch (e) { return toast(e.message); }
  S.objs = list;
  const box = $('#olist'); box.innerHTML = '';
  if (!list.length) {
    box.append(el('div', {class: 'empty'}, el('b', null, 'Keine Objekte'), $('#oMin').checked ?
      'Hier erscheinen Objekte, die mehrfach erkannt wurden. Haken „nur mehrfach erkannte“ entfernen, um alle zu sehen.' : 'Noch nichts erkannt.'));
    return;
  }
  box.append(el('div', {class: 'ohead'}, el('span'), el('span', null, 'Name'), el('span', null, 'Art'), el('span', null, 'Kategorie'),
    el('span', null, 'Sichtungen'), el('span', null, 'Streams'), el('span', null, 'Zuletzt'), el('span')));
  list.forEach(o => {
    const name = el('input', {type: 'text', value: o.name, placeholder: o.label + ' – Namen vergeben'});
    name.onchange = () => saveObj(o.id, {name: name.value});
    box.append(el('div', {class: 'orow'},
      el('div', {class: 'av', style: {backgroundImage: o.avatar ? 'url(' + o.avatar + ')' : 'none', borderColor: o.cat_color || o.color}, onclick: () => openObject(o.id)}),
      el('div', {class: 'nm'}, name),
      el('div', {class: 'cls'}, el('span', {class: 'clsb'}, el('i', {style: {background: o.color}}), o.cls_de + (o.has_face ? ' ☺' : ''))),
      el('div', {class: 'cat'}, catSelect(o.category_id, v => saveObj(o.id, {category_id: v || null}))),
      el('div', {class: 'cnt'}, o.sightings, el('small', null, o.detections + ' Bilder')),
      el('div', {class: 'st', title: o.streams.join(', ')}, o.streams.join(', ') || '–'),
      el('div', {class: 'st seen', title: fmtTime(o.last_seen, 'full')}, ago(o.last_seen)),
      el('div', {class: 'go'}, el('button', {class: 'btn-mini', onclick: () => openObject(o.id)}, 'Öffnen'))));
  });
}
let oqTimer = null;
$('#oQ').oninput = () => { clearTimeout(oqTimer); oqTimer = setTimeout(loadObjects, 300); };
['#oGroup', '#oCat', '#oSort', '#oMin'].forEach(id => $(id).onchange = loadObjects);

async function openObject(id) {
  let o;
  try { o = await api('/api/object/' + id); } catch (e) { return toast(e.message); }
  S.modalObj = id;
  const sel = new Set();
  const name = el('input', {type: 'text', value: o.name, placeholder: o.label});
  const notes = el('textarea', {placeholder: 'Notizen …'}, o.notes);
  const cat = catSelect(o.category_id, () => {});
  const smp = {}; o.samples.forEach(s => smp[s.kind + (s.neg ? '_neg' : '')] = s.n);
  const grid = el('div', {class: 'gal'});
  const bar = el('div', {class: 'selbar', hidden: true});
  const tsel = el('select', {style: {minWidth: '220px'}});
  const nm2 = el('input', {type: 'text', placeholder: 'Name für neues Objekt', style: {display: 'none'}});
  const cntLbl = el('b');
  tsel.onchange = () => nm2.style.display = tsel.value === 'new' ? '' : 'none';
  bar.append(cntLbl, el('span', {class: 'muted small'}, 'falsch zugeordnet → verschieben nach'), tsel, nm2,
    el('button', {class: 'btn', onclick: async () => {
      try {
        let target = tsel.value, moved = 0;
        for (const did of sel) {
          const r = await api('/api/detection/' + did + '/move', {body: {target, name: nm2.value}});
          target = r.object_id; moved += r.moved;
        }
        toast(moved + ' Bild(er) neu zugeordnet – das System lernt daraus'); openObject(id); loadObjects();
      } catch (e) { toast(e.message); }
    }}, 'Verschieben'),
    el('button', {class: 'btn-mini', onclick: () => { sel.clear(); grid.querySelectorAll('input.sel').forEach(c => c.checked = false); upd(); }}, 'Auswahl aufheben'));
  let filled = false;
  const upd = async () => {
    bar.hidden = !sel.size; cntLbl.textContent = sel.size + ' ausgewählt';
    if (sel.size && !filled) { filled = true; await objectOptions(tsel, o.cls, o.id, true); tsel.onchange(); }
  };
  o.items.forEach(d => grid.append(detCard(d, {select: sel, onsel: upd})));
  const merge = el('select', {style: {minWidth: '220px'}});
  const mergeBox = el('div', {class: 'fixbox', hidden: true}, el('b', null, 'Mit anderem Objekt zusammenführen'),
    el('span', {class: 'muted small'}, 'Alle Bilder und Lernbeispiele von „' + o.label + '“ wandern zum gewählten Objekt; dieses Objekt wird danach entfernt.'),
    merge, el('div', {class: 'actions', style: {marginTop: 0}}, el('button', {class: 'btn', onclick: async () => {
      if (!merge.value) return;
      try { await api('/api/objects/' + o.id + '/merge', {body: {into: +merge.value}}); toast('Zusammengeführt'); openObject(+merge.value); loadObjects(); }
      catch (e) { toast(e.message); }
    }}, 'Zusammenführen')));
  openModal(el('div', null,
    el('h2', null, o.cls_de + ' · ', el('span', {class: 'acc'}, o.label)),
    el('div', {class: 'obj-head'},
      el('div', {class: 'av', style: {backgroundImage: o.avatar ? 'url(' + o.avatar + ')' : 'none', borderColor: o.cat_color || o.color}}),
      el('div', {class: 'f'},
        el('div', {class: 'form'}, el('label', null, 'Name'), name, el('label', null, 'Kategorie'), cat, el('label', null, 'Notizen'), notes),
        el('div', {class: 'actions', style: {marginTop: 4}},
          el('button', {class: 'btn', onclick: async () => {
            await saveObj(o.id, {name: name.value, notes: notes.value, category_id: cat.value || null}); loadObjects(); openObject(o.id);
          }}, 'Speichern'),
          el('button', {class: 'btn btn-ghost', onclick: async () => { mergeBox.hidden = !mergeBox.hidden;
            if (!mergeBox.hidden) { await objectOptions(merge, o.cls, o.id, false); } }}, 'Zusammenführen …'),
          el('button', {class: 'btn btn-danger', onclick: async () => {
            if (!confirm('Objekt „' + o.label + '“ mit allen ' + o.detections + ' Bildern und Lernbeispielen löschen?')) return;
            await api('/api/objects/' + o.id, {method: 'DELETE'}); toast('Gelöscht'); closeModal(); loadObjects();
          }}, 'Löschen')),
        mergeBox)),
    el('h4', null, 'Übersicht'),
    el('div', {class: 'kv'},
      el('span', null, 'Sichtungen'), el('span', null, o.sightings + ' (' + o.detections + ' Bilder)'),
      el('span', null, 'Erstmals'), el('span', null, fmtTime(o.first_seen, 'full')),
      el('span', null, 'Zuletzt'), el('span', null, fmtTime(o.last_seen, 'full') + ' (' + ago(o.last_seen) + ')'),
      el('span', null, 'Streams'), el('span', null, o.streams.join(', ') || '–'),
      el('span', null, 'Gelernt'), el('span', null, (smp.face || 0) + ' Gesichter, ' + (smp.app || 0) + ' Aussehen-Beispiele' +
        ((smp.face_neg || smp.app_neg) ? ', ' + ((smp.face_neg || 0) + (smp.app_neg || 0)) + ' Gegenbeispiele aus Korrekturen' : ''))),
    el('h4', null, 'Bilder – zum Korrigieren auswählen'), grid, bar));
}

/* Kategorien */
$('#oCats').onclick = openCats;
async function openCats() {
  await loadCats();
  const list = el('div', {class: 'evs'});
  const draw = () => {
    list.innerHTML = '';
    if (!S.cats.length) list.append(el('div', {class: 'muted'}, 'Noch keine Kategorien.'));
    S.cats.forEach(c => {
      const n = el('input', {type: 'text', value: c.name, style: {flex: 1}});
      const col = el('input', {type: 'color', value: c.color || '#ff9142'});
      list.append(el('div', {class: 'ev ack', style: {borderLeftColor: c.color, opacity: 1}}, n, col,
        el('span', {class: 'muted mono small'}, c.n + ' Obj.'),
        el('button', {class: 'btn-mini', onclick: async () => { await api('/api/categories', {body: {id: c.id, name: n.value, color: col.value}}); toast('Gespeichert'); await loadCats(); draw(); }}, 'Speichern'),
        el('button', {class: 'btn-mini del', onclick: async () => { if (!confirm('Kategorie „' + c.name + '“ löschen? Objekte bleiben erhalten.')) return;
          await api('/api/categories/' + c.id, {method: 'DELETE'}); await loadCats(); draw(); }}, 'Löschen')));
    });
  };
  draw();
  const nn = el('input', {type: 'text', placeholder: 'z. B. Familie, Nachbarn, Lieferdienst, Eigene Fahrzeuge', style: {flex: 1}});
  const nc = el('input', {type: 'color', value: '#3ddc97'});
  openModal(el('div', null, el('h2', null, 'Kategorien'),
    el('div', {class: 'bar'}, nn, nc, el('button', {class: 'btn', onclick: async () => {
      if (!nn.value.trim()) return; await api('/api/categories', {body: {name: nn.value, color: nc.value}}); nn.value = ''; await loadCats(); draw();
    }}, '+ Anlegen')), list), true);
  $('#modal').addEventListener('mousedown', () => { if ($('#modal').hidden && S.tab === 'objects') loadObjects(); }, {once: true});
}

/* ------------------------------------------------------------ Alarme */
const TT = {class: 'Art / Gruppe', category: 'Kategorie', object: 'Bestimmtes Objekt', any: 'Alle Objekte'};
function targetText(r) {
  if (r.target_type === 'any') return 'alle Objekte';
  if (r.target_type === 'class') {
    if (String(r.target).startsWith('grp:')) { const g = S.meta.groups.find(x => 'grp:' + x.id === r.target); return g ? g.name : r.target; }
    const c = S.meta.classes.find(x => x.id === r.target); return c ? c.de : r.target;
  }
  if (r.target_type === 'category') { const c = S.cats.find(x => String(x.id) === String(r.target)); return 'Kategorie ' + (c ? '„' + c.name + '“' : '?'); }
  if (r.target_type === 'object') return 'Objekt #' + r.target + (S.objCache[r.target] ? ' „' + S.objCache[r.target] + '“' : '');
  return r.target;
}
function ruleText(r) {
  const per = r.count_mode !== 'total' || r.target_type === 'object';
  const nm = ['', ' (nur benannte)', ' (nur unbenannte)'][r.only_named || 0];
  return (per ? 'Wenn dasselbe Objekt aus ' : 'Wenn insgesamt Objekte aus ') + targetText(r) + nm + ' mindestens ' + r.min_count + '× in ' +
    r.window_min + ' min erkannt wird' + (r.min_streams > 1 ? ', in mindestens ' + r.min_streams + ' verschiedenen Streams' : '') +
    '. Pause danach: ' + r.cooldown_min + ' min.';
}
async function loadAlarms() {
  await loadCats();
  try {
    const named = await api('/api/objects?named=1&limit=1000');
    named.forEach(o => S.objCache[o.id] = o.label);
  } catch (e) {}
  const [evs, rules] = await Promise.all([api('/api/events?limit=200'), api('/api/alarms')]);
  const box = $('#evs'); box.innerHTML = '';
  const show = $('#evAll').checked ? evs : evs.filter(e => !e.ack);
  if (!show.length) box.append(el('div', {class: 'muted small', style: {padding: '6px 2px'}}, $('#evAll').checked ? 'Noch keine Alarme.' : 'Keine offenen Alarme.'));
  show.forEach(e => box.append(el('div', {class: 'ev' + (e.ack ? ' ack' : '')},
    el('div', {class: 'th', style: {backgroundImage: e.crop ? 'url(' + e.crop + ')' : 'none'}, onclick: () => e.det_id && openDetection(e.det_id)}),
    el('div', {class: 'tx'}, el('b', null, e.text), el('span', null, fmtTime(e.ts, 'full') + ' · Regel „' + e.alarm_name + '“')),
    e.object_id ? el('button', {class: 'btn-mini', onclick: () => openObject(e.object_id)}, 'Objekt') : null,
    e.ack ? null : el('button', {class: 'btn-mini', onclick: async () => { await api('/api/events/ack', {body: {ids: [e.id]}}); loadAlarms(); pollState(); }}, 'Quittieren'))));
  const rb = $('#rules'); rb.innerHTML = '';
  if (!rules.length) rb.append(el('div', {class: 'empty', style: {gridColumn: '1/-1'}}, el('b', null, 'Noch keine Alarmregel'),
    'Beispiel: „Dieselbe Person 3× in 30 Minuten in mindestens 2 Streams“.'));
  rules.forEach(r => {
    const tg = el('input', {type: 'checkbox', checked: !!r.enabled});
    tg.onchange = async () => { await api('/api/alarms', {body: Object.assign({}, r, {enabled: tg.checked})}); loadAlarms(); };
    rb.append(el('div', {class: 'rule' + (r.enabled ? '' : ' off')},
      el('h3', null, tg, r.name), el('p', null, ruleText(r)),
      el('p', null, 'Meldung: ' + ['Oberfläche', r.sound ? 'Ton' : null, r.mqtt ? 'MQTT' : null, r.webhook ? 'Webhook' : null].filter(Boolean).join(', ')),
      el('div', {class: 'row'},
        el('button', {class: 'btn-mini', onclick: () => editRule(r)}, 'Bearbeiten'),
        el('button', {class: 'btn-mini', onclick: async () => { await api('/api/alarms/' + r.id + '/test', {body: {}}); toast('Testalarm ausgelöst'); }}, 'Testen'),
        el('button', {class: 'btn-mini del', onclick: async () => { if (!confirm('Regel „' + r.name + '“ löschen?')) return; await api('/api/alarms/' + r.id, {method: 'DELETE'}); loadAlarms(); }}, 'Löschen'))));
  });
}
$('#evAll').onchange = loadAlarms;
$('#evAck').onclick = async () => { await api('/api/events/ack', {body: {all: true}}); loadAlarms(); pollState(); };
$('#evClear').onclick = async () => { if (!confirm('Gesamten Alarmverlauf löschen?')) return; await api('/api/events/clear', {body: {}}); loadAlarms(); pollState(); };
$('#ruleNew').onclick = () => editRule(null);
async function editRule(r) {
  r = r || {name: 'Person mehrfach erkannt', enabled: 1, target_type: 'class', target: 'person', count_mode: 'per_object', min_count: 3,
    window_min: 30, min_streams: 1, cooldown_min: 30, only_named: 0, mqtt: 1, webhook: '', sound: 1};
  const f = {};
  f.name = el('input', {type: 'text', value: r.name});
  f.tt = el('select'); Object.entries(TT).forEach(([k, v]) => f.tt.append(el('option', {value: k}, v))); f.tt.value = r.target_type;
  f.tg = el('select');
  f.cm = el('select', null, el('option', {value: 'per_object'}, 'jedes Objekt einzeln zählen (z. B. dieselbe Person)'),
    el('option', {value: 'total'}, 'alle passenden Objekte zusammen zählen')); f.cm.value = r.count_mode;
  f.on = el('select', null, el('option', {value: 0}, 'alle'), el('option', {value: 1}, 'nur Objekte mit Namen'), el('option', {value: 2}, 'nur Objekte ohne Namen (Unbekannte)'));
  f.on.value = r.only_named || 0;
  f.mc = el('input', {type: 'number', min: 1, value: r.min_count, style: {width: '100px'}});
  f.wm = el('input', {type: 'number', min: 1, value: r.window_min, style: {width: '100px'}});
  f.ms = el('input', {type: 'number', min: 1, value: r.min_streams, style: {width: '100px'}});
  f.cd = el('input', {type: 'number', min: 0, value: r.cooldown_min, style: {width: '100px'}});
  f.snd = el('input', {type: 'checkbox', checked: !!r.sound});
  f.mq = el('input', {type: 'checkbox', checked: !!r.mqtt});
  f.wh = el('input', {type: 'text', value: r.webhook || '', placeholder: 'http://… (optional, JSON per POST)'});
  const fillTarget = async () => {
    f.tg.innerHTML = ''; f.tg.disabled = false;
    if (f.tt.value === 'class') {
      const og = el('optgroup', {label: 'Gruppen'}); S.meta.groups.forEach(g => og.append(el('option', {value: 'grp:' + g.id}, g.name))); f.tg.append(og);
      const oc = el('optgroup', {label: 'Einzelne Arten'}); S.meta.classes.forEach(c => oc.append(el('option', {value: c.id}, c.de))); f.tg.append(oc);
    } else if (f.tt.value === 'category') {
      S.cats.forEach(c => f.tg.append(el('option', {value: c.id}, c.name)));
      if (!S.cats.length) f.tg.append(el('option', {value: ''}, '(erst unter Objekte → Kategorien anlegen)'));
    } else if (f.tt.value === 'object') {
      const list = await api('/api/objects?sort=name&limit=1000&min=1');
      list.forEach(o => f.tg.append(el('option', {value: o.id}, o.label + ' – ' + o.cls_de + ' (' + o.sightings + '×)')));
    } else { f.tg.append(el('option', {value: ''}, '–')); f.tg.disabled = true; }
    if ([...f.tg.options].some(o => o.value === String(r.target))) f.tg.value = String(r.target);
  };
  f.tt.onchange = () => { r.target = ''; fillTarget(); };
  await fillTarget();
  openModal(el('div', null, el('h2', null, r.id ? 'Alarmregel bearbeiten' : 'Neue Alarmregel'),
    el('div', {class: 'form'},
      el('label', null, 'Name'), f.name,
      el('label', null, 'Gilt für'), f.tt,
      el('label', null, 'Auswahl'), f.tg,
      el('label', null, 'Zählweise'), f.cm,
      el('label', null, 'Objekte'), f.on,
      el('label', null, 'Mindestens erkannt'), el('div', null, f.mc, ' mal'),
      el('label', null, 'innerhalb von'), el('div', null, f.wm, ' Minuten'),
      el('label', null, 'in mindestens'), el('div', null, f.ms, ' verschiedenen Streams'),
      el('div', {class: 'hint'}, 'Eine Sichtung zählt, sobald das Objekt mehrmals hintereinander erkannt wurde; eine durchgehende Anwesenheit zählt einmal.'),
      el('label', null, 'Pause nach Alarm'), el('div', null, f.cd, ' Minuten'),
      el('label', null, 'Meldung'), el('div', {style: {display: 'flex', gap: '16px', flexWrap: 'wrap'}},
        el('label', {class: 'chk'}, f.snd, 'Ton im Browser'), el('label', {class: 'chk'}, f.mq, 'MQTT (Einstellungen → MQTT)')),
      el('label', null, 'Webhook'), f.wh),
    el('div', {class: 'actions'}, el('button', {class: 'btn', onclick: async () => {
      try {
        await api('/api/alarms', {body: {id: r.id, name: f.name.value, enabled: r.enabled === undefined ? 1 : r.enabled, target_type: f.tt.value,
          target: f.tg.value, count_mode: f.cm.value, only_named: +f.on.value, min_count: +f.mc.value, window_min: +f.wm.value,
          min_streams: +f.ms.value, cooldown_min: +f.cd.value, sound: f.snd.checked, mqtt: f.mq.checked, webhook: f.wh.value}});
        toast('Regel gespeichert'); closeModal(); loadAlarms();
      } catch (e) { toast(e.message); }
    }}, 'Speichern'), el('button', {class: 'btn btn-ghost', onclick: closeModal}, 'Abbrechen'))), true);
}

/* ------------------------------------------------------------ Einstellungen */
function numIn(v, attrs) { return el('input', Object.assign({type: 'number', value: v, style: {width: '110px'}}, attrs || {})); }
async function saveSection(section, values, msg) {
  try { await api('/api/config', {body: {section, values}}); S.cfg = await api('/api/config'); toast(msg || 'Gespeichert'); renderLegend(); }
  catch (e) { toast(e.message); }
}
function streamEditor(list) {
  const wrap = el('div');
  const draw = () => {
    wrap.innerHTML = '';
    if (!list.length) wrap.append(el('div', {class: 'muted small', style: {marginBottom: '10px'}}, 'Noch kein Stream eingetragen.'));
    list.forEach((s, i) => {
      const nm = el('input', {type: 'text', value: s.name || '', placeholder: 'Name (z. B. Einfahrt)'}); nm.oninput = () => s.name = nm.value;
      const url = el('input', {type: 'text', value: s.url || '', placeholder: 'rtsp://benutzer:passwort@192.168.200.50:554/stream2'}); url.oninput = () => s.url = url.value;
      const en = el('input', {type: 'checkbox', checked: s.enabled !== false}); en.onchange = () => s.enabled = en.checked;
      const dt = el('input', {type: 'checkbox', checked: s.detect !== false}); dt.onchange = () => s.detect = dt.checked;
      const kf = el('input', {type: 'checkbox', checked: !!s.keyframes_only}); kf.onchange = () => s.keyframes_only = kf.checked;
      const fps = numIn(s.fps || 2, {min: .2, max: 15, step: .2, class: 'num'}); fps.oninput = () => s.fps = +fps.value;
      const w = numIn(s.width || 640, {min: 160, max: 1920, step: 16, class: 'num'}); w.oninput = () => s.width = +w.value;
      const tr = el('select', null, el('option', {value: 'tcp'}, 'TCP'), el('option', {value: 'udp'}, 'UDP')); tr.value = s.transport || 'tcp'; tr.onchange = () => s.transport = tr.value;
      const dc = el('select', {title: 'Dekoder'}, el('option', {value: 'auto'}, 'Dekoder: automatisch'), el('option', {value: 'nvdec'}, 'Hardware (Jetson NVDEC)'),
        el('option', {value: 'gst'}, 'GStreamer (Software)'), el('option', {value: 'ffmpeg'}, 'ffmpeg')); dc.value = s.decoder || 'auto'; dc.onchange = () => s.decoder = dc.value;
      const st = (S.state.streams.find(x => x.id === s.id) || {});
      wrap.append(el('div', {class: 'stream-ed'},
        el('div', {class: 'r'}, el('span', {class: 'status-dot ' + (st.online ? 'online' : s.id ? '' : 'idle'), id: 'sdot-' + (s.id || 'n' + i)}), nm,
          el('button', {class: 'btn-mini del', onclick: () => { if (confirm('Stream „' + (s.name || '') + '“ entfernen?')) { list.splice(i, 1); draw(); } }}, 'Entfernen')),
        el('div', {class: 'r'}, url),
        el('div', {class: 'r small'}, el('label', {class: 'chk'}, en, 'aktiv'), el('label', {class: 'chk'}, dt, 'Objekterkennung'),
          el('span', {class: 'muted'}, 'Bilder/s'), fps, el('span', {class: 'muted'}, 'Breite'), w, tr, dc,
          el('label', {class: 'chk', title: 'Nur Schlüsselbilder dekodieren – spart viel Rechenleistung, Live-Bild ruckelt'}, kf, 'nur Keyframes')),
        st.error ? el('div', {class: 'muted mono small', style: {color: 'var(--danger)'}}, st.error) : null));
    });
  };
  draw();
  return {wrap, draw};
}
async function loadSettings() {
  S.cfg = await api('/api/config');
  await loadCats();
  const C = S.cfg, box = $('#settings'); box.innerHTML = '';
  /* Streams */
  const streams = JSON.parse(JSON.stringify(C.streams));
  const se = streamEditor(streams);
  box.append(el('div', {class: 'panel', style: {gridColumn: '1/-1'}}, el('h3', null, 'Streams (Kameras)'), se.wrap,
    el('div', {class: 'actions'},
      el('button', {class: 'btn btn-ghost', onclick: () => { streams.push({name: 'Kamera ' + (streams.length + 1), url: '', enabled: true, detect: true, fps: S.state.jetson ? 4 : 2, width: 640, decoder: 'auto'}); se.draw(); }}, '+ Stream hinzufügen'),
      el('button', {class: 'btn', onclick: async () => {
        try { await api('/api/streams', {body: {streams}}); toast('Streams gespeichert – verbinde …'); S.cfg = await api('/api/config'); fillSelects(); setTimeout(loadSettings, 1500); }
        catch (e) { toast(e.message); }
      }}, 'Streams speichern')),
    el('p', {class: 'muted small', style: {marginBottom: 0}}, S.state.jetson ?
      'Jetson: Videos werden per Hardware (NVDEC) dekodiert und skaliert – auch Hauptstreams (1080p, H.264/H.265) sind möglich. 2–5 Bilder/s je Kamera.' :
      'Tipp für den Brume 2: den Unter-/Substream der Kamera verwenden (z. B. 640×360). 1–2 Bilder/s reichen für die Erkennung. Die Bildbreite wird vor der Analyse verkleinert.')));
  /* Erkennung */
  const D = C.detection;
  const gl = el('div', {class: 'grp-list'});
  const gch = {};
  S.meta.groups.forEach(g => {
    gch[g.id] = el('input', {type: 'checkbox', checked: D.groups.includes(g.id)});
    gl.append(el('label', {class: 'chk g'}, gch[g.id], el('i', {style: {borderColor: g.color}}), g.name,
      el('span', {class: 'muted small'}, ' – ' + g.classes.slice(0, 6).map(c => S.meta.classes.find(x => x.id === c).de).join(', ') + (g.classes.length > 6 ? ' …' : ''))));
  });
  const extra = new Set(D.extra_classes || []);
  const cp = el('div', {class: 'cls-pick', hidden: true});
  S.meta.classes.forEach(c => { const cb = el('input', {type: 'checkbox', checked: extra.has(c.id)}); cb.onchange = () => cb.checked ? extra.add(c.id) : extra.delete(c.id);
    cp.append(el('label', {class: 'chk small'}, cb, c.de)); });
  const thr = el('input', {type: 'range', min: .2, max: .9, step: .05, value: D.threshold}); const thrv = el('span', {class: 'mono'}, Math.round(D.threshold * 100) + ' %');
  thr.oninput = () => thrv.textContent = Math.round(thr.value * 100) + ' %';
  const ms = el('select', null, el('option', {value: 320}, '320 px – schnell (empfohlen für Brume 2)'), el('option', {value: 416}, '416 px – genauer, ca. 1,7× Rechenzeit')); ms.value = D.model_size;
  const ac = el('select', null, el('option', {value: 'auto'}, 'automatisch (GPU, falls vorhanden)'), el('option', {value: 'gpu'}, 'GPU (TensorRT, Jetson)'), el('option', {value: 'cpu'}, 'nur CPU')); ac.value = D.accel || 'auto';
  const mh = numIn(D.min_hits, {min: 1, max: 10}), iv = numIn(D.interval_s, {min: .2, step: .1}), mg = el('input', {type: 'checkbox', checked: D.motion_gate});
  const ml = numIn(D.motion_level, {min: .5, step: .5}), tt = numIn(D.track_timeout_s, {min: 1, step: .5}), rs = numIn(D.resnap_s, {min: 0, step: 10});
  const mb = numIn(D.min_box_px, {min: 8}), en = el('input', {type: 'checkbox', checked: D.enabled});
  const sg = numIn(D.sighting_gap_s, {min: 0, step: 10});
  box.append(el('div', {class: 'panel'}, el('h3', null, 'Objekterkennung'),
    el('div', {class: 'form'},
      el('label', null, 'Erkennung aktiv'), el('label', {class: 'chk'}, en, 'Streams analysieren'),
      el('label', null, 'Erkennen'), gl,
      el('label', null, 'Weitere Arten'), el('div', null, el('button', {class: 'btn-mini', onclick: () => cp.hidden = !cp.hidden}, 'Einzelne Arten wählen (' + extra.size + ')'), cp),
      el('label', null, 'Mindest-Sicherheit'), el('div', {style: {display: 'flex', gap: '10px', alignItems: 'center'}}, thr, thrv),
      el('label', null, 'Rechenwerk'), ac,
      el('label', null, 'Modellgröße'), ms, el('div', {class: 'hint'}, 'Auf der Jetson-GPU wird immer mit 416 px gerechnet.'),
      el('label', null, 'Sichtung ab'), el('div', null, mh, ' Treffern hintereinander'),
      el('label', null, 'Analyse-Abstand'), el('div', null, iv, ' s je Stream'),
      el('label', null, 'Bewegungsfilter'), el('div', null, el('label', {class: 'chk'}, mg, 'nur bei Bewegung analysieren'), ' Schwelle ', ml),
      el('label', null, 'Objekt verschwunden nach'), el('div', null, tt, ' s'),
      el('label', null, 'Neue Sichtung nach'), el('div', null, sg, ' s Abwesenheit im selben Stream'),
      el('label', null, 'Weiteres Bild alle'), el('div', null, rs, ' s bei längerer Anwesenheit (0 = nie)'),
      el('label', null, 'Mindestgröße'), el('div', null, mb, ' px Höhe')),
    el('div', {class: 'actions'}, el('button', {class: 'btn', onclick: () => saveSection('detection', {
      enabled: en.checked, groups: Object.keys(gch).filter(k => gch[k].checked), extra_classes: [...extra], threshold: +thr.value,
      model_size: +ms.value, accel: ac.value, min_hits: +mh.value, interval_s: +iv.value, motion_gate: mg.checked, motion_level: +ml.value,
      track_timeout_s: +tt.value, sighting_gap_s: +sg.value, resnap_s: +rs.value, min_box_px: +mb.value})}, 'Speichern'))));
  /* Wiedererkennung */
  const I = C.identity;
  const fa = el('input', {type: 'checkbox', checked: I.faces}), ft = numIn(I.face_threshold, {min: .2, max: .9, step: .01});
  const fm = numIn(I.face_min_px, {min: 12}), at = numIn(I.appearance_threshold, {min: .5, max: .99, step: .01});
  const aw = numIn(I.appearance_window_h, {min: 0, step: 1}), ao = numIn(I.appearance_window_other_h, {min: 0, step: 1}), mx = numIn(I.max_samples, {min: 5});
  box.append(el('div', {class: 'panel'}, el('h3', null, 'Wiedererkennung'),
    el('p', {class: 'muted small', style: {marginTop: 0}}, 'Personen werden über das Gesicht (SFace) einzeln wiedererkannt, ohne sichtbares Gesicht über Kleidung/Farben. ',
      'Andere Objekte (Tiere, Fahrzeuge) über ihr Aussehen. Korrekturen in der Galerie oder beim Objekt verbessern die Zuordnung.'),
    el('div', {class: 'form'},
      el('label', null, 'Gesichter'), el('label', {class: 'chk'}, fa, 'Gesichter suchen und vergleichen'),
      el('label', null, 'Gesicht gleich ab'), ft, el('div', {class: 'hint'}, 'Ähnlichkeit 0–1; höher = strenger (Standard 0,40)'),
      el('label', null, 'Gesicht mind.'), el('div', null, fm, ' px breit'),
      el('label', null, 'Aussehen gleich ab'), at, el('div', {class: 'hint'}, 'höher = weniger Verwechslungen, aber mehr neue Objekte (Standard 0,90)'),
      el('label', null, 'Personen über Aussehen'), el('div', null, aw, ' h nach letzter Sichtung'),
      el('label', null, 'Andere über Aussehen'), el('div', null, ao, ' h nach letzter Sichtung'),
      el('label', null, 'Lernbeispiele je Objekt'), mx),
    el('div', {class: 'actions'}, el('button', {class: 'btn', onclick: () => saveSection('identity', {faces: fa.checked, face_threshold: +ft.value,
      face_min_px: +fm.value, appearance_threshold: +at.value, appearance_window_h: +aw.value, appearance_window_other_h: +ao.value, max_samples: +mx.value})}, 'Speichern'))));
  /* Alarme / MQTT */
  const M = C.mqtt;
  const me = el('input', {type: 'checkbox', checked: M.enabled}), mhst = el('input', {type: 'text', value: M.host}), mp = numIn(M.port, {min: 1, max: 65535});
  const mu = el('input', {type: 'text', value: M.user, autocomplete: 'off'}), mpw = el('input', {type: 'password', value: M.password, autocomplete: 'new-password'});
  const mpre = el('input', {type: 'text', value: M.prefix}), mdet = el('input', {type: 'checkbox', checked: M.publish_detections});
  box.append(el('div', {class: 'panel'}, el('h3', null, 'MQTT'),
    el('div', {class: 'form'},
      el('label', null, 'MQTT'), el('label', {class: 'chk'}, me, 'Alarme an MQTT-Broker senden'),
      el('label', null, 'Broker'), el('div', {style: {display: 'flex', gap: '8px'}}, mhst, mp),
      el('div', {class: 'hint'}, 'Auf dem Netzwerk-Core läuft Mosquitto auf demselben Gerät: 127.0.0.1 : 1883'),
      el('label', null, 'Benutzer'), mu, el('label', null, 'Passwort'), mpw,
      el('label', null, 'Topic-Präfix'), mpre, el('div', {class: 'hint'}, 'Alarme: <präfix>/alarm · Erkennungen: <präfix>/detection'),
      el('label', null, 'Erkennungen'), el('label', {class: 'chk'}, mdet, 'jede Sichtung ebenfalls senden')),
    el('div', {class: 'actions'},
      el('button', {class: 'btn', onclick: () => saveSection('mqtt', {enabled: me.checked, host: mhst.value, port: +mp.value, user: mu.value, password: mpw.value,
        prefix: mpre.value, publish_detections: mdet.checked})}, 'Speichern'),
      el('button', {class: 'btn btn-ghost', onclick: async () => { const r = await api('/api/mqtt/test', {body: {}}); toast(r.ok ? 'Testnachricht gesendet' : 'Fehler: ' + r.error); }}, 'Test senden'))));
  /* Speicher + Oberflaeche */
  const T = C.storage, U = C.ui;
  const md = numIn(T.max_days, {min: 0}), mm = numIn(T.max_mb, {min: 0, step: 100});
  const snd = el('input', {type: 'checkbox', checked: U.sound}), rl = numIn(U.repeat_list_min, {min: 1});
  const usage = el('span', {class: 'mono', id: 'diskUse'}, '…');
  api('/api/stats').then(s => usage.textContent = s.disk_mb + ' MB · ' + s.detections + ' Bilder · ' + s.objects + ' Objekte').catch(() => {});
  box.append(el('div', {class: 'panel'}, el('h3', null, 'Speicher & Oberfläche'),
    el('div', {class: 'form'},
      el('label', null, 'Belegt'), usage,
      el('label', null, 'Bilder behalten'), el('div', null, md, ' Tage (0 = unbegrenzt)'),
      el('label', null, 'Höchstens'), el('div', null, mm, ' MB (älteste zuerst löschen)'),
      el('div', {class: 'hint'}, 'Gelernte Personen/Objekte bleiben auch nach dem Löschen alter Bilder erhalten.'),
      el('label', null, 'Alarmton'), el('label', {class: 'chk'}, snd, 'Ton in der Oberfläche abspielen'),
      el('label', null, 'Objektliste ab'), el('div', null, rl, ' Sichtungen („mehrfach erkannt“)')),
    el('div', {class: 'actions'},
      el('button', {class: 'btn', onclick: async () => { await saveSection('storage', {max_days: +md.value, max_mb: +mm.value}, ' '); await saveSection('ui', {sound: snd.checked, repeat_list_min: +rl.value}); }}, 'Speichern'),
      el('button', {class: 'btn btn-ghost', onclick: async () => { await api('/api/cleanup', {body: {}}); toast('Aufräumen gestartet'); }}, 'Jetzt aufräumen'))));
  /* System */
  const port = numIn(S.state.port, {min: 1, max: 65535});
  const mt = el('table', {class: 'mtab', id: 'mtab'});
  const lg = el('pre', {class: 'log', id: 'logv'}, '…');
  box.append(el('div', {class: 'panel'}, el('h3', null, 'System'),
    el('div', {class: 'form'},
      el('label', null, 'Version'), el('span', {class: 'mono'}, 'Sentinel MK1 v' + S.state.version),
      el('label', null, 'Gerät'), el('span', {class: 'mono'}, S.state.platform + (S.state.l4t ? ' · ' + S.state.l4t : '') + (S.state.trt_version ? ' · TensorRT ' + S.state.trt_version : '')),
      el('label', null, 'Video-Dekoder'), el('span', {class: 'mono'}, S.state.decoder),
      el('label', null, 'KI-Laufzeit'), el('span', {class: 'mono', id: 'sysAi'}),
      el('label', null, 'Web-Port'), S.state.port_locked ? el('span', {class: 'mono'}, S.state.port + ' (per Startparameter festgelegt)') :
        el('div', {style: {display: 'flex', gap: '8px', alignItems: 'center'}}, port, el('button', {class: 'btn-mini', onclick: async () => {
          await saveSection('server', {port: +port.value}, 'Port gespeichert – Neustart …');
          await api('/api/restart', {body: {}});
          setTimeout(() => location.href = location.protocol + '//' + location.hostname + ':' + port.value + '/', 4000);
        }}, 'Übernehmen + Neustart'))),
    el('h4', null, 'KI-Modelle'), mt,
    el('div', {class: 'actions'}, el('button', {class: 'btn-mini', onclick: async () => { await api('/api/models/download', {body: {}}); toast('Download gestartet'); }}, 'Fehlende Modelle laden'),
      el('button', {class: 'btn-mini', onclick: async () => { if (!confirm('Sentinel neu starten?')) return; await api('/api/restart', {body: {}}); toast('Neustart …'); }}, 'Neu starten')),
    el('h4', null, 'Protokoll'), lg));
  updateSettingsStatus();
}
async function updateSettingsStatus() {
  const st = S.state; if (!st) return;
  const ai = $('#sysAi');
  if (ai) ai.textContent = st.models_ready ? st.backend + ' · ' + st.model_size + ' px · ' + (st.det_ms != null ? st.det_ms + ' ms/Bild' : '–') +
    (st.face_ms != null ? ' · Gesicht ' + st.face_ms + ' ms' : '') + (st.face_gpu ? ' (GPU)' : '') + (st.building ? ' · GPU-Engines werden gebaut …' : '') : (st.error || 'Modelle werden geladen …');
  const mt = $('#mtab');
  if (mt) { mt.innerHTML = ''; st.models.forEach(m => mt.append(el('tr', null, el('td', null, m.title), el('td', {class: 'muted small'}, (m.size / 1048576).toFixed(1) + ' MB · ' + m.license),
    el('td', {style: {color: m.state === 'ok' ? 'var(--accent-2)' : m.state === 'fehler' ? 'var(--danger)' : 'var(--accent)'}}, m.state === 'lädt' ? 'lädt ' + m.progress + ' %' : m.state))));
    (st.engines || []).forEach(m => mt.append(el('tr', null, el('td', null, 'GPU-Engine ' + m.file), el('td', {class: 'muted small'}, 'TensorRT FP16'),
      el('td', {style: {color: m.state === 'ok' ? 'var(--accent-2)' : m.state === 'fehler' ? 'var(--danger)' : 'var(--accent)'}}, m.state === 'baut' ? 'wird gebaut …' : m.state)))); }
  st.streams.forEach(s => { const d = $('#sdot-' + s.id); if (d) d.className = 'status-dot ' + (s.online ? 'online' : ''); });
  const lg = $('#logv');
  if (lg) { try { const l = await api('/api/log'); const atBottom = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 20;
    lg.textContent = l.join('\n') || '–'; if (atBottom) lg.scrollTop = lg.scrollHeight; } catch (e) {} }
}

/* ------------------------------------------------------------ Start */
(async function init() {
  try {
    S.meta = await api('/api/meta');
    S.cfg = await api('/api/config');
    await loadCats();
    await pollState();
    renderLegend();
  } catch (e) { toast('Keine Verbindung: ' + e.message, '', 6000); }
  let t = 'live';
  try { t = localStorage.getItem('sentinel_tab') || 'live'; } catch (e) {}
  if (location.hash.length > 1) t = location.hash.slice(1);
  showTab(['live', 'gallery', 'objects', 'alarms', 'settings'].includes(t) ? t : 'live');
  setInterval(pollState, 2000);
  setInterval(pollLive, 900);
})();
