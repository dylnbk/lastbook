'use strict';
/* Build your own library: where it goes, whether it carries an AI model, what it holds, which parts of the world it has maps of,
   and then the build itself. The engine side is engine/wizard/api.py (/api/wizard/*); the contract is docs/WIZARD.md.
   Works on its own page before there is a library, and from inside the app. Plain fetch: in the app the bridge adds the key. */
(() => {
  const $ = s => document.querySelector(s);
  const main = $('#main'), foot = $('#wFoot'), nav = $('#wSteps'), live = $('#live');
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const WIN = document.documentElement.classList.contains('plat-win');
  const UNIT = WIN ? 1024 : 1000;   // each system's own way of counting a gigabyte, so the numbers match Finder or Explorer
  const ICO = {
    ok: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    warn: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    pause: '<path d="M9 6v12M15 6v12"/>', play: '<path d="m8 5 11 7-11 7z"/>', x: '<path d="M6 6l12 12M18 6 6 18"/>',
    next: '<path d="m9 6 6 6-6 6"/>', back: '<path d="M15 18 9 12l6-6"/>', book: '<path d="M4 4v16M9 4v16M14 5l4 15M3 20h18"/>',
  };
  const ico = (k, cls) => `<svg viewBox="0 0 24 24" aria-hidden="true"${cls ? ` class="${cls}"` : ''}>${ICO[k] || ''}</svg>`;
  function size(b) {
    if (b == null || !isFinite(b)) return '';
    const g = b / UNIT ** 3;
    if (g >= 100) return Math.round(g).toLocaleString() + ' GB';
    if (g >= 1) return g.toFixed(1) + ' GB';
    const m = b / UNIT ** 2;
    return (m >= 10 ? Math.round(m) : m >= 0.05 ? m.toFixed(1) : '0') + ' MB';
  }
  const hours = h => h >= 48 ? `${Math.round(h / 24)} days` : h >= 1.5 ? `${Math.round(h)} hours` : h >= 0.75 ? 'an hour' : minutes(h * 60);
  const minutes = m => m >= 90 ? hours(m / 60) : m < 1.5 ? 'a minute' : `${Math.round(m)} minutes`;
  const n = x => Number(x || 0).toLocaleString();
  const home = p => String(p || '').replace(/^\/Users\/[^/]+/, '~').replace(/^[A-Z]:\\Users\\[^\\]+\\/i, '');

  async function ask(base, path, body) {
    const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    let r; try { r = await fetch(base + path, opts) } catch { throw new Error('Lastbook did not answer. Close it and open it again.') }
    let d = null; try { d = await r.json() } catch { }
    if (!r.ok) throw Object.assign(new Error((d && d.error) || (r.status === 404 ? 'Not available in this copy of Lastbook.' : 'Lastbook sent an unreadable reply.')), { status: r.status });
    return d;
  }
  const call = (path, body) => ask('/api/wizard/', path, body);

  /* ------------------------------------------------------------ what has been chosen */
  const STEPS = [['where', 'Where'], ['ai', 'AI'], ['library', 'Library'], ['maps', 'Maps'], ['build', 'Build']];
  const MODE = { small: 'Made smaller', original: 'As they are', words: 'Words only', none: 'Leave out' };
  let S = null;                       // GET state
  let step = 0, reached = 0;
  let T = null, where = '';           // the target as the engine judged it; which card is chosen: 'home', 'open', 'other', or a drive's path
  let portable = false;
  let source = null, M = null, menuErr = '', menuBusy = false;   // what the library is built from, and what that holds
  let docs = new Set(), zims = new Set(), vdefault = 'small', vmodes = {}, encoder = 'software';
  let R = null, maps = new Map(), terrain = true, find = '', openRegions = new Set();
  let AI = null, ai = { mode: 'none' }, seen = {}, repoText = '', repoBusy = false, repoErr = '', custom = null;
  let P = null, planErr = '', planT = 0, planN = 0;
  let effort = 'normal', rate = '';
  let B = null, pollT = 0, startErr = '', confirmCancel = false, busy = '';

  const keep = () => source && source.kind === 'keep';
  function choice() {
    const all = M && M.documents.length && M.documents.every(d => docs.has(d.id));
    return {
      documents: all ? 'all' : [...docs], encyclopedias: [...zims], videos: Object.assign({ default: vdefault }, vmodes), encoder, portable,
      maps: [...maps].map(([id, level]) => ({ id, level, terrain })),
      ai: ai.mode === 'download' && !(seen[ai.repo] && ai.version) ? { mode: 'none' } : ai,     // a repository still being looked at is not yet a choice
    };
  }
  function replan() {
    clearTimeout(planT);
    if (!source || !T) { P = null; return footer() }
    planT = setTimeout(async () => {
      const mine = ++planN;
      try { const p = await call('plan', { source, choice: choice(), target: T.path }); if (mine === planN) { P = p; planErr = '' } }
      catch (e) { if (mine === planN) { P = null; planErr = e.message } }
      footer();
      if (STEPS[step][0] === 'build' && !building()) render();
    }, 180);
  }
  const building = () => B && B.state && ['running', 'stopping', 'paused', 'failed', 'done', 'cancelled'].includes(B.state) && view === 'status';
  let view = 'steps';     // 'steps' while choosing, 'status' once a build exists

  /* ------------------------------------------------------------ the frame: steps above, the total below */
  function frame() {
    nav.hidden = view !== 'steps';
    nav.innerHTML = STEPS.map(([id, name], i) => `<button class="w-step${i < step ? ' done' : ''}" data-step="${i}"${i === step ? ' aria-current="step"' : ''}${i > reached ? ' disabled' : ''}><i>${i + 1}</i>${name}</button>`).join('');
    footer();
  }
  function canGo() {
    const id = STEPS[step][0];
    if (id === 'where') return T && T.ok ? '' : 'Choose where the library goes.';
    if (id === 'library') return !source ? 'Choose what to build from.' : menuBusy ? 'Reading what the library holds…' : !M ? (menuErr || 'Choose what to build from.') : '';
    return '';
  }
  function footer() {
    foot.hidden = view !== 'steps';
    if (foot.hidden) return;
    const id = STEPS[step][0], why = canGo();
    let total = '<b>Nothing chosen yet</b>', cls = '';
    if (planErr) { total = `<b>${esc(planErr)}</b>`; cls = ' bad' }
    else if (P) {
      const parts = [];
      if (P.download_bytes) parts.push(`${size(P.download_bytes)} to download`);
      if (P.free_bytes != null) parts.push(`${size(P.free_bytes)} free on ${esc(T.disk || 'the disk')}`);
      const pct = P.free_bytes ? Math.min(100, Math.round((P.bytes + P.working_bytes) / (P.free_bytes + (P.already_bytes || 0)) * 100)) : 0;
      cls = P.fits === false ? ' bad' : '';
      total = `<b>${keep() ? 'Adds' : 'This library:'} ${size(P.bytes)}</b><div class="s-meter${cls}"><i style="width:${pct}%"></i></div>
        <small>${P.fits === false ? `Needs ${size(P.short_bytes)} more room than there is. ` : ''}${parts.join(' · ')}</small>`;
    } else if (T) total = `<b>${size(T.free_bytes)} free</b><small>on ${esc(T.disk || 'the disk')}</small>`;
    foot.innerHTML = `<div class="w-foot-in"><div class="w-total${cls}">${total}</div><div class="w-nav">
      ${step > 0 ? `<button class="btn plain" data-go="-1">${ico('back')}Back</button>` : ''}
      ${id === 'build' ? '' : `<button class="btn" data-go="1"${why ? ' disabled title="' + esc(why) + '"' : ''}>Next${ico('next')}</button>`}</div></div>`;
  }
  function go(to) {
    if (to > step && canGo()) return;
    step = Math.max(0, Math.min(STEPS.length - 1, to));
    reached = Math.max(reached, step);
    startErr = '';
    render(true);
    const id = STEPS[step][0];
    if (id === 'ai' && !AI) loadAI();
    if (id === 'library') loadSources(0);
    if (id === 'maps' && !R) loadRegions();
    if (id === 'build') replan();
  }
  /* The list of published libraries is fetched when the page is first asked for it, and arrives a moment later. */
  let sourcesTried = false;
  async function loadSources(tries) {
    if ((S.sources || []).some(s => s.kind === 'pack')) { sourcesTried = true; return }
    try { const s = await call('state'); S.sources = s.sources; S.manifest = s.manifest } catch { }
    const have = (S.sources || []).some(s => s.kind === 'pack');
    if (!have && tries < 6) return setTimeout(() => loadSources(tries + 1), 1200);
    sourcesTried = true;
    if (STEPS[step][0] === 'library' && !menuBusy) keepScroll(libraryView);
  }
  function render(focus) {
    clearTimeout(pollT);
    if (view === 'status') statusView();
    else ({ where: whereView, ai: aiView, library: libraryView, maps: mapsView, build: buildView })[STEPS[step][0]]();
    frame();
    if (focus) { const h = main.querySelector('h1'); if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }) } scrollTo(0, 0) }
  }
  const say = t => { live.textContent = t };

  /* The app's window has the system's folder chooser. A browser on the server version has none, so the path is typed. */
  const IN_WINDOW = location.protocol === 'lastbook:' || location.hostname === 'lastbook.invalid';
  async function typed() {
    const p = (prompt('The whole path of the folder:') || '').trim();
    return p ? call('target', { path: p }) : { cancelled: true };
  }

  /* ------------------------------------------------------------ 1. where */
  function whereView() {
    const cards = [];
    const card = (value, title, sub, badge, off) => `<label class="s-opt${off ? ' off' : ''}"><input type="radio" name="where" value="${esc(value)}"${where === value ? ' checked' : ''}${off ? ' disabled' : ''}>
      <span class="s-opt-in"><span class="s-opt-h"><b>${title}</b>${badge || ''}</span><small>${sub}</small></span></label>`;
    for (const d of S.drives || [])
      cards.push(card(d.path, `${esc(d.name)}`, `${size(d.free_bytes)} free of ${size(d.total_bytes)}. ${esc(!d.writable && d.ok ? 'Lastbook cannot save to this drive.' : d.note)}`,
        d.travels === 'all' && d.ok ? '<span class="s-badge rec">Opens anywhere</span>' : d.removable ? '<span class="s-badge">A drive</span>' : '', !d.ok || !d.writable));
    cards.push(card('home', 'This computer', `In ${esc(home(S.default_target))}`, ''));
    if (S.current) cards.push(card('open', 'The library that is open', `Add maps or an AI model to it. What it holds stays as it is. ${esc(home(S.current.path))}`, '<span class="s-badge here">Open now</span>'));
    cards.push(card('other', 'Another folder', where === 'other' && T ? esc(home(T.path)) : 'Choose a folder on any disk.', ''));
    const note = !T ? '' : !T.ok ? `<p class="s-note" role="alert">${esc(T.note)}</p>`
      : `<p class="s-note ok">${esc(T.library && where !== 'open' ? 'There is a library in this folder already. Building here adds to it; the next step lets you keep what it holds. ' : '')}${esc(T.note)}</p>`;
    const again = T && T.built && T.built.choice && where !== 'open'
      ? `<p class="s-note">A build was started here${T.built.at ? ' on ' + esc(T.built.at.slice(0, 10)) : ''}. <button class="s-link" data-act="same">Use the same choices</button></p>` : '';
    main.innerHTML = `<h1>Build your own library</h1>
      <p class="lead">Choose what goes in it, which parts of the world it has maps of, and whether it carries an AI model. Lastbook builds it on this computer, from the published library and open map data. It needs the internet while it builds, and never after.</p>
      <h2>Where should it go?</h2><div class="s-opts">${cards.join('')}</div>${note}${again}
      ${T && T.ok && where !== 'open' ? `<div class="w-group"><label class="w-row"><input type="checkbox" id="wPortable"${portable ? ' checked' : ''}><span class="w-name"><b>A library that travels</b>
        <small>Everything stays in its folder: notes, settings and the app itself. Plug the drive into another computer and open it there; nothing is left behind on either.</small></span></label></div>` : ''}`;
  }
  async function setWhere(value, path) {
    where = value;
    if (value === 'other' && !path) {
      let t; try { t = IN_WINDOW ? await call('pick', {}) : await typed() } catch (e) { t = { ok: false, note: e.message, path: '' } }
      if (!t || t.cancelled) { if (!T) where = ''; return render() }
      T = t;
    } else {
      const p = path || (value === 'home' ? S.default_target : value === 'open' ? S.current.path : value);
      try { T = await call('target', { path: p }) } catch (e) { T = { ok: false, note: e.message, path: p } }
    }
    const drive = (S.drives || []).find(d => d.path === value);
    portable = !!(T.portable || (drive && drive.removable));
    if (value === 'open') { source = { kind: 'keep' }; M = null; loadMenu() }
    else if (keep()) { source = null; M = null }
    R = null;       // which regions are built already depends on where
    render(); replan();
  }

  /* ------------------------------------------------------------ 2. AI */
  async function loadAI() {
    try { AI = await call('ai', { target: T ? T.path : null }) } catch (e) { AI = { here: [], presets: [], error: e.message } }
    if (STEPS[step][0] === 'ai') render();
  }
  const fitLine = f => !f ? '' : f.fits === false ? `<span class="w-fit no">Too large for this computer.</span> ${esc(f.why || '')}`
    : f.fits == null ? esc(f.why || '') : `<span class="w-fit ${f.tight ? 'tight' : 'yes'}">${f.tight ? 'Fits, with little to spare.' : 'Fits this computer.'}</span> ${esc(f.why || '')}`;
  function versionOf(found, name) { return (found.versions || []).find(v => v.name === name) }
  function aiView() {
    if (!AI) { main.innerHTML = '<h1>An AI model to answer questions</h1><p class="loading">Looking for models on this computer…</p>'; return }
    const radio = (value, title, sub, badge) => `<label class="s-opt"><input type="radio" name="ai" value="${esc(value)}"${aiValue() === value ? ' checked' : ''}>
      <span class="s-opt-in"><span class="s-opt-h"><b>${title}</b>${badge || ''}</span><small>${sub}</small></span></label>`;
    const here = (AI.here || []).map(m => radio('here:' + m.id, esc(m.name), `${size(m.bytes)} · ${esc(m.origin || 'On this computer')}${m.vision ? ' · reads page images' : ''}<br>${fitLine(m.fit)}`, '<span class="s-badge here">On this computer</span>'));
    const presets = (AI.presets || []).map(p => {
      const f = seen[p.repo], v = f && versionOf(f, (ai.repo === p.repo && ai.version) || f.recommended);
      return radio('get:' + p.repo, esc(p.title), `${esc(p.about)}${v ? `<br>${size(v.total_bytes)} to download. ${fitLine(v.fit)}` : ''}`, '');
    });
    const c = custom && seen[custom];
    const versions = c ? `<div class="w-group">${c.versions.map(v => `<label class="w-row"><input type="radio" name="version" value="${esc(v.name)}"${ai.mode === 'download' && ai.repo === custom && ai.version === v.name ? ' checked' : ''}>
        <span class="w-name"><b>${esc(v.quant || v.name)}${v.name === c.recommended ? ' <span class="s-badge rec">Suggested</span>' : ''}</b><small>${fitLine(v.fit)}</small></span><span class="w-size">${size(v.total_bytes)}</span></label>`).join('')}</div>
        <p class="w-sub">${esc(c.name || custom)}${c.license ? ` · licence: ${esc(c.license)}` : ''}${c.vision ? ' · reads page images' : ''}</p>` : '';
    const isHere = ai.mode === 'computer', m = isHere && (AI.here || []).find(x => x.id === ai.id);
    main.innerHTML = `<h1>An AI model to answer questions</h1>
      <p class="lead">Optional. Search, the guides and the maps all work without one. A model reads the library and answers in sentences, naming the pages it used. It runs on the computer the library is opened on, with no connection.${AI.ram_bytes ? ` This computer has ${Math.round(AI.ram_bytes / 1024 ** 3)} GB of memory.` : ''}</p>
      <div class="s-opts">${radio('none', 'No AI model', 'One can be added later, in Chat.', '')}${here.join('')}</div>
      ${isHere && m ? `<div class="w-group"><label class="w-row"><input type="checkbox" id="wCopy"${ai.copy ? ' checked' : ''}><span class="w-name"><b>Copy it into the library</b>
        <small>${portable ? 'A library that travels needs its own copy: on another computer the one here is not there.' : 'Without a copy the library uses the one here, and has no model if that is removed.'}</small></span><span class="w-size">${size(m.bytes)}</span></label></div>` : ''}
      <h2>Or download one</h2><div class="s-opts">${presets.join('')}</div>
      <h2>Or any model from Hugging Face</h2>
      <p class="w-sub">Paste the name of a repository that holds the model as GGUF files, for example <code>unsloth/gemma-4-12B-it-GGUF</code>. Lastbook reads what is in it and says whether each version fits this computer before anything is downloaded.</p>
      <div class="w-repo"><input id="wRepo" type="text" spellcheck="false" autocapitalize="off" autocomplete="off" placeholder="owner/name" value="${esc(repoText)}" aria-label="Hugging Face repository">
        <button class="btn plain" data-act="look"${repoBusy ? ' disabled' : ''}>${repoBusy ? '<span class="w-busy"></span>Looking…' : 'Look'}</button></div>
      ${repoErr ? `<p class="s-note" role="alert">${esc(repoErr)}</p>` : ''}${versions}
      ${AI.runtime === false && ai.mode !== 'none' ? '<p class="s-note">This copy of Lastbook has no program to run a model on this computer, so the model will be in the library but will not answer here. It will on a computer that has one.</p>' : ''}`;
  }
  const aiValue = () => ai.mode === 'computer' ? 'here:' + ai.id : ai.mode === 'download' && ai.repo !== custom ? 'get:' + ai.repo : ai.mode === 'download' ? 'custom' : 'none';
  async function look(repo, isCustom) {
    repoBusy = isCustom; repoErr = '';
    if (isCustom) render();
    try {
      const f = await call('inspect', { repo, target: T ? T.path : null });
      seen[f.repo] = f;
      if (isCustom) { custom = f.repo; repoText = f.repo }
      const preset = (AI.presets || []).find(p => p.repo === f.repo);
      const want = (preset && f.versions.find(v => (v.quant || '') === preset.quant)) || versionOf(f, f.recommended) || f.versions[0];
      if (!isCustom || want) ai = { mode: 'download', repo: f.repo, version: want ? want.name : null, draft: !!(f.draft && (!preset || preset.draft)) };
    } catch (e) {
      if (isCustom) repoErr = e.message; else { repoErr = e.message; ai = { mode: 'none' } }
    }
    repoBusy = false;
    if (STEPS[step][0] === 'ai') render();
    replan();
  }

  /* ------------------------------------------------------------ 3. what the library holds */
  async function loadMenu() {
    M = null; menuErr = ''; menuBusy = true;
    const mine = source;
    if (STEPS[step][0] === 'library') render();
    try {
      const m = await call('menu', { source });
      if (mine !== source) return;
      M = m;
      docs = new Set(m.documents.map(d => d.id)); zims = new Set(m.encyclopedias.map(z => z.id)); vmodes = {};
      vdefault = m.can_shrink ? 'small' : 'words';
      if (pending) { applyChoice(pending); pending = null }
    } catch (e) { if (mine === source) menuErr = e.message }
    menuBusy = false;
    if (STEPS[step][0] === 'library') render();
    replan();
  }
  let pending = null;     // the choices of a build begun before, waiting for the menu they apply to
  function applyChoice(c) {
    if (!c) return;
    if (M && !M.keep) {
      docs = new Set(c.documents === 'all' ? M.documents.map(d => d.id) : (c.documents || []).filter(id => M.documents.some(d => d.id === id)));
      zims = new Set(c.encyclopedias === 'all' ? M.encyclopedias.map(z => z.id) : (c.encyclopedias || []).filter(id => M.encyclopedias.some(z => z.id === id)));
      const v = Object.assign({}, c.videos || {});
      vdefault = v.default || 'none'; delete v.default; vmodes = v;
    }
    encoder = c.encoder || 'software';
    maps = new Map((c.maps || []).map(m => [m.id, m.level || 'detail']));
    terrain = !(c.maps || []).some(m => m.terrain === false);
  }
  const srcKey = s => s ? s.kind + ':' + (s.id || s.path || '') : '';
  function videoBytes(g, mode) { return mode === 'none' ? 0 : mode === 'words' ? g.words_bytes : mode === 'small' ? g.small_bytes : g.bytes }
  function libraryView() {
    const srcs = (S.sources || []).filter(s => !(T && T.is_current)).map(s => ({ s: s.kind === 'pack' ? { kind: 'pack', id: s.id } : { kind: 'folder', path: s.path }, title: s.title, sub: s.summary, bytes: s.installed_bytes }));
    if (T && T.library) srcs.unshift({ s: { kind: 'keep' }, title: 'Keep what this library holds', sub: 'Nothing in it is changed. Only maps and an AI model are added.' });
    if (source && source.kind === 'folder' && !srcs.some(x => srcKey(x.s) === srcKey(source))) srcs.push({ s: source, title: 'A library on this computer', sub: esc(home(source.path)) });
    const cards = srcs.map(x => `<label class="s-opt"><input type="radio" name="source" value="${esc(JSON.stringify(x.s))}"${srcKey(x.s) === srcKey(source) ? ' checked' : ''}>
      <span class="s-opt-in"><span class="s-opt-h"><b>${esc(x.title)}</b></span><small>${x.sub || ''}</small>${x.bytes ? `<span class="s-sizes"><span>${size(x.bytes)} whole</span></span>` : ''}</span></label>`).join('');
    let body = '';
    if (menuBusy) body = '<p class="loading"><span class="w-busy"></span>Reading what the library holds…</p>';
    else if (menuErr) body = `<p class="s-note" role="alert">${esc(menuErr)} <button class="s-link" data-act="menu-again">Try again</button></p>`;
    else if (M && M.keep) body = '<p class="s-note ok">The library stays as it is. Choose maps and an AI model to add in the other steps.</p>';
    else if (M) {
      const dsum = M.documents.filter(d => docs.has(d.id)), zsum = M.encyclopedias.filter(z => zims.has(z.id));
      const opt = (m, cur, g) => `<option value="${m}"${m === cur ? ' selected' : ''}${m === 'small' && !M.can_shrink ? ' disabled' : ''}>${MODE[m]}${g ? ' · ' + size(videoBytes(g, m)) : ''}</option>`;
      const vtotal = M.videos.reduce((t, g) => t + videoBytes(g, vmodes[g.id] || vdefault), 0);
      body = `<div class="w-h"><h2>Documents</h2><span>${n(dsum.reduce((t, d) => t + d.count, 0))} chosen · ${size(dsum.reduce((t, d) => t + d.bytes, 0))}</span></div>
        <p class="w-quick"><button class="s-link" data-act="docs-essential">The essentials</button><button class="s-link" data-act="docs-all">All</button><button class="s-link" data-act="docs-none">None</button></p>
        <div class="w-group">${M.documents.map(d => `<label class="w-row"><input type="checkbox" data-doc="${esc(d.id)}"${docs.has(d.id) ? ' checked' : ''}><span class="w-name"><b>${esc(d.title)}</b><small>${n(d.count)} documents and saved pages</small></span><span class="w-size">${size(d.bytes)}</span></label>`).join('')}</div>
        ${M.encyclopedias.length ? `<div class="w-h"><h2>Encyclopedias</h2><span>${zsum.length} chosen · ${size(zsum.reduce((t, z) => t + z.bytes, 0))}</span></div>
        <div class="w-group">${M.encyclopedias.map(z => `<label class="w-row"><input type="checkbox" data-zim="${esc(z.id)}"${zims.has(z.id) ? ' checked' : ''}><span class="w-name"><b>${esc(z.title)}</b></span><span class="w-size">${size(z.bytes)}</span></label>`).join('')}</div>` : ''}
        ${M.videos.length ? `<div class="w-h"><h2>Videos</h2><span>${size(vtotal)}</span></div>
        <p class="w-sub"><b>Made smaller</b>: re-encoded on this computer to about a tenth of the size (480p), still clear enough to follow what the hands are doing. Videos that are small already are left alone. <b>Words only</b>: no picture, but everything said is kept, searched and cited.${M.can_shrink ? '' : ' <b>This computer has no ffmpeg that writes HEVC, so videos cannot be made smaller here.</b>'}</p>
        <div class="w-group"><label class="w-row"><span class="w-name"><b>All videos</b><small>Unless a group below says otherwise</small></span><select data-vmode="default" aria-label="All videos">${M.modes.map(m => opt(m, vdefault)).join('')}</select></label>
        ${M.videos.map(g => `<label class="w-row"><span class="w-name"><b>${esc(g.title)}</b><small>${n(g.count)} video${g.count === 1 ? '' : 's'} · ${g.seconds >= 5400 ? Math.round(g.seconds / 3600) + ' hours' : Math.max(1, Math.round(g.seconds / 60)) + ' minutes'}${g.channel ? ' · one channel' : ''}</small></span>
          <select data-vmode="${esc(g.id)}" aria-label="${esc(g.title)}"><option value=""${vmodes[g.id] ? '' : ' selected'}>As above · ${size(videoBytes(g, vdefault))}</option>${M.modes.map(m => opt(m, vmodes[g.id], g)).join('')}</select></label>`).join('')}</div>
        ${M.can_shrink_hardware && (vdefault === 'small' || Object.values(vmodes).includes('small')) ? `<div class="w-group">
          <label class="w-row"><input type="radio" name="encoder" value="software"${encoder === 'software' ? ' checked' : ''}><span class="w-name"><b>Smallest, and the better picture</b><small>Slow: the processor does the work. Days for the whole library; it can be paused and carried on.</small></span></label>
          <label class="w-row"><input type="radio" name="encoder" value="hardware"${encoder === 'hardware' ? ' checked' : ''}><span class="w-name"><b>Fastest</b><small>This computer’s video chip does the work, three or four times as fast. The videos come out about twice the size, and rougher.</small></span></label></div>` : ''}` : ''}`;
    }
    main.innerHTML = `<h1>What the library holds</h1><p class="lead">Start from one of the published libraries and keep the parts you want. Only what you keep is downloaded.</p>
      <h2>Start from</h2><div class="s-opts">${cards}</div>
      ${(S.sources || []).some(s => s.kind === 'pack') || (T && T.is_current) ? '' : sourcesTried ? '<p class="s-note">The list of published libraries could not be had: there may be no connection. A library folder already on this computer can still be built from.</p>' : '<p class="loading"><span class="w-busy"></span>Asking which libraries are published…</p>'}
      ${T && T.is_current ? '' : `<p><button class="s-link" data-act="pick-source">${ico('folder')}A library folder already on this computer…</button></p>`}${body}`;
  }

  /* ------------------------------------------------------------ 4. maps */
  async function loadRegions() {
    try { R = await call('regions', { target: T ? T.path : null }) } catch (e) { R = { regions: [], built: [], error: e.message } }
    R.by = new Map(R.regions.map(r => [r.id, r]));
    R.kids = new Map();
    for (const r of R.regions) { if (!R.kids.has(r.parent || '')) R.kids.set(r.parent || '', []); R.kids.get(r.parent || '').push(r) }
    for (const b of R.built || []) if (R.by.has(b.id) && !maps.has(b.id) && keep()) { /* already there: shown, not chosen again */ }
    if (STEPS[step][0] === 'maps') render();
  }
  function est(r, level) {
    if (!r.mb || !R.ratios) return null;
    const k = R.ratios[level], area = Math.max(0, (r.box[2] - r.box[0]) * (r.box[3] - r.box[1]));
    return (r.mb * k.data + (terrain ? area * k.terrain_mb : 0)) * 1e6;
  }
  const builtAs = id => { const b = (R.built || []).find(b => b.id === id); return b ? b.level : null };
  function regionRow(r, inner, withParent) {
    const kids = (R.kids.get(r.id) || []).filter(k => !k.composite), level = maps.get(r.id), was = builtAs(r.id);
    const big = !r.mb || r.summed && !r.parts;          // a continent: chosen a country at a time
    const sizes = big || r.composite ? '' : `outline about ${size(est(r, 'overview'))} · full detail about ${size(est(r, 'detail'))}`;
    const seg = big || r.composite ? '' : `<span class="w-seg" role="group" aria-label="${esc(r.name)}">
      <button data-map="${esc(r.id)}" data-level="overview" aria-pressed="${level === 'overview'}">Outline</button><button data-map="${esc(r.id)}" data-level="detail" aria-pressed="${level === 'detail'}">Full detail</button></span>`;
    const more = kids.length ? `<button class="w-more" data-open="${esc(r.id)}" aria-expanded="${openRegions.has(r.id)}" aria-label="Parts of ${esc(r.name)}">${ico('next')}</button>` : '';
    const parent = withParent && r.parent && R.by.get(r.parent) ? R.by.get(r.parent).name + ' · ' : '';
    let out = `<div class="w-row${inner ? ' in' : ''}"><span class="w-name"><b>${esc(r.name)}${was ? ` <span class="s-badge here">Built: ${was === 'detail' ? 'full detail' : 'outline'}</span>` : ''}</b><small>${esc(parent)}${sizes}${kids.length && !sizes ? `${kids.length} parts` : ''}</small></span>${more}${seg}</div>`;
    if (kids.length && openRegions.has(r.id) && !withParent) out += kids.map(k => regionRow(k, true)).join('');
    return out;
  }
  function mapsView() {
    if (!R) { main.innerHTML = '<h1>Maps</h1><p class="loading">Reading the list of regions…</p>'; return }
    const chosen = [...maps].map(([id, level]) => { const r = R.by.get(id) || { name: id }; return `<span class="w-chip">${esc(r.name)} · ${level === 'detail' ? 'full detail' : 'outline'}<button data-unmap="${esc(id)}" aria-label="Take out ${esc(r.name)}">${ico('x')}</button></span>` });
    const built = (R.built || []).filter(b => !maps.has(b.id)).map(b => `<span class="w-chip built">${esc(b.name || b.id)} · ${b.level === 'detail' ? 'full detail' : 'outline'} · built</span>`);
    const q = find.trim().toLowerCase();
    let list;
    if (q) {
      const hits = R.regions.filter(r => !r.composite && r.mb && !(r.summed && !r.parts) && (r.name.toLowerCase().includes(q) || r.id.toLowerCase().includes(q))).slice(0, 40);
      list = hits.length ? hits.map(r => regionRow(r, false, true)).join('') : '<p class="w-row"><span class="w-name"><small>No region by that name. Regions are countries, and the states or provinces of the large ones.</small></span></p>';
    } else {
      list = (R.kids.get('') || []).map(c => {
        const kids = (R.kids.get(c.id) || []).filter(k => !k.composite), groups = (R.kids.get(c.id) || []).filter(k => k.composite && (R.kids.get(k.id) || []).length);
        if (!kids.length && !groups.length) return regionRow(c);
        const all = kids.filter(k => k.mb), total = all.reduce((t, k) => t + (est(k, 'overview') || 0), 0);
        return `<details${openRegions.has('c:' + c.id) ? ' open' : ''} data-cont="${esc(c.id)}"><summary><span>${esc(c.name)}</span><small>${kids.length} countries and regions</small></summary>
          ${all.length > 1 ? `<div class="w-row"><span class="w-name"><b>All of ${esc(c.name)} in outline</b><small>${all.length} regions · about ${size(total)}</small></span><button class="btn plain" data-all="${esc(c.id)}">Add them all</button></div>` : ''}
          ${kids.map(k => regionRow(k)).join('')}${groups.map(g => regionRow(g)).join('')}</details>`;
      }).join('');
    }
    main.innerHTML = `<h1>Maps</h1>
      <p class="lead">Every library has the outline of the world’s countries. Add the regions you want more of. <b>Full detail</b> has every street, path, building and help point, and finds an address. <b>Outline</b> has the main roads, towns, hospitals and other help points: enough to find the way across a country, at a twentieth of the size.</p>
      ${R.can_build === false ? '<p class="s-note" role="alert">This copy of Lastbook cannot build maps: the part that reads OpenStreetMap data (osmium) is not in it.</p>' : ''}
      ${chosen.length || built.length ? `<div class="w-chips">${chosen.join('')}${built.join('')}</div>` : ''}
      <div class="w-group"><label class="w-row"><input type="checkbox" id="wTerrain"${terrain ? ' checked' : ''}><span class="w-name"><b>With the shape of the land</b><small>Hills, contour lines and the 3D view, from the Copernicus elevation model. Adds about a tenth to a region in full detail.</small></span></label></div>
      <input class="w-find" id="wFind" type="search" placeholder="Find a country or region" value="${esc(find)}" aria-label="Find a country or region">
      <div class="w-group w-tree" id="wTree">${list}</div>
      <p class="w-sub">Built on this computer from OpenStreetMap (through Geofabrik’s extracts${R.sizes_as_of ? `, sizes as of ${esc(R.sizes_as_of)}` : ''}), the Copernicus elevation model and GeoNames. Sizes are estimates: a region of dense streets comes out larger than one of open country.</p>`;
  }

  /* ------------------------------------------------------------ 5. the build */
  function summary() {
    const c = P ? P.counts || {} : {};
    const lib = keep() ? 'Kept as it is' : !P ? '…' : `${n(c.documents)} documents, ${n(c.encyclopedias)} encyclopedias, ${n(c.videos)} videos${c.videos_small ? ` (${n(c.videos_small)} made smaller)` : ''}${c.videos_words ? ` (${n(c.videos_words)} by their words only)` : ''}
      <small>${esc(M ? M.title : '')} · ${size(P.pack_bytes)}</small>`;
    const mp = maps.size ? [...maps].map(([id, level]) => `${esc((R && R.by.get(id) || { name: id }).name)} (${level === 'detail' ? 'full detail' : 'outline'})`).join(', ') + (P ? `<small>about ${size(P.map_bytes)}${terrain ? ', with the shape of the land' : ''}</small>` : '') : 'None added';
    const a = ai.mode === 'none' ? 'None' : ai.mode === 'computer' ? esc(((AI && AI.here || []).find(m => m.id === ai.id) || {}).name || 'A model on this computer') + `<small>${ai.copy ? 'Copied into the library' : 'Used where it is on this computer'}</small>`
      : esc(ai.repo) + `<small>${esc(ai.version || '')}${P ? ' · ' + size(P.ai_bytes) : ''}</small>`;
    const time = [];
    if (P && P.map_minutes) time.push(`Maps: about ${minutes(P.map_minutes)}, after their data is downloaded. The download includes the world’s coastline (${size(P.coast_bytes)}), needed once however many regions there are; it is deleted when the maps are built.`);
    if (P && P.videos_to_shrink) time.push(`Videos: about ${hours(P.encode_hours)} of this computer’s time to make ${n(Math.round(P.video_hours))} hours of video smaller. That is a guess from one computer; an older one takes longer. The library opens as soon as its documents are in, and videos fill in as they are done.`);
    return `<dl class="w-sum">
      <div><dt>Where</dt><dd>${esc(home(T.path))}<small>${esc(T.format || '')}${portable ? ' · a library that travels' : ''}</small></dd></div>
      <div><dt>Library</dt><dd>${lib}</dd></div><div><dt>Maps</dt><dd>${mp}</dd></div><div><dt>AI model</dt><dd>${a}</dd></div>
      <div><dt>Size</dt><dd>${P ? size(P.bytes) : '…'}${P ? `<small>${P.download_bytes ? size(P.download_bytes) + ' to download' : 'Nothing to download'}${P.working_bytes ? `; ${size(P.working_bytes)} more room while it builds` : ''} · ${size(P.free_bytes)} free</small>` : ''}</dd></div>
      ${time.length ? `<div><dt>Time</dt><dd><small>${time.join('<br>')}</small></dd></div>` : ''}</dl>`;
  }
  function buildView() {
    const nothing = keep() && !maps.size && ai.mode === 'none';
    const cant = planErr || (P && P.fits === false ? `This needs ${size(P.short_bytes)} more room than ${esc(T.disk || 'the disk')} has.` : '') || (nothing ? 'Nothing is chosen to add: go back and choose a map or an AI model.' : '');
    const e = S.efforts || {};
    const eff = (id, title, sub) => `<label class="w-row"><input type="radio" name="effort" value="${id}"${effort === id ? ' checked' : ''}><span class="w-name"><b>${title}</b><small>${sub}</small></span></label>`;
    main.innerHTML = `<h1>Ready to build</h1><p class="lead">Check the choices, then build. It can be paused at any time and carried on later, and it picks up where it stopped.</p>
      ${summary()}
      <h2>How hard may this computer work?</h2>
      <div class="w-group">${eff('gentle', 'Gently', 'In the background, on ' + ((e.gentle || {}).threads || 2) + ' processor threads. The computer stays free for other work; the build takes several times as long.')}
        ${eff('normal', 'Normally', 'At low priority, on ' + ((e.normal || {}).threads || 6) + ' of ' + (S.cores || '') + ' processor threads. Other work stays quick.')}
        ${eff('full', 'As fast as it can', 'Every processor thread. For a computer left alone to get on with it.')}
        <label class="w-row"><span class="w-name"><b>Downloads</b><small>Leave room on the line for everyone else on it.</small></span><select id="wRate" aria-label="Download speed limit">
          ${[['', 'No limit'], ['2', 'At most 2 MB/s'], ['5', 'At most 5 MB/s'], ['10', 'At most 10 MB/s'], ['20', 'At most 20 MB/s']].map(([v, t]) => `<option value="${v}"${rate === v ? ' selected' : ''}>${t}</option>`).join('')}</select></label></div>
      ${startErr ? `<div class="s-err" role="alert">${ico('warn')}<div><b>The build did not start.</b><p>${esc(startErr)}</p></div></div>` : ''}
      ${cant ? `<p class="s-note" role="alert">${cant}</p>` : ''}
      <div class="s-go left"><button class="btn big" data-act="start"${cant || !P || busy ? ' disabled' : ''}>${busy === 'start' ? '<span class="w-busy"></span>Starting…' : keep() ? 'Add to the library' : 'Build this library'}</button>
        <small class="muted">Keep Lastbook open while it builds: closing it pauses the build.</small></div>`;
  }

  const PHASES = [['maps', 'Maps'], ['files', 'Documents'], ['library', 'Catalog'], ['ai', 'AI model'], ['videos', 'Videos'], ['finishing', 'Finishing']];
  function statusView() {
    const b = B || {}, job = b.job || {}, c = job.choice || {};
    const had = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.act : '';
    const kept = job.source && job.source.kind === 'keep';
    const phases = PHASES.filter(([id]) => id === 'maps' ? (c.maps || []).length : id === 'ai' ? c.ai && c.ai.mode !== 'none' : id === 'videos' ? !kept && b.videos_total !== 0 : !kept);
    const at = b.phase === 'done' ? phases.length : phases.findIndex(p => p[0] === b.phase);      // -1 while it is still reading what the pack holds
    const stopped = ['paused', 'failed', 'cancelled'].includes(b.state);
    const head = { running: kept ? 'Adding to the library' : 'Building the library', stopping: 'Stopping…', paused: 'Paused', failed: 'The build stopped', cancelled: 'The build was cancelled', done: kept ? 'Added to the library' : 'The library is ready' }[b.state] || 'Building';
    let pct = null, line = b.state === 'done' ? '' : b.message || '';      // done: the heading has said so
    if (b.phase === 'files' && b.total_bytes) { pct = b.done_bytes / b.total_bytes; line += ` · ${size(b.done_bytes)} of ${size(b.total_bytes)}` }
    else if (b.phase === 'videos' && b.videos_total) pct = b.videos_done / b.videos_total;
    else if (b.phase === 'ai' && b.ai_total_bytes) { pct = b.ai_done_bytes / b.ai_total_bytes; line += ` · ${size(b.ai_done_bytes)} of ${size(b.ai_total_bytes)}` }
    else if (b.phase === 'library' && b.index_total_bytes) { pct = b.index_done_bytes / b.index_total_bytes; line += ` · ${size(b.index_done_bytes)} of ${size(b.index_total_bytes)}` }
    else if (b.phase === 'maps' && b.maps_total) pct = null;
    const usable = !kept && ['ai', 'videos', 'finishing', 'done'].includes(b.phase);
    const isOpen = S.current && job.target === S.current.path;
    const lead = b.state === 'running' ? `You can keep using this computer. Closing Lastbook pauses the build.${usable && b.phase !== 'done' ? ' The library can be opened already: videos still to be made smaller show their words until their turn comes.' : ''}`
      : b.state === 'paused' ? 'Everything built so far is kept. Carry on when you are ready.'
        : b.state === 'cancelled' ? `What was built is still in ${esc(home(job.target))}. Delete that folder to take it back, or build again to finish it.`
          : b.state === 'done' ? (isOpen ? 'It is in the library now.' : `It is in ${esc(home(job.target))}.`) : '';
    main.innerHTML = `<h1>${head}</h1>${lead ? `<p class="lead">${lead}</p>` : ''}
      ${b.state === 'failed' ? `<div class="s-err" role="alert">${ico('warn')}<div><b>${esc(b.error || 'Something went wrong.')}</b><p>What was built is kept. Carry on to try again from where it stopped.</p></div></div>` : ''}
      <div class="s-items"><div class="s-item${b.state === 'failed' ? ' bad' : ''}"><div class="s-item-h">${ico('book', 's-ri')}<b>${esc(home(job.target || ''))}</b></div>
        ${b.state === 'done' ? '' : `<div class="s-bar${pct == null && b.state === 'running' ? ' busy' : ''}"><i style="width:${pct == null ? 0 : Math.round(pct * 1000) / 10}%"></i></div>`}
        <p class="s-line">${esc(line)}</p>
        <ol class="s-steps">${phases.map(([id, name], i) => `<li class="${i < at ? 'done' : i === at && b.state !== 'done' ? (stopped ? 'stop' : 'now') : ''}">${name}${id === 'videos' && b.videos_total ? ` ${n(b.videos_done)} of ${n(b.videos_total)}` : id === 'maps' && b.maps_total ? ` ${n(b.maps_done || 0)} of ${n(b.maps_total)}` : ''}</li>`).join('')}</ol></div></div>
      ${(b.problems || []).length ? `<ul class="w-problems">${b.problems.slice(0, 30).map(p => `<li>${esc(p)}</li>`).join('')}${b.problems.length > 30 ? `<li>…and ${b.problems.length - 30} more, in ${esc(home(b.log))}</li>` : ''}</ul>` : ''}
      <div class="s-go left">${confirmCancel ? `<span class="s-confirm"><span>Stop building? What is built stays in its folder.</span><button class="btn danger" data-act="cancel-yes">Stop</button><button class="btn plain" data-act="cancel-no">Keep going</button></span>`
        : `${b.state === 'running' ? `<button class="btn plain" data-act="pause">${ico('pause')}Pause</button>` : ''}
           ${b.state === 'paused' || b.state === 'failed' ? `<button class="btn" data-act="resume"${busy ? ' disabled' : ''}>${ico('play')}Carry on</button>` : ''}
           ${usable && !isOpen ? `<button class="btn${b.state === 'done' ? ' big' : ' plain'}" data-act="open"${busy ? ' disabled' : ''}>${busy === 'open' ? 'Opening…' : 'Open this library'}</button>` : ''}
           ${b.state === 'done' && (isOpen || kept) ? `<a class="btn big" href="/">Back to the library</a>` : ''}
           ${b.state === 'running' || b.state === 'paused' || b.state === 'failed' ? `<button class="btn plain" data-act="cancel">${ico('x')}Cancel</button>` : ''}
           ${stopped || b.state === 'done' ? `<button class="btn plain" data-act="new">Build another</button>` : ''}`}</div>
      <p class="s-note" id="wOpenNote" hidden></p>`;
    const again = had && main.querySelector(`[data-act="${had}"]`); if (again) again.focus();
    if (b.state === 'running' || b.state === 'stopping') pollT = setTimeout(poll, 2000);
  }
  async function poll() {
    try { const b = await call('status'); if (b && b.state) { const was = B && B.state; B = b; if (was !== b.state) say({ paused: 'The build is paused.', done: 'The library is ready.', failed: 'The build stopped.' }[b.state] || '') } } catch { }
    if (view === 'status') { statusView(); frame() }
  }

  /* ------------------------------------------------------------ what the person does */
  async function act(name, el) {
    if (name === 'same' && T && T.built) {
      const c = T.built.choice;
      pending = c; portable = !!c.portable; ai = c.ai && c.ai.mode === 'computer' ? c.ai : { mode: 'none' };
      applyChoice(c); render(); replan();
    }
    else if (name === 'menu-again') loadMenu();
    else if (name === 'look') { repoText = ($('#wRepo') || {}).value || ''; if (repoText.trim()) look(repoText.trim(), true) }
    else if (name === 'pick-source') {
      let t; try { t = IN_WINDOW ? await ask('/api/setup/', 'pick', {}) : { path: (prompt('The whole path of the library’s folder:') || '').trim() } } catch { t = null }
      if (t && t.path) { source = { kind: 'folder', path: t.path }; loadMenu() }
    }
    else if (name === 'docs-all' || name === 'docs-none' || name === 'docs-essential') {
      docs = new Set(name === 'docs-none' ? [] : M.documents.filter(d => name === 'docs-all' || d.essential).map(d => d.id)); render(); replan();
    }
    else if (name === 'start') {
      busy = 'start'; startErr = ''; render();
      try { B = await call('start', { source, choice: choice(), target: T.path, effort, rate: rate || null }); view = 'status' } catch (e) { startErr = e.message }
      busy = ''; render(true);
    }
    else if (name === 'pause') { try { B = await call('stop', { how: 'pause' }) } catch { } render() }
    else if (name === 'resume') { busy = 'resume'; render(); try { B = await call('resume', {}) } catch (e) { B = Object.assign({}, B, { state: 'failed', error: e.message }) } busy = ''; render() }
    else if (name === 'cancel') { confirmCancel = true; render() }
    else if (name === 'cancel-no') { confirmCancel = false; render() }
    else if (name === 'cancel-yes') { confirmCancel = false; try { B = await call('stop', { how: 'cancel' }) } catch { } if (!B || !B.state) { B = null; view = 'steps' } render() }
    else if (name === 'new') { try { await call('stop', { how: 'cancel' }) } catch { } B = null; view = 'steps'; step = 0; render(true) }
    else if (name === 'open') {
      busy = 'open'; render();
      const note = t => { busy = ''; render(); const p = $('#wOpenNote'); if (p) { p.hidden = false; p.textContent = t } };
      try {
        await call('reopen', {});
        const u = await ask('/api/setup/', 'use', { path: B.job.target });
        if (u && u.error) return note(u.error);
        const f = await ask('/api/setup/', 'finish', {});
        if (f && f.restart) return note('Close Lastbook and open it again: it will open on the new library.');
        say('Opening the library…');      // the app starts again on the new library; a build that is running carries on
      } catch (e) { note(e.message) }
    }
  }
  main.addEventListener('click', e => {
    const t = e.target.closest('[data-act],[data-map],[data-unmap],[data-open],[data-all]');
    if (!t || t.disabled) return;
    if (t.dataset.act) return act(t.dataset.act, t);
    if (t.dataset.map) {
      const id = t.dataset.map, level = t.dataset.level;
      if (maps.get(id) === level) maps.delete(id); else maps.set(id, level);
      keepScroll(mapsView); replan();
    } else if (t.dataset.unmap) { maps.delete(t.dataset.unmap); keepScroll(mapsView); replan() }
    else if (t.dataset.open) { const id = t.dataset.open; openRegions.has(id) ? openRegions.delete(id) : openRegions.add(id); keepScroll(mapsView) }
    else if (t.dataset.all) {
      for (const k of (R.kids.get(t.dataset.all) || [])) if (!k.composite && k.mb && !maps.has(k.id) && !builtAs(k.id)) maps.set(k.id, 'overview');
      keepScroll(mapsView); replan();
    }
  });
  function keepScroll(draw) { const y = scrollY; draw(); footer(); scrollTo(0, y) }
  main.addEventListener('toggle', e => {      // a continent opened or closed: remembered, so a redraw leaves it as it was
    const d = e.target; if (!d.dataset || !d.dataset.cont) return;
    d.open ? openRegions.add('c:' + d.dataset.cont) : openRegions.delete('c:' + d.dataset.cont);
  }, true);
  main.addEventListener('change', e => {
    const t = e.target;
    if (t.name === 'where') return setWhere(t.value);
    if (t.id === 'wPortable') { portable = t.checked; if (ai.mode === 'computer' && portable) ai.copy = true; return replan() }
    if (t.name === 'ai') {
      repoErr = '';
      if (t.value === 'none') ai = { mode: 'none' };
      else if (t.value.startsWith('here:')) ai = { mode: 'computer', id: t.value.slice(5), copy: portable };
      else if (t.value.startsWith('get:')) {
        const repo = t.value.slice(4);
        ai = { mode: 'download', repo, version: null, draft: false };
        if (seen[repo]) { const f = seen[repo], p = (AI.presets || []).find(p => p.repo === repo), v = (p && f.versions.find(v => (v.quant || '') === p.quant)) || versionOf(f, f.recommended) || f.versions[0]; ai.version = v && v.name; ai.draft = !!(f.draft && (!p || p.draft)) }
        else look(repo, false);
      }
      keepScroll(aiView); return replan();
    }
    if (t.name === 'version') { const f = seen[custom]; ai = { mode: 'download', repo: custom, version: t.value, draft: !!f.draft }; keepScroll(aiView); return replan() }
    if (t.id === 'wCopy') { ai.copy = t.checked; return replan() }
    if (t.name === 'source') { source = JSON.parse(t.value); return loadMenu() }
    if (t.dataset.doc) { t.checked ? docs.add(t.dataset.doc) : docs.delete(t.dataset.doc); keepScroll(libraryView); return replan() }
    if (t.dataset.zim) { t.checked ? zims.add(t.dataset.zim) : zims.delete(t.dataset.zim); keepScroll(libraryView); return replan() }
    if (t.dataset.vmode) {
      if (t.dataset.vmode === 'default') vdefault = t.value; else if (t.value) vmodes[t.dataset.vmode] = t.value; else delete vmodes[t.dataset.vmode];
      keepScroll(libraryView); return replan();
    }
    if (t.name === 'encoder') { encoder = t.value; return replan() }
    if (t.id === 'wTerrain') { terrain = t.checked; keepScroll(mapsView); return replan() }
    if (t.name === 'effort') effort = t.value;
    if (t.id === 'wRate') rate = t.value;
  });
  main.addEventListener('input', e => {
    if (e.target.id === 'wRepo') repoText = e.target.value;
    if (e.target.id === 'wFind') {      // only the list is redrawn: the field keeps the caret
      find = e.target.value;
      const y = scrollY; mapsView(); const f = $('#wFind'); f.focus(); f.setSelectionRange(find.length, find.length); scrollTo(0, y);
    }
  });
  main.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.id === 'wRepo') { e.preventDefault(); act('look') } });
  foot.addEventListener('click', e => { const b = e.target.closest('[data-go]'); if (b && !b.disabled) go(step + Number(b.dataset.go)) });
  nav.addEventListener('click', e => { const b = e.target.closest('[data-step]'); if (b && !b.disabled) go(Number(b.dataset.step)) });
  addEventListener('keydown', e => { if (e.key === 'Escape' && confirmCancel) { confirmCancel = false; render() } });
  addEventListener('focus', () => { if (view === 'status') poll() });

  /* ------------------------------------------------------------ start */
  async function load() {
    try { S = await call('state') }
    catch (e) { main.innerHTML = `<div class="s-err" role="alert">${ico('warn')}<div><b>The builder could not start.</b><p>${esc(e.message)}</p><p><button class="btn" id="again">Try again</button></p></div></div>`; $('#again').onclick = load; return }
    const back = $('#wBack');
    back.href = S.current ? '/' : '/ui/setup.html';
    $('#wBackText').textContent = S.current ? 'Back to the library' : 'Back to setup';
    B = S.build;
    if (B && B.state && B.state !== 'cancelled' && !(B.state === 'done' && /maps/.test(location.hash))) view = 'status';
    else if (/maps/.test(location.hash) && S.current) {      // from the map: add a region to the library that is open
      await setWhere('open');
      step = reached = 3; loadRegions(); loadAI();
    }
    render(true);
  }
  load();
})();
