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
  if (t === 'events') { initEvents(); loadEvents(true); }
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
    if (st.fan && st.fan.available && st.fan.pct != null) h.append(el('span', {title: 'Lüfter' + (st.fan.rpm ? ' ' + st.fan.rpm + ' U/min' : '')},
      el('span', {class: 'fan-ico' + (st.fan.pct > 0 ? ' spin' : ''), style: {animationDuration: (st.fan.pct > 0 ? Math.max(0.25, 1.6 - st.fan.pct / 70) : 0) + 's'}}, '✲'),
      ' Lüfter ', el('b', null, st.fan.pct + ' %')));
    if (st.sys && st.sys.mem_total) h.append(el('span', null, 'RAM ', el('b', null, (st.sys.mem_used / 1024).toFixed(1) + '/' + (st.sys.mem_total / 1024).toFixed(1) + ' GB')));
    h.append(el('span', {class: 'clock'}, el('b', null, st.time_text)));
    if (st.update && (st.update.available || st.update.busy)) h.prepend(el('button', {class: 'btn-mini on', onclick: () => { showTab('settings'); setTimeout(() => { const u = $('#updPanel'); if (u) u.scrollIntoView({behavior: 'smooth'}); }, 400); }},
      st.update.busy ? '⟳ Update läuft …' : '⬆ Update v' + st.update.remote));
    if (S.updating && st.version !== S.updating) { toast('Update auf v' + st.version + ' installiert'); setTimeout(() => location.reload(), 1200); S.updating = null; }
    const pill = $('#pill'); pill.hidden = !st.unack; pill.textContent = st.unack;
    if (S.alarmSeq !== null && st.alarm_seq > S.alarmSeq) newAlarm();
    S.alarmSeq = st.alarm_seq;
    if (S.tab === 'live') renderLive(false);
    if (S.tab === 'settings') updateSettingsStatus();
  } catch (e) {
    $('#hstat').innerHTML = '';
    $('#hstat').append(el('span', null, el('span', {class: 'status-dot' + (S.updating ? ' warn' : '')}), S.updating ? ' Neustart nach Update …' : ' Verbindung zum Gerät unterbrochen'));
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
      (L ? L.boxes : []).filter(b => b.confirmed && b.age <= 0.3 && b.cls !== 'motion').forEach(b => { const g = groupOf(b.cls); cnt[g] = (cnt[g] || 0) + 1; });
      const keys = Object.keys(cnt);
      if (!keys.length) foot.append(el('span', {class: 'muted mono small'}, L ? (L.motion ? (L.idle ? 'nur Bewegungserkennung · ruhig' : 'nur Bewegungserkennung') : L.idle ? 'keine Bewegung' : 'nichts erkannt') : 'warte auf Analyse …'));
      if (s.recording) foot.append(el('span', {class: 'chip rec-chip'}, '● REC'));
      if (L && L.boxes.some(b => b.cls === 'motion')) foot.append(el('span', {class: 'chip'}, el('i', {style: {background: '#3ecf8e'}}), 'Bewegung'));
      keys.forEach(g => { const gg = S.meta.groups.find(x => x.id === g); if (!gg) return; foot.append(el('span', {class: 'chip'}, el('i', {style: {background: gg.color}}), gg.name + ': ' + cnt[g])); });
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
  if ($('#gNamed').value) p.set('named', $('#gNamed').value);
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
  const c = el('div', {class: 'card', 'data-id': d.id, onclick: e => { if (e.target.type === 'checkbox') return;
    if (o.toggle) { o.toggle(d.id, c); return; } (o.onclick || openDetection)(d.id); }},
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
    r.items.forEach(d => g.append(detCard(d, S.gselMode ? {select: S.gsel, onsel: redrawGalSel,
      toggle: id => { S.gsel.has(id) ? S.gsel.delete(id) : S.gsel.add(id); redrawGalSel(); }} : null)));
    S.gal.items = S.gal.items.concat(r.items);
    if (S.gselMode) redrawGalSel();
    if (r.items.length) S.gal.last = r.items[r.items.length - 1].id;
    $('#gMore').hidden = S.gal.items.length >= r.total || !r.items.length;
    $('#gCount').textContent = r.total + ' Bilder';
  } catch (e) { toast(e.message); }
}
['#gStream', '#gGroup', '#gCat', '#gSight', '#gNamed', '#gDate'].forEach(id => $(id).onchange = () => loadGallery(true));
$('#gMore').onclick = () => loadGallery(false);
$('#gSelBtn').onclick = () => toggleGalSel(!S.gselMode);
$('#gPurge').onclick = openPurge;

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

/* Zuweisen-Auswahl: benannte Personen/Objekte zuerst, "Neue Person anlegen" mit Namensfeld.
   cls: Art der Bilder (fuer passende Vorschlaege); null = alle Arten (gemischte Auswahl). */
function assignPicker(cls, current) {
  const sel = el('select', {class: 'apick'});
  const name = el('input', {type: 'text', placeholder: 'Name, z. B. „Paketbote“', style: {display: 'none'}});
  const cat = catSelect('', () => {}); cat.style.display = 'none';
  const wrap = el('div', {class: 'assign'}, sel, name, cat);
  wrap.ready = (async () => {
    let list = await api('/api/objects?sort=name&limit=1000' + (cls ? '&group=' + encodeURIComponent(groupOf(cls)) : ''));
    if (cls) { const tk = c => ({truck: 'car', bus: 'car'}[c] || c); list = list.filter(o => tk(o.cls) === tk(cls)); }
    sel.innerHTML = '';
    sel.append(el('option', {value: ''}, '— zuweisen an … —'));
    sel.append(el('option', {value: 'new'}, '➕ ' + (cls && cls !== 'person' ? 'Neues Objekt' : 'Neue Person') + ' anlegen …'));
    const named = list.filter(o => o.name), un = list.filter(o => !o.name).sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0)).slice(0, 60);
    const byCat = {};
    named.forEach(o => { const k = o.category || 'Ohne Kategorie'; (byCat[k] = byCat[k] || []).push(o); });
    Object.keys(byCat).sort((a, b) => a === 'Ohne Kategorie' ? 1 : b === 'Ohne Kategorie' ? -1 : a.localeCompare(b)).forEach(k => {
      const og = el('optgroup', {label: k});
      byCat[k].forEach(o => og.append(el('option', {value: o.id, disabled: o.id === current}, (cls ? '' : o.cls_de + ': ') + o.label + ' (' + o.sightings + '×)' + (o.id === current ? ' – aktuell' : ''))));
      sel.append(og);
    });
    if (un.length) { const og = el('optgroup', {label: 'Unbenannt (zuletzt gesehen)'});
      un.forEach(o => og.append(el('option', {value: o.id, disabled: o.id === current}, (cls ? '' : o.cls_de + ': ') + o.label + ' (' + o.sightings + '×)'))); sel.append(og); }
  })();
  sel.onchange = () => { const n = sel.value === 'new'; name.style.display = n ? '' : 'none'; cat.style.display = n ? '' : 'none'; if (n) name.focus(); };
  wrap.value = () => ({target: sel.value, name: sel.value === 'new' ? name.value.trim() : '', category_id: sel.value === 'new' ? (cat.value || null) : null});
  return wrap;
}
async function doAssign(ids, pick, wholeTrack) {
  const v = pick.value();
  if (!v.target) { toast('Bitte auswählen, wem die Bilder gehören'); return null; }
  if (v.target === 'new' && !v.name) { toast('Bitte einen Namen eingeben'); return null; }
  try {
    const r = await api('/api/detections/assign', {body: {ids: ids, whole_track: !!wholeTrack, target: v.target === 'new' ? 'new' : +v.target, name: v.name, category_id: v.category_id}});
    toast(r.moved + ' Bild(er) zugewiesen' + (r.skipped ? ' · ' + r.skipped + ' übersprungen (andere Art)' : '') + ' – Sentinel lernt daraus');
    return r;
  } catch (e) { toast(e.message); return null; }
}

/* Galerie: Auswahlmodus mit Sammelaktionen */
S.gsel = new Set(); S.gselMode = false;
function galSelBar() {
  let bar = $('#gSelBar');
  if (!S.gselMode) { if (bar) bar.remove(); return; }
  if (!bar) {
    const pick = assignPicker(null, null);
    bar = el('div', {class: 'selbar gsel', id: 'gSelBar'},
      el('b', {id: 'gSelCnt'}), el('button', {class: 'btn-mini', onclick: () => { S.gal.items.forEach(d => S.gsel.add(d.id)); redrawGalSel(); }}, 'Alle sichtbaren'),
      el('button', {class: 'btn-mini', onclick: () => { S.gsel.clear(); redrawGalSel(); }}, 'Keine'),
      el('span', {class: 'sep'}), pick,
      el('button', {class: 'btn', onclick: async () => { if (!S.gsel.size) return toast('Nichts ausgewählt');
        const r = await doAssign([...S.gsel], pick); if (r) { S.gsel.clear(); loadGallery(true); } }}, 'Zuweisen'),
      el('button', {class: 'btn btn-danger', title: 'Kein echtes Objekt (z. B. Schatten)', onclick: async () => { if (!S.gsel.size) return toast('Nichts ausgewählt');
        if (!confirm(S.gsel.size + ' Bild(er) als Fehlerkennung lernen und löschen?')) return;
        const r = await api('/api/detections/false', {body: {ids: [...S.gsel]}}); toast(r.learned + ' Fehlerkennung(en) gelernt'); S.gsel.clear(); loadGallery(true); }}, 'Fehlerkennung'),
      el('button', {class: 'btn btn-danger', onclick: async () => { if (!S.gsel.size) return toast('Nichts ausgewählt');
        if (!confirm(S.gsel.size + ' Bild(er) löschen?')) return;
        const r = await api('/api/detections/delete', {body: {ids: [...S.gsel]}}); toast(r.deleted + ' gelöscht'); S.gsel.clear(); loadGallery(true); }}, 'Löschen'),
      el('button', {class: 'btn-mini', onclick: () => toggleGalSel(false)}, 'Fertig'));
    $('#tab-gallery').append(bar);
  }
  $('#gSelCnt').textContent = S.gsel.size + ' ausgewählt';
}
function redrawGalSel() {
  $$('#gal .card').forEach(c => { const id = +c.dataset.id; c.classList.toggle('picked', S.gsel.has(id)); const cb = c.querySelector('input.sel'); if (cb) cb.checked = S.gsel.has(id); });
  galSelBar();
}
function toggleGalSel(on) {
  S.gselMode = on; S.gsel.clear();
  $('#gSelBtn').classList.toggle('on', on); $('#gSelBtn').textContent = on ? 'Auswahl beenden' : 'Auswählen';
  $('#gal').classList.toggle('selmode', on);
  loadGallery(true); galSelBar();
}

/* Alles loeschen */
function openPurge() {
  const obj = el('input', {type: 'checkbox'}), fp = el('input', {type: 'checkbox'}), al = el('input', {type: 'checkbox'}), ev = el('input', {type: 'checkbox', checked: true});
  const word = el('input', {type: 'text', placeholder: 'LÖSCHEN eintippen'});
  openModal(el('div', null, el('h2', null, 'Alle Erkennungen und Bilder löschen'),
    el('p', null, 'Löscht ', el('b', null, 'alle gespeicherten Erkennungen und Bilder'), ' aus der Galerie. Unbenannte, automatisch angelegte Objekte verschwinden dabei.'),
    el('div', {class: 'form', style: {gridTemplateColumns: '1fr'}},
      el('label', {class: 'chk'}, obj, 'auch alle Personen/Objekte mit Namen, Kategorie und Gelerntem löschen (kompletter Neustart)'),
      el('label', {class: 'chk'}, fp, 'auch gelernte Fehlerkennungen vergessen'),
      el('label', {class: 'chk'}, al, 'auch den Alarmverlauf löschen'),
      el('label', {class: 'chk'}, ev, 'auch Bewegungsereignisse und Videos löschen')),
    el('p', {class: 'muted small'}, 'Ohne den ersten Haken bleiben benannte Personen/Objekte samt Gelerntem erhalten und werden weiter wiedererkannt.'),
    el('div', {class: 'actions'}, word,
      el('button', {class: 'btn btn-danger', onclick: async () => {
        if (word.value.trim().toUpperCase() !== 'LÖSCHEN') { toast('Zur Sicherheit „LÖSCHEN“ eintippen'); word.focus(); return; }
        try { const r = await api('/api/purge', {body: {objects: obj.checked, falsepos: fp.checked, alarms: al.checked, events: ev.checked}});
          toast(r.deleted + ' Erkennungen gelöscht'); closeModal(); refreshCurrent(); pollState(); }
        catch (e) { toast(e.message); }
      }}, 'Endgültig löschen'),
      el('button', {class: 'btn btn-ghost', onclick: closeModal}, 'Abbrechen'))), true);
}

async function openDetection(id) {
  let d;
  try { d = await api('/api/detection/' + id); } catch (e) { return toast(e.message); }
  const info = el('div', {class: 'kv'},
    el('span', null, 'Zeit'), el('span', null, fmtTime(d.ts, 'full')),
    el('span', null, 'Stream'), el('span', null, d.stream),
    el('span', null, 'Art'), el('span', null, el('span', {class: 'clsb'}, el('i', {style: {background: d.color}}), d.cls_de + ' (' + d.group + ')')),
    el('span', null, 'Sicherheit'), el('span', null, Math.round(d.score * 100) + ' %'),
    el('span', null, 'Gehört zu'), el('span', null, d.object_id ? el('a', {href: '#', onclick: e => { e.preventDefault(); openObject(d.object_id); }}, d.object) : '–',
      d.category ? el('span', {class: 'muted'}, ' · ' + d.category) : null),
    el('span', null, 'Zuordnung'), el('span', null, d.manual ? 'von Hand' : (d.match === 'neu' ? 'neu angelegt' :
      (d.match || '–') + (d.sim ? ' (' + Math.round(d.sim * 100) + ' %)' : ''))),
    el('span', null, 'Gesicht'), el('span', null, d.face ? 'ja' : 'nein'));
  const pick = assignPicker(d.cls, d.object_id);
  const whole = el('input', {type: 'checkbox', checked: d.track_count > 1});
  const assignBox = el('div', {class: 'fixbox'},
    el('b', null, d.named ? 'Jemand anderes? Richtig zuweisen' : 'Wer ist das? Zuweisen'),
    pick,
    d.track_count > 1 ? el('label', {class: 'chk'}, whole, 'alle ' + d.track_count + ' Bilder dieser Sichtung') : null,
    el('div', {class: 'actions', style: {marginTop: 0}}, el('button', {class: 'btn', onclick: async () => {
      const r = await doAssign([d.id], pick, whole.checked && d.track_count > 1); if (r) { closeModal(); refreshCurrent(); }
    }}, 'Zuweisen')),
    el('span', {class: 'muted small'}, 'Sentinel lernt daraus: Merkmale zählen künftig für die gewählte Person und als Gegenbeispiel für die bisherige.'));
  openModal(el('div', null, el('h2', null, d.cls_de + ' · ', el('span', {class: 'acc'}, d.object || '')),
    el('div', {class: 'detail'}, shotWithBox(d), el('div', null, info, assignBox,
      el('div', {class: 'actions'},
        d.object_id ? el('button', {class: 'btn btn-ghost', onclick: () => openObject(d.object_id)}, 'Objekt öffnen') : null,
        el('button', {class: 'btn btn-danger', title: 'Kein echtes Objekt (z. B. Schatten) – Sentinel lernt daraus', onclick: async () => {
          const all = d.track_count > 1 && confirm('Alle ' + d.track_count + ' Bilder dieser Sichtung als Fehlerkennung lernen?\n(Abbrechen = nur dieses Bild)');
          try { const r = await api('/api/detection/' + d.id + '/false', {body: {whole_track: all}}); toast(r.learned + ' Fehlerkennung(en) gelernt – wird künftig ignoriert'); closeModal(); refreshCurrent(); }
          catch (e) { toast(e.message); }
        }}, 'Fehlerkennung'),
        el('a', {class: 'btn btn-ghost', href: d.img || d.crop, download: '', style: {textDecoration: 'none'}}, 'Bild laden'),
        el('button', {class: 'btn btn-danger', onclick: async () => {
          if (!confirm('Dieses Bild löschen?')) return;
          await api('/api/detection/' + d.id, {method: 'DELETE'}); toast('Gelöscht'); closeModal(); refreshCurrent();
        }}, 'Löschen'))))));
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
          el('button', {class: 'btn btn-teal', onclick: () => openAssignTo(o)}, '+ Erkennungen zuweisen'),
          el('button', {class: 'btn btn-danger', title: 'Alle Bilder dieses Objekts sind Fehltreffer (z. B. Schatten)', onclick: async () => {
            if (!confirm('„' + o.label + '“ ist kein echtes Objekt?\nAlle ' + o.detections + ' Bilder werden als Fehlerkennung gelernt und das Objekt gelöscht.')) return;
            try { const r = await api('/api/objects/' + o.id + '/false', {body: {}}); toast(r.learned + ' Fehlerkennung(en) gelernt'); closeModal(); loadObjects(); }
            catch (e) { toast(e.message); }
          }}, 'Ist Fehlerkennung'),
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
    el('h4', null, 'Bilder – zum Korrigieren auswählen'),
    o.items.length ? grid : el('div', {class: 'empty'}, el('b', null, 'Noch keine Bilder'), 'Mit „+ Erkennungen zuweisen“ Bilder aus der Galerie hinzufügen.'), bar));
}

/* Person/Objekt von Hand anlegen */
async function openNewObject() {
  const name = el('input', {type: 'text', placeholder: 'z. B. Paketbote, Nachbar Jonas, Familienauto'});
  const cls = el('select');
  const common = ['person', 'car', 'truck', 'motorcycle', 'bicycle', 'dog', 'cat', 'bird', 'horse'];
  const og1 = el('optgroup', {label: 'Häufig'}), og2 = el('optgroup', {label: 'Alle Arten'});
  common.forEach(c => { const m = S.meta.classes.find(x => x.id === c); if (m) og1.append(el('option', {value: c}, m.de)); });
  S.meta.classes.filter(c => !common.includes(c.id)).forEach(c => og2.append(el('option', {value: c.id}, c.de)));
  cls.append(og1, og2); cls.value = 'person';
  const cat = catSelect('', () => {});
  const notes = el('textarea', {placeholder: 'Notizen (optional)'});
  openModal(el('div', null, el('h2', null, 'Neue Person / neues Objekt'),
    el('div', {class: 'form'}, el('label', null, 'Name'), name, el('label', null, 'Art'), cls, el('label', null, 'Kategorie'), cat,
      el('label', null, 'Notizen'), notes),
    el('p', {class: 'muted small'}, 'Danach direkt Bilder aus der Galerie zuweisen. Je mehr Bilder (am besten mit Gesicht), desto sicherer erkennt Sentinel die Person wieder.'),
    el('div', {class: 'actions'}, el('button', {class: 'btn', onclick: async () => {
      if (!name.value.trim()) { toast('Bitte einen Namen eingeben'); name.focus(); return; }
      try {
        const r = await api('/api/objects', {body: {name: name.value.trim(), cls: cls.value, category_id: cat.value || null, notes: notes.value}});
        toast('„' + name.value.trim() + '“ angelegt'); loadObjects();
        const o = await api('/api/object/' + r.id); openAssignTo(o);
      } catch (e) { toast(e.message); }
    }}, 'Anlegen und Erkennungen zuweisen'), el('button', {class: 'btn btn-ghost', onclick: closeModal}, 'Abbrechen'))), true);
  setTimeout(() => name.focus(), 50);
}
$('#oNew').onclick = openNewObject;

/* Erkennungen einer Person/einem Objekt zuweisen (Auswahl aus der Galerie) */
async function openAssignTo(o) {
  const sel = new Set();
  const grid = el('div', {class: 'gal'});
  const only = el('select', null, el('option', {value: '0'}, 'nur noch nicht zugewiesene'), el('option', {value: ''}, 'alle (auch anderen zugewiesene)'));
  const sight = el('input', {type: 'checkbox', checked: true});
  const stream = el('select'); stream.append(el('option', {value: ''}, 'Alle Streams'));
  (S.cfg ? S.cfg.streams : []).forEach(s => stream.append(el('option', {value: s.id}, s.name)));
  const cnt = el('b'), more = el('button', {class: 'btn btn-ghost', hidden: true}, 'Weitere laden');
  const go = el('button', {class: 'btn'}, 'Zuweisen');
  let last = null;
  const upd = () => { cnt.textContent = sel.size + ' ausgewählt'; go.disabled = !sel.size; go.textContent = sel.size ? sel.size + ' Bild(er) „' + o.label + '“ zuweisen' : 'Bilder anklicken'; };
  const toggle = (id, c) => { sel.has(id) ? sel.delete(id) : sel.add(id); c.classList.toggle('picked', sel.has(id)); const cb = c.querySelector('input.sel'); if (cb) cb.checked = sel.has(id); upd(); };
  const load = async reset => {
    if (reset) { grid.innerHTML = ''; last = null; }
    const p = new URLSearchParams({like_object: o.id, exclude_object: o.id, limit: 120});
    if (only.value) p.set('named', only.value);
    if (sight.checked) p.set('sightings', '1');
    if (stream.value) p.set('stream', stream.value);
    if (last) p.set('before', last);
    const r = await api('/api/detections?' + p.toString());
    if (reset && !r.items.length) grid.append(el('div', {class: 'empty', style: {gridColumn: '1/-1'}}, el('b', null, 'Keine passenden Bilder'), 'Filter ändern oder warten, bis etwas erkannt wird.'));
    r.items.forEach(d => { const c = detCard(d, {select: sel, onsel: upd, toggle: toggle}); if (sel.has(d.id)) c.classList.add('picked'); grid.append(c); });
    if (r.items.length) last = r.items[r.items.length - 1].id;
    more.hidden = r.items.length < 120;
  };
  only.onchange = sight.onchange = stream.onchange = () => load(true);
  more.onclick = () => load(false);
  const whole = el('input', {type: 'checkbox', checked: true});
  go.onclick = async () => {
    if (!sel.size) return;
    try {
      const r = await api('/api/detections/assign', {body: {ids: [...sel], target: o.id, whole_track: whole.checked}});
      toast(r.moved + ' Bild(er) zugewiesen' + (r.skipped ? ' · ' + r.skipped + ' übersprungen' : '')); loadObjects(); openObject(o.id);
    } catch (e) { toast(e.message); }
  };
  openModal(el('div', null, el('h2', null, 'Erkennungen zuweisen · ', el('span', {class: 'acc'}, o.label)),
    el('p', {class: 'muted small', style: {marginTop: 0}}, 'Bilder anklicken, die zu „' + o.label + '“ gehören. Angezeigt werden nur passende Arten (' + o.cls_de + ').'),
    el('div', {class: 'bar'}, only, stream, el('label', {class: 'chk'}, sight, 'nur erste Bilder je Sichtung'), el('label', {class: 'chk'}, whole, 'ganze Sichtung mitnehmen')),
    grid, el('div', {class: 'more'}, more),
    el('div', {class: 'selbar'}, cnt, el('div', {class: 'grow'}), go, el('button', {class: 'btn-mini', onclick: () => openObject(o.id)}, 'Zurück zum Objekt'))));
  upd(); load(true);
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
      el('label', null, 'Mindestens erkannt'), el('div', {class: 'row'}, f.mc, ' mal'),
      el('label', null, 'innerhalb von'), el('div', {class: 'row'}, f.wm, ' Minuten'),
      el('label', null, 'in mindestens'), el('div', {class: 'row'}, f.ms, ' verschiedenen Streams'),
      el('div', {class: 'hint'}, 'Eine Sichtung zählt, sobald das Objekt mehrmals hintereinander erkannt wurde; eine durchgehende Anwesenheit zählt einmal.'),
      el('label', null, 'Pause nach Alarm'), el('div', {class: 'row'}, f.cd, ' Minuten'),
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
function numIn(v, attrs) { return el('input', Object.assign({type: 'number', value: v, class: 'num'}, attrs || {})); }
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
      if (!s.analysis) s.analysis = s.detect === false ? 'off' : 'objects';
      const an = el('select', {title: 'Was in diesem Stream ausgewertet wird'}, el('option', {value: 'objects'}, 'Analyse: Objekterkennung (KI)'),
        el('option', {value: 'motion'}, 'Analyse: nur Bewegung'), el('option', {value: 'off'}, 'Analyse: aus (nur Live-Bild)'));
      an.value = s.analysis;
      const mp = numIn(s.motion_pct != null ? s.motion_pct : 0.5, {min: 0.05, max: 50, step: 0.05, class: 'num'}); mp.oninput = () => s.motion_pct = +mp.value;
      const rec = el('select', {title: 'Videoschnipsel aufnehmen'}, el('option', {value: 'off'}, 'Aufnahme: aus'),
        el('option', {value: 'motion'}, 'Aufnahme bei Bewegung'), el('option', {value: 'objects'}, 'Aufnahme bei erkanntem Objekt'));
      rec.value = s.record || 'off';
      const rs = numIn(s.record_s || 15, {min: 3, max: 600, step: 1, class: 'num'}); rs.oninput = () => s.record_s = +rs.value;
      const rp = numIn(s.record_pre_s != null ? s.record_pre_s : 5, {min: 0, max: 60, step: 1, class: 'num'}); rp.oninput = () => s.record_pre_s = +rp.value;
      if (s.record_pre_s == null) s.record_pre_s = 5;
      const rx = el('input', {type: 'checkbox', checked: s.record_extend !== false}); rx.onchange = () => { s.record_extend = rx.checked; upd(); };
      const rm = numIn(s.record_max_s || 60, {min: 5, max: 1800, step: 5, class: 'num'}); rm.oninput = () => s.record_max_s = +rm.value;
      const mrow = el('span', {class: 'pair', title: 'Ab diesem Anteil bewegter Bildfläche gilt es als Bewegung (Rauschen, Regen und Blätter bleiben darunter)'},
        el('span', {class: 'muted'}, 'Bewegung ab'), mp, el('span', {class: 'muted'}, '% Bildfläche'));
      const rrow = el('div', {class: 'r small rec-row'}, rec,
        el('span', {class: 'pair', title: 'So viele Sekunden VOR dem Ereignis werden mitgespeichert (0 = kein Vorlauf). Dafür läuft dauerhaft ein kleiner Puffer im Arbeitsspeicher.'},
          el('span', {class: 'muted'}, 'Vorlauf'), rp, el('span', {class: 'muted'}, 's')),
        el('span', {class: 'pair'}, el('span', {class: 'muted'}, 'Länge'), rs, el('span', {class: 'muted'}, 's')),
        el('label', {class: 'chk', title: 'Aufnahme läuft weiter, solange sich noch etwas bewegt'}, rx, 'verlängern, solange Bewegung'),
        el('span', {class: 'pair rmax'}, el('span', {class: 'muted'}, 'max.'), rm, el('span', {class: 'muted'}, 's')));
      const upd = () => {
        mrow.hidden = !(s.analysis === 'motion' || s.record === 'motion');
        rrow.querySelectorAll('.pair, label').forEach(e => e.hidden = (s.record || 'off') === 'off');
        rrow.querySelector('.rmax').hidden = (s.record || 'off') === 'off' || !rx.checked;
        if (s.analysis === 'off' && s.record === 'objects') { s.record = 'motion'; rec.value = 'motion'; }
        rec.querySelector('[value=objects]').disabled = s.analysis !== 'objects';
      };
      an.onchange = () => { s.analysis = an.value; s.detect = an.value === 'objects'; upd(); };
      rec.onchange = () => { s.record = rec.value; upd(); };
      upd();
      const kf = el('input', {type: 'checkbox', checked: !!s.keyframes_only}); kf.onchange = () => s.keyframes_only = kf.checked;
      const fps = numIn(s.fps || 2, {min: .2, max: 15, step: .2, class: 'num'}); fps.oninput = () => s.fps = +fps.value;
      const w = numIn(s.width || 640, {min: 160, max: 1920, step: 16, class: 'num'}); w.oninput = () => s.width = +w.value;
      const tr = el('select', {title: 'RTSP-Transport'}, el('option', {value: 'auto'}, 'RTSP: automatisch'), el('option', {value: 'tcp'}, 'RTSP über TCP'), el('option', {value: 'udp'}, 'RTSP über UDP (wie VLC)'));
      tr.value = s.transport || 'auto'; tr.onchange = () => s.transport = tr.value;
      const res = el('div', {class: 'stest', hidden: true});
      const tb = el('button', {class: 'btn-mini', title: 'Verbindung, RTSP-Antworten und Empfang über TCP und UDP direkt vom Gerät aus prüfen'}, 'Testen');
      tb.onclick = async () => {
        tb.disabled = true; tb.textContent = 'Teste … (bis 1 Min.)'; res.hidden = false; res.innerHTML = '';
        res.append(el('div', {class: 'muted small'}, 'Prüfe ' + (s.url || '') + ' …'));
        try {
          const r = await api('/api/streams/test', {body: {id: s.id, url: s.url}});
          res.innerHTML = '';
          r.steps.forEach(x => res.append(el('div', {class: 'st ' + (x.ok === true ? 'ok' : x.ok === false ? 'bad' : 'info')},
            el('span', {class: 'ic'}, x.ok === true ? '✓' : x.ok === false ? '✕' : 'i'), el('b', null, x.name), el('span', {class: 'd'}, x.detail))));
          if (r.recommend && r.recommend !== (s.transport || 'auto')) {
            res.append(el('div', {class: 'st info'}, el('span', {class: 'ic'}, '→'), el('b', null, 'Empfehlung'),
              el('span', {class: 'd'}, 'RTSP über ' + r.recommend.toUpperCase() + ' ',
                el('button', {class: 'btn-mini', onclick: () => { s.transport = r.recommend; tr.value = r.recommend; toast('Übernommen – „Streams speichern“ nicht vergessen'); }}, 'übernehmen'))));
          } else if (!r.recommend) {
            res.append(el('div', {class: 'st bad'}, el('span', {class: 'ic'}, '!'), el('b', null, 'Ergebnis'), el('span', {class: 'd'}, 'Kein Videoempfang möglich – siehe rot markierten Schritt.')));
          }
          if (r.sdp) res.append(el('details', null, el('summary', {class: 'muted small'}, 'SDP der Kamera'), el('pre', {class: 'mono small'}, r.sdp)));
        } catch (e) { res.innerHTML = ''; res.append(el('div', {class: 'st bad'}, e.message)); }
        tb.disabled = false; tb.textContent = 'Testen';
      };
      const dc = el('select', {title: 'Dekoder'}, el('option', {value: 'auto'}, 'Dekoder: automatisch'), el('option', {value: 'nvdec'}, 'Hardware (Jetson NVDEC)'),
        el('option', {value: 'gst'}, 'GStreamer (Software)'), el('option', {value: 'ffmpeg'}, 'ffmpeg')); dc.value = s.decoder || 'auto'; dc.onchange = () => s.decoder = dc.value;
      const st = (S.state.streams.find(x => x.id === s.id) || {});
      wrap.append(el('div', {class: 'stream-ed'},
        el('div', {class: 'r'}, el('span', {class: 'status-dot ' + (st.online ? 'online' : s.id ? '' : 'idle'), id: 'sdot-' + (s.id || 'n' + i)}), nm,
          el('button', {class: 'btn-mini del', onclick: () => { if (confirm('Stream „' + (s.name || '') + '“ entfernen?')) { list.splice(i, 1); draw(); } }}, 'Entfernen')),
        el('div', {class: 'r'}, url, tb),
        el('div', {class: 'r small'}, el('label', {class: 'chk'}, en, 'aktiv'), an, mrow),
        rrow,
        el('div', {class: 'r small'},
          el('span', {class: 'pair'}, el('span', {class: 'muted'}, 'Bilder/s'), fps), el('span', {class: 'pair', title: 'Analyse-Auflösung – 1280 px erkennt weit entfernte Personen viel besser (Hauptstream nutzen)'}, el('span', {class: 'muted'}, 'Breite'), w), tr, dc,
          el('label', {class: 'chk', title: 'Nur Schlüsselbilder dekodieren – spart viel Rechenleistung, Live-Bild ruckelt'}, kf, 'nur Keyframes')),
        (st.decoder || st.transport) ? el('div', {class: 'muted small'}, 'Aktiv: ' + [st.decoder, st.transport ? 'RTSP ' + st.transport : '',
          st.prebuf ? (st.prebuf.ok ? 'Vorlauf-Puffer ' + st.prebuf.seconds + ' s / ' + st.prebuf.mb + ' MB' : 'Vorlauf-Puffer startet …') : ''].filter(Boolean).join(' · ') + (st.online ? ' · ' + st.fps + ' Bilder/s' : '')) : null,
        res,
        st.error && !st.online ? el('div', {class: 'mono small err-line'}, st.error) : null));
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
      el('button', {class: 'btn btn-ghost', onclick: () => { streams.push({name: 'Kamera ' + (streams.length + 1), url: '', enabled: true, detect: true, fps: 4, width: S.state && S.state.gpu_available ? 1280 : 640, decoder: 'auto', transport: 'auto'}); se.draw(); }}, '+ Stream hinzufügen'),
      el('button', {class: 'btn', onclick: async () => {
        try { await api('/api/streams', {body: {streams}}); toast('Streams gespeichert – verbinde …'); S.cfg = await api('/api/config'); fillSelects(); setTimeout(loadSettings, 1500); }
        catch (e) { toast(e.message); }
      }}, 'Streams speichern')),
    el('p', {class: 'muted small', style: {marginBottom: 0}}, 'H.264/H.265 werden per Hardware (NVDEC) dekodiert und skaliert – auch Hauptstreams (1080p) sind möglich. ',
      'MJPEG-Kameras laufen über ffmpeg. 2–5 Bilder/s je Kamera reichen für die Erkennung. ',
      'Breite = Auflösung, mit der analysiert wird: 1280 px findet weit entfernte Personen deutlich besser als 640 px. ',
      'RTSP „automatisch“ verbindet über TCP und wechselt bei Problemen auf UDP (wie VLC). „Testen“ zeigt, woran eine Kamera scheitert.')));
  /* Erkennung */
  const D = C.detection;
  const GT = Object.assign({}, D.group_thresholds || {});
  const gl = el('div', {class: 'grp-list'});
  const gch = {}, gthr = {}, gmov = {};
  const RM = D.require_motion || ['person', 'animal', 'bike'];
  gl.append(el('div', {class: 'g head'}, el('span'), el('span'), el('span', null, 'Gruppe'), el('span', {title: 'Mindest-Sicherheit'}, 'Schwelle'),
    el('span', {title: 'Neue Objekte nur melden, wenn sich an der Stelle etwas bewegt (gegen Schatten, Plakate, Statuen)'}, 'Bewegung')));
  S.meta.groups.forEach(g => {
    gch[g.id] = el('input', {type: 'checkbox', checked: D.groups.includes(g.id)});
    gthr[g.id] = numIn(Math.round((GT[g.id] != null ? GT[g.id] : D.threshold) * 100), {min: 20, max: 95, step: 5, class: 'thr'});
    gmov[g.id] = el('input', {type: 'checkbox', checked: RM.includes(g.id), title: 'nur mit Bewegung'});
    gl.append(el('div', {class: 'g'}, gch[g.id], el('i', {style: {borderColor: g.color}}),
      el('label', {class: 'gname', title: g.classes.map(c => S.meta.classes.find(x => x.id === c).de).join(', ')}, g.name,
        el('span', {class: 'muted small'}, g.classes.slice(0, 4).map(c => S.meta.classes.find(x => x.id === c).de).join(', ') + (g.classes.length > 4 ? ' …' : ''))),
      el('span', {class: 'thrbox'}, gthr[g.id], ' %'), el('span', {class: 'movbox'}, gmov[g.id])));
    gl.lastChild.querySelector('.gname').onclick = () => { gch[g.id].checked = !gch[g.id].checked; };
  });
  const extra = new Set(D.extra_classes || []);
  const cp = el('div', {class: 'cls-pick', hidden: true});
  S.meta.classes.forEach(c => { const cb = el('input', {type: 'checkbox', checked: extra.has(c.id)}); cb.onchange = () => cb.checked ? extra.add(c.id) : extra.delete(c.id);
    cp.append(el('label', {class: 'chk small'}, cb, c.de)); });
  const thr = numIn(Math.round(D.threshold * 100), {min: 20, max: 95, step: 5});
  const mdl = el('select', null, el('option', {value: 'auto'}, 'Automatisch (GPU: YOLOX-S, ohne GPU: YOLOX-Tiny)'),
    el('option', {value: 'yolox_m'}, 'YOLOX-M 640 px – am genauesten, langsam (Jetson-GPU, 1–2 Kameras)'),
    el('option', {value: 'yolox_s'}, 'YOLOX-S 640 px – sehr genau (Jetson-GPU)'), el('option', {value: 'yolox_tiny'}, 'YOLOX-Tiny 416 px – genau'),
    el('option', {value: 'nanodet'}, 'NanoDet-Plus – am schnellsten, ungenau')); mdl.value = D.model || 'auto';
  const ms = el('select', null, el('option', {value: 320}, '320 px – schneller'), el('option', {value: 416}, '416 px – genauer')); ms.value = D.model_size;
  const msRow = [el('label', null, 'NanoDet-Größe'), ms];
  const updMs = () => msRow.forEach(e => e.style.display = mdl.value === 'nanodet' ? '' : 'none');
  mdl.onchange = updMs;
  const tl = el('select', null, el('option', {value: 'auto'}, 'automatisch (mit GPU an)'), el('option', {value: 'on'}, 'immer an'), el('option', {value: 'off'}, 'aus')); tl.value = D.tiles || 'auto';
  const ac = el('select', null, el('option', {value: 'auto'}, 'automatisch (GPU, falls vorhanden)'), el('option', {value: 'gpu'}, 'GPU (TensorRT, Jetson)'), el('option', {value: 'cpu'}, 'nur CPU')); ac.value = D.accel || 'auto';
  const mh = numIn(D.min_hits, {min: 1, max: 10}), iv = numIn(D.interval_s, {min: .2, step: .1}), mg = el('input', {type: 'checkbox', checked: D.motion_gate});
  const ml = numIn(D.motion_area_pct != null ? D.motion_area_pct : 0.03, {min: 0, max: 5, step: 0.01}), tt = numIn(D.track_timeout_s, {min: 1, step: .5}), rs = numIn(D.resnap_s, {min: 0, step: 10});
  const mb = numIn(D.min_box_px, {min: 8}), en = el('input', {type: 'checkbox', checked: D.enabled});
  const sg = numIn(D.sighting_gap_s, {min: 0, step: 10});
  const vf = el('input', {type: 'checkbox', checked: D.verify !== false});
  const pa = numIn(D.person_min_aspect != null ? D.person_min_aspect : 0.9, {min: 0, max: 2, step: 0.1});
  const fst = el('span', {class: 'mono small', id: 'filterStats'});
  box.append(el('div', {class: 'panel'}, el('h3', null, 'Objekterkennung'),
    el('div', {class: 'form'},
      el('label', null, 'Erkennung aktiv'), el('label', {class: 'chk'}, en, 'Streams analysieren'),
      el('label', null, 'Modell'), mdl,
      el('label', null, 'Rechenwerk'), ac,
      el('label', null, 'Kachel-Analyse'), tl,
      el('div', {class: 'hint'}, 'Große Kamerabilder werden zusätzlich in Ausschnitten geprüft – findet auch kleine, weit entfernte Personen. ',
        'Lohnt sich ab ca. 800 px Bildbreite (Stream-Breite erhöhen bzw. Hauptstream nutzen). Kostet je Kachel eine weitere Analyse.'),
      msRow[0], msRow[1],
      el('label', null, 'Erkennen'), gl,
      el('label', null, 'Weitere Arten'), el('div', {class: 'row'}, el('button', {class: 'btn-mini', onclick: () => cp.hidden = !cp.hidden}, 'Einzelne Arten wählen (' + extra.size + ')'), cp),
      el('label', null, 'Grundschwelle'), el('div', {class: 'row'}, thr, ' % für einzeln gewählte Arten'),
      el('label', null, 'Zweitprüfung'), el('label', {class: 'chk'}, vf, 'neue Objekte im vergrößerten Ausschnitt nochmal prüfen'),
      el('div', {class: 'hint'}, 'Verwirft die meisten Schatten, Spiegelungen und Muster. Kostet nur bei neuen Objekten eine zusätzliche Analyse.'),
      el('label', null, 'Personen-Form'), el('div', {class: 'row'}, pa, ' × Breite = Mindesthöhe einer Person (0 = aus)'),
      el('label', null, 'Sichtung ab'), el('div', {class: 'row'}, mh, ' Treffern hintereinander'),
      el('label', null, 'Verworfen'), fst,
      el('label', null, 'Analyse-Abstand'), el('div', {class: 'row'}, iv, ' s je Stream'),
      el('label', null, 'Bewegungsfilter'), el('div', {class: 'row'},
        el('label', {class: 'chk'}, mg, 'nur bei Bewegung analysieren'), el('span', {class: 'muted'}, 'ab'), ml, el('span', {class: 'muted'}, '% der Bildfläche')),
      el('label', null, 'Objekt verschwunden nach'), el('div', {class: 'row'}, tt, ' s'),
      el('label', null, 'Neue Sichtung nach'), el('div', {class: 'row'}, sg, ' s Abwesenheit im selben Stream'),
      el('label', null, 'Weiteres Bild alle'), el('div', {class: 'row'}, rs, ' s bei längerer Anwesenheit (0 = nie)'),
      el('label', null, 'Mindestgröße'), el('div', {class: 'row'}, mb, ' px Höhe')),
    el('div', {class: 'actions'}, el('button', {class: 'btn', onclick: () => {
      const gt = {}; Object.keys(gthr).forEach(k => { gt[k] = +gthr[k].value / 100; });
      saveSection('detection', {
        enabled: en.checked, groups: Object.keys(gch).filter(k => gch[k].checked), extra_classes: [...extra], threshold: +thr.value / 100,
        group_thresholds: gt, require_motion: Object.keys(gmov).filter(k => gmov[k].checked), model: mdl.value, model_size: +ms.value, accel: ac.value, min_hits: +mh.value, verify: vf.checked,
        person_min_aspect: +pa.value, interval_s: +iv.value, motion_gate: mg.checked, motion_area_pct: +ml.value, tiles: tl.value,
        track_timeout_s: +tt.value, sighting_gap_s: +sg.value, resnap_s: +rs.value, min_box_px: +mb.value});
    }}, 'Speichern'))));
  setTimeout(updMs, 0);
  box.append(falsePosPanel());
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
      el('label', null, 'Gesicht mind.'), el('div', {class: 'row'}, fm, ' px breit'),
      el('label', null, 'Aussehen gleich ab'), at, el('div', {class: 'hint'}, 'höher = weniger Verwechslungen, aber mehr neue Objekte (Standard 0,90)'),
      el('label', null, 'Personen über Aussehen'), el('div', {class: 'row'}, aw, ' h nach letzter Sichtung'),
      el('label', null, 'Andere über Aussehen'), el('div', {class: 'row'}, ao, ' h nach letzter Sichtung'),
      el('label', null, 'Lernbeispiele je Objekt'), mx),
    el('div', {class: 'actions'}, el('button', {class: 'btn', onclick: () => saveSection('identity', {faces: fa.checked, face_threshold: +ft.value,
      face_min_px: +fm.value, appearance_threshold: +at.value, appearance_window_h: +aw.value, appearance_window_other_h: +ao.value, max_samples: +mx.value})}, 'Speichern'))));
  /* Alarme / MQTT */
  const M = C.mqtt;
  const me = el('input', {type: 'checkbox', checked: M.enabled}), mhst = el('input', {type: 'text', value: M.host}), mp = numIn(M.port, {min: 1, max: 65535});
  const mu = el('input', {type: 'text', value: M.user, autocomplete: 'off'}), mpw = el('input', {type: 'password', value: M.password, autocomplete: 'new-password'});
  const mpre = el('input', {type: 'text', value: M.prefix}), mdet = el('input', {type: 'checkbox', checked: M.publish_detections});
  const mst = el('input', {type: 'checkbox', checked: M.publish_streams !== false}), msp = el('input', {type: 'checkbox', checked: M.publish_storage !== false});
  const msi = numIn(M.storage_interval_s || 1, {min: 1, max: 3600, step: 1});
  box.append(el('div', {class: 'panel'}, el('h3', null, 'MQTT'),
    el('div', {class: 'form'},
      el('label', null, 'MQTT'), el('label', {class: 'chk'}, me, 'an MQTT-Broker senden (Alarme, Stream-Zustand, Speicher)'),
      el('label', null, 'Broker'), el('div', {class: 'row'}, mhst, mp),
      el('div', {class: 'hint'}, 'Läuft Mosquitto auf dem Jetson selbst: 127.0.0.1 : 1883'),
      el('label', null, 'Benutzer'), mu, el('label', null, 'Passwort'), mpw,
      el('label', null, 'Topic-Präfix'), mpre, el('div', {class: 'hint'}, 'Alarme: <präfix>/alarm · Erkennungen: <präfix>/detection'),
      el('label', null, 'Erkennungen'), el('label', {class: 'chk'}, mdet, 'jede Sichtung ebenfalls senden'),
      el('label', null, 'Stream-Zustand'), el('label', {class: 'chk'}, mst, 'je Stream eigene Topics (Bewegung, Aufnahme, erreichbar …)'),
      el('div', {class: 'hint mono'}, '<präfix>/stream/<Name>/erreichbar · bewegung · aufnahme · personen · … · json'),
      el('label', null, 'Speicherfüllstand'), el('div', {class: 'row'}, el('label', {class: 'chk'}, msp, 'senden alle'), msi, el('span', {class: 'muted'}, 's')),
      el('div', {class: 'hint mono'}, '<präfix>/speicher/<Ort>/frei_gb · belegt_prozent · … · json')),
    el('div', {class: 'actions'},
      el('button', {class: 'btn', onclick: () => saveSection('mqtt', {enabled: me.checked, host: mhst.value, port: +mp.value, user: mu.value, password: mpw.value,
        prefix: mpre.value, publish_detections: mdet.checked, publish_streams: mst.checked, publish_storage: msp.checked, storage_interval_s: +msi.value})}, 'Speichern'),
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
      el('label', null, 'Behalten'), el('div', {class: 'row'}, md, ' Tage Bilder und Videos (0 = unbegrenzt)'),
      el('label', null, 'Höchstens'), el('div', {class: 'row'}, mm, ' MB (älteste zuerst löschen)'),
      el('div', {class: 'hint'}, 'Gelernte Personen/Objekte bleiben auch nach dem Löschen alter Bilder erhalten.'),
      el('label', null, 'Alarmton'), el('label', {class: 'chk'}, snd, 'Ton in der Oberfläche abspielen'),
      el('label', null, 'Objektliste ab'), el('div', {class: 'row'}, rl, ' Sichtungen („mehrfach erkannt“)')),
    el('div', {class: 'actions'},
      el('button', {class: 'btn', onclick: async () => { await saveSection('storage', {max_days: +md.value, max_mb: +mm.value}, ' '); await saveSection('ui', {sound: snd.checked, repeat_list_min: +rl.value}); }}, 'Speichern'),
      el('button', {class: 'btn btn-ghost', onclick: async () => { await api('/api/cleanup', {body: {}}); toast('Aufräumen gestartet'); }}, 'Jetzt aufräumen'),
      el('button', {class: 'btn btn-danger', onclick: openPurge}, 'Alle Erkennungen und Bilder löschen'))));
  box.append(storagePanel(C.storage || {}));
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
        el('div', {class: 'row'}, port, el('button', {class: 'btn-mini', onclick: async () => {
          await saveSection('server', {port: +port.value}, 'Port gespeichert – Neustart …');
          await api('/api/restart', {body: {}});
          setTimeout(() => location.href = location.protocol + '//' + location.hostname + ':' + port.value + '/', 4000);
        }}, 'Übernehmen + Neustart'))),
    el('h4', null, 'KI-Modelle'), mt,
    el('div', {class: 'actions'}, el('button', {class: 'btn-mini', onclick: async () => { await api('/api/models/download', {body: {}}); toast('Download gestartet'); }}, 'Fehlende Modelle laden'),
      (S.state.engines || []).some(x => x.state === 'fehler') ? el('button', {class: 'btn-mini', title: 'Streams und Erkennung werden dafür einige Minuten angehalten', onclick: async () => {
        if (!confirm('GPU-Engine erneut bauen? Streams und Erkennung pausieren dafür ca. 5–20 Minuten.')) return;
        await api('/api/engines/retry', {body: {}}); toast('Engine-Bau gestartet – siehe Protokoll'); }}, 'GPU-Engine erneut bauen') : null,
      el('button', {class: 'btn-mini', onclick: async () => { if (!confirm('Sentinel neu starten?')) return; await api('/api/restart', {body: {}}); toast('Neustart …'); }}, 'Neu starten')),
    el('h4', null, 'Protokoll'), lg));
  if (S.state.fan && S.state.fan.available) box.append(fanPanel());
  box.append(updatePanel());
  updateSettingsStatus();
}
/* ------------------------------------------------------------ Speicherort */
function fmtBytes(n) {
  if (n == null) return '?';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i < 2 ? Math.round(n) : n.toFixed(1)) + ' ' + u[i];
}
function storagePanel(st) {
  const t = Object.assign({type: 'local'}, st.target || {});
  const typ = el('select', null, el('option', {value: 'local'}, 'Datenordner (interner Speicher)'),
    el('option', {value: 'disk'}, 'USB-Festplatte / USB-Stick'), el('option', {value: 'nas'}, 'Netzwerkspeicher (NAS / Freigabe)'),
    el('option', {value: 'path'}, 'Eigener Ordner (bereits eingebunden)'));
  typ.value = t.type;
  const info = el('div', {class: 'stor-info'}, '…');
  const disk = el('select', null, el('option', {value: ''}, 'Laufwerke werden gesucht …'));
  const rescan = el('button', {class: 'btn-mini'}, 'Neu suchen');
  const path = el('input', {type: 'text', value: t.path || '', placeholder: '/media/usb/aufnahmen'});
  const proto = el('select', null, el('option', {value: 'smb'}, 'SMB (Windows, Fritzbox, Synology …)'), el('option', {value: 'nfs'}, 'NFS'));
  proto.value = t.proto || 'smb';
  const srv = el('input', {type: 'text', value: t.server || '', placeholder: '192.168.178.10 oder nas.local'});
  const share = el('input', {type: 'text', value: t.share || '', placeholder: 'z. B. kameras (NFS: /volume1/kameras)'});
  const sub = el('input', {type: 'text', value: t.subdir || '', placeholder: 'Sentinel-MK1'});
  const usr = el('input', {type: 'text', value: t.user || '', placeholder: 'leer = Gast', autocomplete: 'off'});
  const pw = el('input', {type: 'password', value: t.password || '', autocomplete: 'new-password'});
  const ver = el('select', null, el('option', {value: ''}, 'automatisch'), el('option', {value: '3.0'}, '3.0'), el('option', {value: '2.1'}, '2.1'),
    el('option', {value: '2.0'}, '2.0'), el('option', {value: '1.0'}, '1.0 (alte Fritzbox/NAS)'));
  ver.value = t.smb_version || '';
  const mig = el('input', {type: 'checkbox'});
  const lp = el('input', {type: 'checkbox', checked: !!st.loop});
  const lr = numIn(st.loop_reserve_gb != null ? st.loop_reserve_gb : 2, {min: 0.5, max: 500, step: 0.5, class: 'num'});
  const out = el('div', {class: 'stest', hidden: true});
  const rows = {
    disk: [el('label', null, 'Laufwerk'), el('div', {class: 'row'}, disk, rescan),
      el('div', {class: 'hint'}, 'Sentinel bindet das Laufwerk selbst ein (auch nach einem Neustart). ext4 ist am zuverlässigsten; exFAT/NTFS gehen ebenfalls.')],
    path: [el('label', null, 'Ordner'), path,
      el('div', {class: 'hint'}, 'Ordner muss existieren und dauerhaft eingebunden sein. Sentinel legt darin „Sentinel-MK1“ an.')],
    nas: [el('label', null, 'Protokoll'), proto, el('label', null, 'Server'), srv, el('label', null, 'Freigabe'), share,
      el('label', {class: 'smb'}, 'Benutzer'), usr, el('label', {class: 'smb'}, 'Passwort'), pw, el('label', {class: 'smb'}, 'SMB-Version'), ver,
      el('label', null, 'Unterordner'), sub,
      el('div', {class: 'hint'}, 'Fritzbox: Server = fritz.box, Freigabe = Name des USB-Speichers (z. B. FRITZ.NAS bzw. der Laufwerksname), Benutzer mit NAS-Recht.')]
  };
  const form = el('div', {class: 'form'}, el('label', null, 'Speicherort'), typ);
  Object.values(rows).forEach(r => r.forEach(e => form.append(e)));
  form.append(el('label', null, ''), el('label', {class: 'chk'}, mig, 'vorhandene Bilder und Videos dorthin verschieben'));
  const lrow = el('div', {class: 'row'}, el('span', {class: 'muted'}, 'freihalten:'), lr, el('span', {class: 'muted'}, 'GB'));
  form.append(el('label', null, 'Loop-Betrieb'), el('label', {class: 'chk'}, lp, 'Speicher füllen, dann älteste Bilder und Videos überschreiben'),
    el('label', null, ''), lrow,
    el('div', {class: 'hint'}, 'Wie ein Ringspeicher: Sentinel nutzt den Speicherort bis auf die Reserve (mind. 5 % bzw. der Wert oben) und löscht dann laufend das Älteste. ',
      'Die Grenze „Höchstens … MB“ gilt dann nicht; „Behalten … Tage“ weiterhin. Gelernte Personen/Objekte bleiben erhalten.'));
  lp.onchange = () => lrow.hidden = !lp.checked; lrow.hidden = !lp.checked;
  const show = () => {
    Object.entries(rows).forEach(([k, r]) => r.forEach(e => e.hidden = k !== typ.value));
    const smb = typ.value === 'nas' && proto.value === 'smb';
    rows.nas.forEach((e, i) => { if (i >= 6 && i <= 11) e.hidden = typ.value !== 'nas' || !smb; });
    mig.parentElement.hidden = typ.value === (st.target || {type: 'local'}).type && typ.value === 'local';
  };
  typ.onchange = () => { show(); if (typ.value === 'disk') scan(); };
  proto.onchange = show;
  const scan = async () => {
    disk.innerHTML = ''; disk.append(el('option', {value: ''}, 'suche …'));
    try {
      const ds = await api('/api/storage/disks');
      disk.innerHTML = '';
      if (!ds.length) disk.append(el('option', {value: ''}, 'kein externes Laufwerk gefunden – anstecken und „Neu suchen“'));
      ds.forEach(d => disk.append(el('option', {value: d.uuid, 'data-fs': d.fstype},
        [d.label || d.model || d.dev, fmtBytes(d.size), d.fstype, d.tran ? d.tran.toUpperCase() : '', d.mount ? 'eingebunden: ' + d.mount : ''].filter(Boolean).join(' · '))));
      if (t.uuid) disk.value = t.uuid;
    } catch (e) { disk.innerHTML = ''; disk.append(el('option', {value: ''}, e.message)); }
  };
  rescan.onclick = scan;
  const target = () => {
    const v = {type: typ.value};
    if (typ.value === 'disk') { v.uuid = disk.value; const o = disk.selectedOptions[0]; v.fstype = o ? o.dataset.fs || '' : ''; }
    if (typ.value === 'path') v.path = path.value.trim();
    if (typ.value === 'nas') Object.assign(v, {proto: proto.value, server: srv.value.trim(), share: share.value.trim(), subdir: sub.value.trim(),
      user: usr.value.trim(), password: pw.value, smb_version: ver.value});
    return v;
  };
  const showRes = (ok, msg) => { out.hidden = false; out.innerHTML = '';
    out.append(el('div', {class: 'st ' + (ok ? 'ok' : 'bad')}, el('span', {class: 'ic'}, ok ? '✓' : '✕'), el('b', null, ok ? 'OK' : 'Fehler'), el('span', {class: 'd'}, msg))); };
  const refresh = async () => {
    try {
      const i = await api('/api/storage');
      info.innerHTML = '';
      info.append(...[el('span', {class: 'mono'}, i.root), ' ',
        el('span', {class: 'badge ' + (i.state === 'ersatz' ? 'warn' : 'ok')}, i.state === 'ok' ? 'aktiv' : i.state === 'ersatz' ? 'Ziel nicht erreichbar – speichert im Datenordner' : 'Datenordner'),
        el('span', {class: 'muted'}, ' · ' + fmtBytes(i.free) + ' frei von ' + fmtBytes(i.total)),
        i.loop ? el('span', {class: 'badge ok'}, 'Loop') : null,
        i.migrating ? el('span', {class: 'muted'}, ' · Umzug: ' + i.migrating) : null,
        i.error ? el('div', {class: 'mono small err-line'}, i.error) : null].filter(Boolean));
    } catch (e) { info.textContent = e.message; }
  };
  show(); refresh(); if (t.type === 'disk') scan();
  return el('div', {class: 'panel'}, el('h3', null, 'Speicherort für Bilder und Videos'),
    el('div', {class: 'space-bars', id: 'spaceBars'}, el('div', {class: 'muted small'}, 'Speicherplatz wird gemessen …')),
    el('div', {class: 'form', style: {marginBottom: '10px'}}, el('label', null, 'Aktuell'), info), form, out,
    el('div', {class: 'actions'},
      el('button', {class: 'btn btn-ghost', onclick: async e => {
        const b = e.target; b.disabled = true; b.textContent = 'Teste …';
        try { const r = await api('/api/storage/test', {body: {target: target()}}); showRes(r.ok, r.msg); } catch (x) { showRes(false, x.message); }
        b.disabled = false; b.textContent = 'Testen';
      }}, 'Testen'),
      el('button', {class: 'btn', onclick: async e => {
        const b = e.target; b.disabled = true;
        try {
          const r = await api('/api/config', {body: {section: 'storage', values: {target: target(), loop: lp.checked, loop_reserve_gb: +lr.value}, migrate: mig.checked}});
          showRes(r.ok, r.ok ? 'Übernommen: ' + r.msg + (mig.checked ? ' – Dateien werden im Hintergrund verschoben' : '') : r.msg);
          refresh();
        } catch (x) { showRes(false, x.message); }
        b.disabled = false;
      }}, 'Übernehmen')));
}

/* ------------------------------------------------------------ Ereignisse (Bewegung, Videos) */
async function loadEvents(reset) {
  const box = $('#evg');
  if (reset) { box.innerHTML = ''; S.evBefore = null; }
  const q = new URLSearchParams({limit: 60});
  if ($('#eStream').value) q.set('stream', $('#eStream').value);
  if ($('#eKind').value) q.set('kind', $('#eKind').value);
  if ($('#eClip').checked) q.set('clip', '1');
  if ($('#eDate').value) { const d0 = new Date($('#eDate').value + 'T00:00:00').getTime() / 1000; q.set('from', d0); q.set('to', d0 + 86400); }
  if (S.evBefore) q.set('before', S.evBefore);
  let rows;
  try { rows = await api('/api/events?' + q); } catch (e) { return toast(e.message); }
  if (reset && !rows.length) box.append(el('div', {class: 'empty'}, 'Noch keine Ereignisse. Bewegungen und Aufnahmen erscheinen hier, sobald ein Stream auf „nur Bewegung“ oder eine Aufnahme eingestellt ist.'));
  rows.forEach(r => box.append(eventCard(r)));
  if (rows.length) S.evBefore = rows[rows.length - 1].ts;
  $('#eMore').hidden = rows.length < 60;
}
function eventCard(r) {
  const img = r.img ? el('img', {src: r.img, loading: 'lazy'}) : el('div', {class: 'noimg'}, 'kein Bild');
  const inner = el('div', {class: 'ev-in'}, img);
  const fr = el('div', {class: 'ev-img'}, inner);
  if (r.img) img.onload = () => {
    // Rahmen genau auf das Bild legen (Bild wird mit Rand in 16:9 eingepasst)
    const ar = img.naturalWidth / img.naturalHeight, box = 16 / 9;
    if (ar >= box) { inner.style.width = '100%'; inner.style.height = (box / ar * 100) + '%'; }
    else { inner.style.height = '100%'; inner.style.width = (ar / box * 100) + '%'; }
  };
  if (r.box && r.box[0] != null) inner.append(el('div', {class: 'ev-box ' + r.kind, style: {left: r.box[0] * 100 + '%', top: r.box[1] * 100 + '%',
    width: (r.box[2] - r.box[0]) * 100 + '%', height: (r.box[3] - r.box[1]) * 100 + '%'}}));
  if (r.clip || r.recording) fr.append(el('span', {class: 'ev-play' + (r.recording ? ' rec' : '')}, r.recording ? '● Aufnahme läuft' : '▶ ' + Math.round(r.clip_dur || 0) + ' s'));
  return el('div', {class: 'ev-card', onclick: () => openEvent(r)}, fr,
    el('div', {class: 'ev-meta'}, el('b', null, r.kind === 'motion' ? 'Bewegung' : r.label || 'Objekt'),
      el('span', {class: 'muted'}, r.stream_name), el('span', {class: 'muted mono small'}, fmtTime(r.ts, true))));
}
function openEvent(r) {
  const media = r.clip ? el('video', {src: r.clip, controls: true, autoplay: true, playsinline: true, poster: r.img || '', class: 'ev-video'})
    : r.img ? el('img', {src: r.img, class: 'ev-video'}) : el('div', {class: 'muted'}, 'kein Bild');
  const dur = r.ts_end && r.ts_end > r.ts ? Math.round(r.ts_end - r.ts) + ' s' : '–';
  openModal(el('div', null, el('h2', null, (r.kind === 'motion' ? 'Bewegung' : r.label || 'Objekt') + ' · ' + r.stream_name), media,
    r.clip ? el('p', {class: 'muted small'}, 'Spielt das Video nicht ab (H.265-Kameras in manchen Browsern), „Video herunterladen“ nutzen.') : null,
    el('div', {class: 'kv'}, el('span', null, 'Zeit'), el('span', null, fmtTime(r.ts, 'full')),
      el('span', null, 'Dauer der Bewegung'), el('span', null, dur),
      r.kind === 'motion' ? el('span', null, 'bewegte Fläche') : null, r.kind === 'motion' ? el('span', null, 'bis ' + (r.area || 0).toFixed(1) + ' %') : null,
      el('span', null, 'Video'), el('span', null, r.clip ? Math.round(r.clip_dur || 0) + ' s · ' + fmtBytes(r.clip_size) : r.recording ? 'Aufnahme läuft noch …' : 'keins')),
    el('div', {class: 'actions'},
      r.clip ? el('a', {class: 'btn', href: r.clip + '?dl=1'}, 'Video herunterladen') : null,
      r.img ? el('a', {class: 'btn btn-ghost', href: r.img, target: '_blank'}, 'Bild öffnen') : null,
      el('button', {class: 'btn btn-danger', onclick: async () => {
        if (!confirm('Ereignis samt Bild und Video löschen?')) return;
        await api('/api/events/delete', {body: {ids: [r.id]}}); closeModal(); loadEvents(true);
      }}, 'Löschen'))));
}
function initEvents() {
  $('#eStream').innerHTML = ''; $('#eStream').append(el('option', {value: ''}, 'Alle Streams'));
  (S.state.streams || []).forEach(x => $('#eStream').append(el('option', {value: x.id}, x.name)));
  ['#eStream', '#eKind', '#eClip', '#eDate'].forEach(k => $(k).onchange = () => loadEvents(true));
  $('#eMore').onclick = () => loadEvents(false);
  $('#eDel').onclick = async () => {
    if (!confirm('Alle Ereignisse samt Bildern und Videos löschen?')) return;
    const r = await api('/api/events/delete', {body: {ids: 'alle'}}); toast(r.deleted + ' Ereignisse gelöscht'); loadEvents(true);
  };
}

/* Gelernte Fehlerkennungen */
function falsePosPanel() {
  const list = el('div', {class: 'fp-list'});
  const cnt = el('span', {class: 'mono'});
  const load = async () => {
    const rows = await api('/api/falsepos');
    cnt.textContent = rows.length + ' gelernt';
    list.innerHTML = '';
    if (!rows.length) list.append(el('div', {class: 'muted small'}, 'Noch keine. In der Galerie bei einem falschen Treffer „Fehlerkennung“ wählen.'));
    rows.forEach(r => list.append(el('div', {class: 'fp', title: r.cls_de + ' · ' + r.stream_name + ' · ' + fmtTime(r.ts, true)},
      el('div', {class: 'img', style: {backgroundImage: r.crop ? 'url(' + r.crop + ')' : 'none'}}),
      el('span', null, r.cls_de), el('span', {class: 'muted'}, r.stream_name),
      el('button', {class: 'btn-mini del', onclick: async () => { await api('/api/falsepos/' + r.id, {method: 'DELETE'}); load(); }}, '✕'))));
  };
  load();
  return el('div', {class: 'panel'}, el('h3', null, 'Gelernte Fehlerkennungen'),
    el('p', {class: 'muted small', style: {marginTop: 0}}, 'Treffer, die als „kein echtes Objekt“ markiert wurden (z. B. Schatten). ',
      'Taucht an derselben Stelle im selben Stream wieder so etwas auf, meldet Sentinel es nicht mehr.'),
    el('div', {class: 'bar', style: {marginBottom: '10px'}}, cnt, el('div', {class: 'grow'}),
      el('button', {class: 'btn-mini del', onclick: async () => { if (!confirm('Alle gelernten Fehlerkennungen vergessen?')) return; await api('/api/falsepos/alle', {method: 'DELETE'}); load(); }}, 'Alle vergessen')),
    list);
}
/* Luefter (Jetson) */
function fanCurveSvg(F) {
  const W = 360, H = 150, x0 = 34, y0 = 10, w = W - x0 - 10, h = H - y0 - 24, tMin = 20, tMax = 85;
  const X = t => x0 + (Math.min(tMax, Math.max(tMin, t)) - tMin) / (tMax - tMin) * w;
  const Y = p => y0 + h - p / 255 * h;
  const pts = [[tMin, 0], [F.off_temp, 0], [F.min_temp, 0], [F.min_temp, F.min_pwm], [F.max_temp, F.max_pwm], [F.emergency_temp, F.max_pwm], [F.emergency_temp, 255], [tMax, 255]];
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H); svg.style.width = '100%'; svg.style.maxWidth = W + 'px';
  const add = (tag, a, txt) => { const e = document.createElementNS(ns, tag); for (const k in a) e.setAttribute(k, a[k]); if (txt) e.textContent = txt; svg.append(e); return e; };
  for (let t = 30; t <= 80; t += 10) { add('line', {x1: X(t), x2: X(t), y1: y0, y2: y0 + h, stroke: '#242a33'}); add('text', {x: X(t), y: H - 8, fill: '#8891a0', 'font-size': 10, 'text-anchor': 'middle', 'font-family': 'monospace'}, t + '°'); }
  [0, 50, 100].forEach(p => { add('line', {x1: x0, x2: x0 + w, y1: Y(p * 2.55), y2: Y(p * 2.55), stroke: '#242a33'}); add('text', {x: x0 - 4, y: Y(p * 2.55) + 3, fill: '#8891a0', 'font-size': 10, 'text-anchor': 'end', 'font-family': 'monospace'}, p + '%'); });
  add('polyline', {points: pts.map(p => X(p[0]) + ',' + Y(p[1])).join(' '), fill: 'none', stroke: '#ff9142', 'stroke-width': 2});
  add('line', {x1: X(F.off_temp), x2: X(F.min_temp), y1: Y(F.min_pwm), y2: Y(F.min_pwm), stroke: '#3ddc97', 'stroke-dasharray': '3 3', 'stroke-width': 1.5});
  const st = S.state.fan;
  if (st && st.temp != null) {
    add('line', {x1: X(st.temp), x2: X(st.temp), y1: y0, y2: y0 + h, stroke: '#4aa3ff', 'stroke-dasharray': '2 3'});
    add('circle', {cx: X(st.temp), cy: Y(st.pwm || 0), r: 4.5, fill: '#4aa3ff'});
  }
  return svg;
}
function fanPanel() {
  const F = Object.assign({mode: 'auto', off_temp: 32, min_temp: 38, max_temp: 62, min_pwm: 80, max_pwm: 255, fixed_pct: 60, emergency_temp: 75, kickstart: true}, S.cfg.fan || {});
  const mode = el('select', null, el('option', {value: 'auto'}, 'Automatisch – Sentinel-Kurve (empfohlen)'), el('option', {value: 'kernel'}, 'Jetson-Standardregelung'),
    el('option', {value: 'fixed'}, 'Feste Drehzahl'), el('option', {value: 'max'}, 'Immer volle Drehzahl')); mode.value = F.mode;
  const offT = numIn(F.off_temp, {min: 20, max: 80}), minT = numIn(F.min_temp, {min: 20, max: 80}), maxT = numIn(F.max_temp, {min: 25, max: 90});
  const minP = numIn(Math.round(F.min_pwm / 2.55), {min: 10, max: 100}), maxP = numIn(Math.round(F.max_pwm / 2.55), {min: 20, max: 100});
  const emT = numIn(F.emergency_temp, {min: 50, max: 95}), fix = numIn(F.fixed_pct, {min: 0, max: 100}), kick = el('input', {type: 'checkbox', checked: F.kickstart !== false});
  const prev = el('div', {id: 'fanCurve', style: {marginTop: '6px'}});
  const cur = () => ({mode: mode.value, off_temp: +offT.value, min_temp: +minT.value, max_temp: +maxT.value, min_pwm: Math.round(+minP.value * 2.55),
    max_pwm: Math.round(+maxP.value * 2.55), emergency_temp: +emT.value, fixed_pct: +fix.value, kickstart: kick.checked});
  const redraw = () => { prev.innerHTML = ''; prev.append(fanCurveSvg(cur()));
    $$('.fan-auto').forEach(e => e.hidden = mode.value !== 'auto'); $$('.fan-fix').forEach(e => e.hidden = mode.value !== 'fixed'); };
  [offT, minT, maxT, minP, maxP, emT, fix, mode].forEach(i => i.addEventListener('input', redraw));
  const st = S.state.fan;
  const p = el('div', {class: 'panel', id: 'fanPanel'}, el('h3', null, 'Lüfter'),
    el('div', {class: 'form'},
      el('label', null, 'Jetzt'), el('span', {class: 'mono', id: 'fanNow'}),
      el('label', null, 'Betriebsart'), mode,
      el('label', {class: 'fan-auto'}, 'Aus unter'), el('div', {class: 'fan-auto row'}, offT, ' °C'),
      el('label', {class: 'fan-auto'}, 'Start bei'), el('div', {class: 'fan-auto row'}, minT, ' °C mit ', minP, ' %'),
      el('label', {class: 'fan-auto'}, 'Voll bei'), el('div', {class: 'fan-auto row'}, maxT, ' °C mit ', maxP, ' %'),
      el('label', {class: 'fan-auto'}, 'Notbetrieb ab'), el('div', {class: 'fan-auto row'}, emT, ' °C immer 100 %'),
      el('label', {class: 'fan-fix'}, 'Feste Drehzahl'), el('div', {class: 'fan-fix row'}, fix, ' %'),
      el('label', null, 'Anlaufhilfe'), el('label', {class: 'chk'}, kick, 'beim Einschalten 1 s voll anlaufen lassen'),
      el('label', {class: 'fan-auto'}, 'Kurve'), el('div', {class: 'fan-auto'}, prev,
        el('div', {class: 'muted small'}, 'orange = Drehzahl je Temperatur · grün gestrichelt = Nachlauf bis „Aus unter“ · blau = jetzt'))),
    st.conflict ? el('p', {class: 'small', style: {color: 'var(--accent)'}}, 'Hinweis: NVIDIAs Dienst nvfancontrol läuft ebenfalls – Betriebsart „Jetson-Standardregelung“ wählen oder nvfancontrol abschalten.') : null,
    el('div', {class: 'actions'}, el('button', {class: 'btn', onclick: () => saveSection('fan', cur(), 'Lüfter-Einstellungen gespeichert')}, 'Speichern'),
      el('button', {class: 'btn btn-ghost', onclick: () => { offT.value = 32; minT.value = 38; maxT.value = 62; minP.value = 31; maxP.value = 100; emT.value = 75; mode.value = 'auto'; redraw(); }}, 'Standardwerte')),
    el('p', {class: 'muted small', style: {marginBottom: 0}}, 'Regelt den Lüfter am 4-Pin-Anschluss des Jetson nach der höchsten CPU/GPU-Temperatur. Beim Beenden von Sentinel übernimmt wieder die Jetson-Standardregelung.'));
  S.fanRedraw = redraw;
  setTimeout(redraw, 0);
  return p;
}
function renderFan() {
  const st = S.state && S.state.fan, n = $('#fanNow'); if (!st || !n) return;
  const t = st.temps || {};
  n.textContent = (st.pct != null ? st.pct + ' %' : '–') + (st.rpm ? ' · ' + st.rpm + ' U/min' : '') + ' · ' + (st.temp != null ? st.temp + ' °C' : '–') +
    (Object.keys(t).length ? '  (' + Object.keys(t).filter(k => !/fan-est/i.test(k)).map(k => k.replace(/-therm/i, '') + ' ' + Math.round(t[k]) + '°').join(', ') + ')' : '');
  n.style.color = st.error ? 'var(--danger)' : '';
  if (st.error) n.textContent += ' · ' + st.error;
  if ($('#fanCurve') && S.fanRedraw) S.fanRedraw();
}
/* Updates von GitHub */
function updatePanel() {
  const U = S.cfg.update || {};
  const auto = el('input', {type: 'checkbox', checked: U.auto_check !== false});
  auto.onchange = () => saveSection('update', {auto_check: auto.checked});
  const repo = el('input', {type: 'text', value: U.repo || 'Scorch1262/Sentinel-MK1'});
  const br = el('input', {type: 'text', value: U.branch || 'main', style: {width: '120px'}});
  const p = el('div', {class: 'panel', id: 'updPanel'}, el('h3', null, 'Updates (GitHub)'),
    el('div', {class: 'form'},
      el('label', null, 'Installiert'), el('span', {class: 'mono', id: 'updCur'}),
      el('label', null, 'Auf GitHub'), el('span', {class: 'mono', id: 'updRem'}),
      el('label', null, 'Stand'), el('span', {class: 'mono small', id: 'updInfo'}),
      el('label', null, 'Automatisch'), el('label', {class: 'chk'}, auto, 'alle 6 Stunden nach Updates suchen'),
      el('label', null, 'Repo / Zweig'), el('div', {class: 'row'}, repo, br,
        el('button', {class: 'btn-mini', onclick: () => saveSection('update', {repo: repo.value.trim(), branch: br.value.trim() || 'main'})}, 'Speichern'))),
    el('pre', {class: 'log', id: 'updChanges', hidden: true, style: {marginTop: '14px', maxHeight: '220px'}}),
    el('div', {class: 'actions'},
      el('button', {class: 'btn btn-ghost', id: 'updCheck', onclick: async () => {
        $('#updCheck').disabled = true; $('#updInfo').textContent = 'prüfe …';
        try { const r = await api('/api/update/check', {body: {}}); S.state.update = r; renderUpdate(); toast(r.error ? r.error : r.available ? 'Update v' + r.remote + ' verfügbar' : 'Sentinel ist aktuell'); }
        catch (e) { toast(e.message); }
        $('#updCheck').disabled = false;
      }}, 'Nach Updates suchen'),
      el('button', {class: 'btn', id: 'updGo', onclick: async () => {
        const u = S.state.update || {};
        const force = !u.available;
        if (!confirm(force ? 'Keine neuere Version gefunden. Version v' + (u.remote || '?') + ' trotzdem neu von GitHub installieren?' :
          'Update von v' + u.version + ' auf v' + u.remote + ' installieren?\n\nEinstellungen, Datenbank und Bilder bleiben erhalten. Sentinel startet danach neu.')) return;
        try { await api('/api/update/install', {body: {force}}); S.updating = u.version; toast('Update wird installiert …', '', 8000); }
        catch (e) { toast(e.message); }
      }}, 'Update installieren')),
    el('p', {class: 'muted small', style: {marginBottom: 0}}, 'Holt die neueste Version aus dem GitHub-Repo, sichert die laufende Version (data/backup/) und startet neu. ',
      'Startet die neue Version nicht, wird die Sicherung automatisch wiederhergestellt.'));
  return p;
}
function renderUpdate() {
  const u = S.state && S.state.update; if (!u || !$('#updCur')) return;
  $('#updCur').textContent = 'v' + u.version;
  $('#updRem').textContent = u.remote ? 'v' + u.remote + (u.available ? '  – neuer!' : '  – aktuell') : '–';
  $('#updRem').style.color = u.available ? 'var(--accent)' : '';
  $('#updInfo').textContent = u.busy ? '⟳ ' + u.step : u.error ? u.error : (u.checked ? 'geprüft ' + fmtTime(u.checked, true) : 'noch nicht geprüft') +
    (u.last_result ? ' · ' + u.last_result : '');
  $('#updInfo').style.color = u.error ? 'var(--danger)' : '';
  const ch = $('#updChanges'); ch.hidden = !(u.available && u.changes); ch.textContent = u.changes || '';
  const b = $('#updGo'); b.disabled = !!u.busy; b.textContent = u.busy ? 'Update läuft …' : u.available ? 'Update auf v' + u.remote + ' installieren' : 'Neu installieren';
  b.className = u.available ? 'btn' : 'btn btn-ghost';
}
async function renderSpaceBars() {
  const box = $('#spaceBars'); if (!box) return;
  let r; try { r = await api('/api/storage/locations'); } catch (e) { return; }
  box.innerHTML = '';
  if (!r.locations.length) { box.append(el('div', {class: 'muted small'}, 'Keine Speicherorte gefunden')); return; }
  const kindName = {data: 'intern', disk: 'Festplatte', usb: 'USB', nas: 'Netzwerk', path: 'Ordner'};
  r.locations.forEach(l => {
    const pct = l.pct != null ? l.pct : 0;
    const cls = !l.reachable ? 'off' : pct >= 90 ? 'crit' : pct >= 75 ? 'warn' : 'ok';
    box.append(el('div', {class: 'sbar ' + cls + (l.active ? ' active' : ''), title: l.path},
      el('div', {class: 'sbar-head'},
        el('b', null, l.name), el('span', {class: 'muted small'}, kindName[l.kind] || l.kind),
        l.active ? el('span', {class: 'badge ok'}, 'Bilder & Videos') : null,
        el('span', {class: 'grow'}),
        el('span', {class: 'mono small'}, l.reachable ? fmtBytes(l.free) + ' frei von ' + fmtBytes(l.total) : 'nicht erreichbar')),
      el('div', {class: 'sbar-track'}, el('div', {class: 'sbar-fill', style: {width: Math.min(100, pct) + '%'}}),
        el('span', {class: 'sbar-pct mono'}, l.reachable ? pct.toFixed(1) + ' % belegt' : ''))));
  });
}
async function updateSettingsStatus() {
  renderUpdate();
  renderSpaceBars();
  const fs = $('#filterStats'), st0 = S.state;
  if (fs && st0 && st0.filter_stats) fs.textContent = 'seit Start: ' + st0.filter_stats.rejected_verify + '× Zweitprüfung, ' +
    st0.filter_stats.rejected_shape + '× Form, ' + (st0.filter_stats.rejected_static || 0) + '× ohne Bewegung, ' + st0.filter_stats.rejected_fp + '× gelernte Fehlerkennung';
  renderFan();
  const st = S.state; if (!st) return;
  const ai = $('#sysAi');
  if (ai) ai.textContent = st.models_ready ? (st.model_title ? st.model_title + ' · ' : '') + st.backend + ' · ' + st.model_size + ' px · ' + (st.det_ms != null ? st.det_ms + ' ms/Bild' : '–') + (st.det_passes > 1 ? ' (' + st.det_passes + ' Durchläufe inkl. Kacheln)' : '') +
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
  showTab(['live', 'gallery', 'objects', 'events', 'alarms', 'settings'].includes(t) ? t : 'live');
  setInterval(pollState, 2000);
  setInterval(pollLive, 900);
})();
