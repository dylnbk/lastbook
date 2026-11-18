'use strict';
/* The model picker: which AI model answers in Chat. Shown only on the computer the library is open on.
   It lists the models that travel with the library, those other programs on this computer already hold, and offers to
   bring one from Hugging Face (three by name, or any repository the person pastes). /api/ai/* in server.py. */
const Models = (() => {
  let box = null, view = null, found = null, timer = null, onChange = null, lastFocus = null;
  const post = (path, body) => fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }).then(async r => ({ ok: r.ok, data: await r.json().catch(() => ({})) }));
  // Memory as the computer's own label gives it (a "16 GB" Mac); file sizes as a download shows them.
  const mem = n => { const g = n / 2 ** 30; return (g >= 10 || Math.abs(g - Math.round(g)) < 0.05 ? g.toFixed(0) : g.toFixed(1)) + ' GB' };
  const size = n => n >= 1e9 ? (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + ' GB' : Math.max(1, Math.round(n / 1e6)) + ' MB';

  async function info(look) {
    try { view = await fetch('/api/ai/models' + (look ? '?look=1' : '')).then(r => r.json()) } catch { view = { available: false } }
    return view;
  }
  // Is there a picker here at all (the desktop app or a browser on this computer; not a phone the library is shared with)?
  async function available() { return (view || await info()).available }

  const here = () => typeof LBStorage !== 'undefined' && LBStorage.where() === 'drive' ? 'onto the drive' : 'into the library';
  function fitTag(f) {
    if (!f) return '';
    if (f.fits === false) return `<span class="mp-tag no">Too large for this computer</span>`;
    if (f.fits == null) return `<span class="mp-tag">Not judged</span>`;
    return `<span class="mp-tag ${f.tight ? 'tight' : 'ok'}">${f.tight ? 'Fits, with little to spare' : 'Fits this computer'}</span>`;
  }
  function row(id, title, sub, opts = {}) {
    const on = view.selected === id, now = view.active === id;
    return `<label class="mp-row${opts.off ? ' off' : ''}"><input type="radio" name="mp" value="${esc(id)}"${on ? ' checked' : ''}${opts.off ? ' disabled' : ''}>
      <span class="mp-what"><b>${esc(title)}${now ? ' <i class="mp-now">answering now</i>' : ''}</b><small>${sub}</small></span>${opts.extra || ''}</label>`;
  }
  function modelRow(m) {
    const bits = [size(m.bytes), m.vision ? 'reads page images' : 'reads text only', esc(m.fit.why || '')];
    const where = m.where === 'drive' ? '' : ` Used where ${esc(m.origin)} keeps it; it answers on this computer only.`;
    const extra = m.where === 'drive' ? `<button class="mp-x" data-remove="${esc(m.id)}" aria-label="Remove ${esc(m.name)} from this library" title="Remove from this library">Remove</button>`
      : `<button class="mp-x keep" data-carry="${esc(m.id)}" aria-label="Copy ${esc(m.name)} ${here()}" title="So the library answers on other computers too">Copy ${here()}</button>`;
    return row(m.id, m.name, `${fitTag(m.fit)} ${bits.join(' · ')}${where}`, { off: m.fit.fits === false || !view.runtime, extra });
  }

  function render() {
    if (!box || !view) return;
    const drive = view.models.filter(m => m.where === 'drive'), here = view.models.filter(m => m.where !== 'drive');
    const c = view.computer || {}, run = view.running || {};
    const stock = view.stock && /ready|stopped|loading/.test(view.stock.state), server = view.server && view.server.online;
    const words = typeof LBStorage !== 'undefined' ? LBStorage.where() : 'library';
    const travels = words === 'drive' ? 'On this drive: they travel with it' : 'In this library';
    box.querySelector('.mp-body').innerHTML = `
      <p class="mp-lead">Answers are written by a model that runs on this computer. Nothing you ask leaves it.
        ${c.ram_bytes ? `This computer has ${mem(c.ram_bytes)} of memory${c.chip ? ` (${esc(c.chip)})` : ''}.` : ''}</p>
      ${view.runtime ? '' : `<p class="mp-warn"><b>No program to run models is here yet.</b> Lastbook uses llama.cpp’s <code>llama-server</code>. Installing AnythingLLM or llama.cpp on this computer provides one.</p>`}
      <fieldset class="mp-set"><legend>Which model answers</legend>
        ${row('auto', 'Choose for me', 'The largest model in this library that fits this computer.')}
        ${drive.length ? `<div class="mp-h">${travels}</div>${drive.map(modelRow).join('')}` : ''}
        ${here.length ? `<div class="mp-h">Already on this computer</div>${here.map(modelRow).join('')}` : ''}
        ${stock ? row('stock', view.stock.name, 'The model Lastbook was made with. Reads text only.') : ''}
        ${server ? row('server', 'A model server running on this computer', esc(view.server.model)) : ''}
        ${row('none', 'No AI', 'Chat shows the matching pages and writes nothing. Search, guides, videos and the map are the same.')}
      </fieldset>
      ${run.state === 'ready' || run.state === 'loading' ? `<p class="mp-run"><span class="dotok"></span>${run.state === 'ready' ? `Loaded and holding memory${run.load_s ? ` (it took ${run.load_s} s to start)` : ''}. It lets go by itself after 15 minutes unused.` : 'Starting…'} <button class="btn plain small" id="mpStop">Let it go now</button></p>` : ''}
      <fieldset class="mp-set"><legend>Add a model</legend>
        <p class="small muted" style="margin:0 0 8px">From Hugging Face, into this library’s <code>models</code> folder. This is the one thing here that uses the internet, and only when you press a button.</p>
        <div class="mp-presets">${(view.presets || []).map(p => `<button class="mp-preset" data-repo="${esc(p.repo)}:${esc(p.quant)}"><b>${esc(p.title)}</b><small>${esc(p.about)}</small></button>`).join('')}</div>
        <form class="mp-form" id="mpForm"><label class="sr" for="mpRepo">A Hugging Face repository, as owner/name</label>
          <input id="mpRepo" placeholder="Or paste a Hugging Face repository: owner/name" autocomplete="off" spellcheck="false" autocapitalize="off">
          <button class="btn plain">Look</button></form>
        <div id="mpFound" aria-live="polite"></div>
      </fieldset>
      <p class="small muted">${view.runtime ? `Models are run by llama.cpp’s <code>llama-server</code>, from ${esc(view.runtime.origin)}.${/library|Lastbook/.test(view.runtime.origin) ? '' : ` <button class="linkish" id="mpCarry">Put a copy ${here()}</button>, for other computers of this kind.`} ` : ''}<button class="linkish" id="mpLook">Look again for models</button></p>`;
    box.querySelectorAll('input[name=mp]').forEach(i => i.onchange = () => choose(i.value));
    box.querySelectorAll('[data-remove]').forEach(b => b.onclick = e => { e.preventDefault(); remove(b.dataset.remove) });
    box.querySelectorAll('[data-carry]').forEach(b => b.onclick = async e => {
      e.preventDefault();
      const r = await post('/api/ai/carry', { what: 'model', id: b.dataset.carry });
      if (!r.ok) { toast(r.data.error || 'It could not be copied'); return }
      view.bringing = r.data; renderFound(); poll(); box.querySelector('#mpFound').scrollIntoView({ block: 'nearest' });
    });
    const carry = box.querySelector('#mpCarry'); if (carry) carry.onclick = async () => {
      carry.disabled = true; const r = await post('/api/ai/carry', { what: 'runtime' });
      if (!r.ok) { toast(r.data.error || 'It could not be copied'); carry.disabled = false; return }
      view = r.data; render(); toast('Copied');
    };
    box.querySelectorAll('.mp-preset').forEach(b => b.onclick = () => look(b.dataset.repo));
    box.querySelector('#mpForm').onsubmit = e => { e.preventDefault(); const v = box.querySelector('#mpRepo').value.trim(); if (v) look(v) };
    box.querySelector('#mpLook').onclick = async () => { await info(true); render(); toast('Looked again') };
    const stop = box.querySelector('#mpStop'); if (stop) stop.onclick = async () => { const r = await post('/api/ai/stop'); if (r.ok) { view = r.data; render(); changed() } };
    renderFound();
  }
  function changed() { if (onChange) onChange() }
  async function choose(id) {
    const r = await post('/api/ai/select', { id });
    if (!r.ok) { toast(r.data.error || 'That could not be chosen'); await info(true) } else view = r.data;
    render(); changed();
  }
  async function remove(id) {
    const m = view.models.find(x => x.id === id);
    if (!m || !confirm(`Remove ${m.name} from this library? Its files (${size(m.bytes)}) are deleted.`)) return;
    const r = await post('/api/ai/remove', { id });
    if (!r.ok) toast(r.data.error || 'It could not be removed'); else view = r.data;
    render(); changed();
  }

  // ------------------------------------------------------------ bringing a model ----
  async function look(repo) {
    const el = box.querySelector('#mpFound'); found = null;
    el.innerHTML = '<p class="muted small">Asking Hugging Face what is in it…</p>';
    const r = await post('/api/ai/inspect', { repo });
    if (!box) return;
    if (!r.ok) { el.innerHTML = `<p class="mp-warn">${esc(r.data.error || 'Hugging Face could not be reached.')}</p>`; return }
    found = r.data; found.pick = found.recommended; renderFound();
  }
  function renderFound() {
    const el = box && box.querySelector('#mpFound'); if (!el) return;
    const job = view.bringing || {};
    if (job.state === 'downloading' || job.state === 'copying') {
      const pct = job.total_bytes ? Math.floor(100 * (job.done_bytes || 0) / job.total_bytes) : 0;
      el.innerHTML = `<div class="mp-job"><b>${job.state === 'copying' ? 'Copying' : 'Bringing'} ${esc(job.repo || '')}</b><small>${esc(job.file || '')} · ${size(job.done_bytes || 0)} of ${size(job.total_bytes || 0)}</small>
        <span class="wk-bar"><span style="width:${pct}%"></span></span><button class="btn plain small" id="mpJobStop">Stop</button></div>`;
      el.querySelector('#mpJobStop').onclick = async () => { await post('/api/ai/import/stop'); poll() };
      return;
    }
    const note = job.state === 'failed' || job.state === 'stopped' ? `<p class="mp-warn">${esc(job.message || 'It did not finish.')}</p>` : job.state === 'done' ? '<p class="mp-ok">The model is here and chosen. Ask a question to start it.</p>' : '';
    if (!found) { el.innerHTML = note; return }
    const f = found, none = !f.versions.some(v => v.fit.fits !== false);
    const free = f.free_bytes != null ? `${size(f.free_bytes)} free on the library’s drive.` : '';
    el.innerHTML = `${note}<div class="mp-found"><b>${esc(f.name)}</b>
      <small>${esc(f.repo)}${f.license ? ' · licence: ' + esc(f.license) : ''} · ${f.vision ? 'reads page images' : 'reads text only'}${f.judged ? '' : ' · its size could not be judged from its header'}</small>
      ${none ? '<p class="mp-warn">No version of this model fits this computer’s memory. A smaller model will.</p>' : ''}
      <div class="mp-versions">${f.versions.map(v => `<label class="mp-row${v.fit.fits === false ? ' off' : ''}"><input type="radio" name="mpv" value="${esc(v.name)}"${v.name === f.pick ? ' checked' : ''}${v.fit.fits === false ? ' disabled' : ''}>
        <span class="mp-what"><b>${esc(v.quant || v.name)}${v.name === f.recommended ? ' <i class="mp-now">suggested</i>' : ''}</b>
        <small>${fitTag(v.fit)} ${size(v.total_bytes)} to bring${v.coarse ? ' · very compressed: answers are noticeably worse' : ''}${v.fit.fits === false ? '' : ' · ' + esc(v.fit.why)}</small></span></label>`).join('')}</div>
      ${f.draft ? `<label class="mp-opt"><input type="checkbox" id="mpDraft"> Also bring its speed-up file (${size(f.draft.bytes)}; used where this computer’s llama.cpp supports it)</label>` : ''}
      <div class="row"><button class="btn" id="mpBring"${f.pick ? '' : ' disabled'}>Bring it into this library</button><span class="small muted">${free}</span></div></div>`;
    el.querySelectorAll('input[name=mpv]').forEach(i => i.onchange = () => { f.pick = i.value; el.querySelector('#mpBring').disabled = false });
    el.querySelector('#mpBring').onclick = async () => {
      const r = await post('/api/ai/import', { repo: f.repo, version: f.pick, draft: !!(el.querySelector('#mpDraft') || {}).checked });
      if (!r.ok) { toast(r.data.error || 'It could not be started'); return }
      view.bringing = r.data; found = null; renderFound(); poll();
    };
  }
  function poll() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!box) return;
      let job; try { job = await fetch('/api/ai/import').then(r => r.json()) } catch { return poll() }
      const going = s => s === 'downloading' || s === 'copying';
      const was = (view.bringing || {}).state; view.bringing = job;
      if (going(job.state)) { renderFound(); return poll() }
      if (going(was)) { await info(true); render(); changed() } else renderFound();
    }, 1000);
  }

  // ------------------------------------------------------------------ the panel ----
  async function open(changedFn) {
    onChange = changedFn || null; lastFocus = document.activeElement;
    await info(true);
    if (!view.available) { toast('The AI model is chosen on the computer the library is open on'); return }
    close();
    box = document.createElement('div'); box.className = 'mp-back';
    box.innerHTML = `<div class="mp" role="dialog" aria-modal="true" aria-labelledby="mpTitle" tabindex="-1">
      <div class="mp-head"><h2 id="mpTitle">AI model</h2><button class="iconbtn" id="mpClose" aria-label="Close">×</button></div><div class="mp-body"></div></div>`;
    document.body.appendChild(box);
    box.addEventListener('click', e => { if (e.target === box) close() });
    box.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); close() } });
    box.querySelector('#mpClose').onclick = close;
    render(); box.querySelector('.mp').focus();
    if (/downloading|copying/.test((view.bringing || {}).state || '')) poll();
  }
  function close() { clearTimeout(timer); if (box) { box.remove(); box = null; found = null; if (lastFocus && lastFocus.focus) lastFocus.focus() } }
  return { open, close, available, info };
})();
