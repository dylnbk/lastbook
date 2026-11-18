'use strict';
/* iPad-only wording. The shared desktop UI (web/) is unchanged; this runs in the app's top frame only and swaps a few
   exact desktop sentences for their iPad versions. Only whole, known sentences are replaced, so library text is never
   touched. The model card points to On My iPad › Lastbook instead of a desktop folder or Downloads. */
(() => {
  if (window.top !== window || window.origin === 'null') return;
  /* One app for iPhone and iPad: each sentence names the one it is on. (An iPad's web view says it is a Mac; only an iPhone says iPhone.) */
  const DEV = /iPhone|iPod/.test(navigator.userAgent) ? 'iPhone' : 'iPad';
  const SWAP = new Map([
    ['Chats stay on this device. Nothing is sent anywhere except this computer’s own assistant.',
     'Chats stay on this ' + DEV + '. Nothing is sent anywhere.'],
    ['Answers come from the library on this computer and can be wrong. Check the linked pages; get trained help for anything serious.',
     'Answers come from the library on this ' + DEV + ' and can be wrong. Check the linked pages; get trained help for anything serious.'],
    ['Checking this computer’s memory…', 'Checking this ' + DEV + '’s memory…'],
    ['checking this computer’s memory…', 'checking this ' + DEV + '’s memory…'],
    ['Answers are built only from the manuals, guides and video transcripts on this computer — with the exact page or video moment for each point.',
     'Answers are built only from the manuals, guides and video transcripts on this ' + DEV + ' — with the exact page or video moment for each point.'],
    ['Public manuals, health guides, courses, videos and encyclopedias kept on this computer, so it works when the internet and phones don’t.',
     'Public manuals, health guides, courses, videos and encyclopedias kept on this ' + DEV + ', so it works when the internet and phones don’t.'],
    ['No local AI added', 'No local AI on this ' + DEV],
    ['not added. Add it in Library and local AI (about 17 GB).',
     'not added. Put the local AI file (about 17 GB) in Files › On My ' + DEV + ' › Lastbook, then Look again.'],
  ]);
  const MODEL = 'gemma-4-26B-A4B-it-UD-Q4_K_M.gguf';
  const CARD = `<p><b>Add the local AI to get written answers.</b> It is one file of about 17 GB, and runs on this ${DEV} only.</p>
    <p class="small">Put <code>${MODEL}</code> in <b>Files › On My ${DEV} › Lastbook</b> (copy it from a Mac with Finder, or from a drive), then tap Look again.</p>
    <div class="row"><button class="btn" data-ai="rescan">Look again</button></div>`;

  function fixText(root) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const t = n.nodeValue.trim();
      if (SWAP.has(t)) n.nodeValue = n.nodeValue.replace(t, SWAP.get(t));
    }
  }
  function fixCard() {
    const box = document.getElementById('aiSetup');
    if (!box || box.hidden || box.dataset.ipad === '1' || !box.textContent.includes('Add the local AI to get written answers')) return;
    box.dataset.ipad = '1';
    box.innerHTML = CARD;
    box.querySelector('[data-ai]').onclick = async (e) => {   // the same call the desktop card's Look again makes
      e.target.disabled = true;
      try { await fetch('/api/assistant/local', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'rescan' }) }) } catch { }
      setTimeout(() => location.reload(), 300);
    };
  }
  /* Local AI status line (chat.js statusLine/localLine are desktop code, unchanged). On desktop, checking and measuring get the
     red "not ready" dot; on iPad the model prepares by itself when Lastbook opens, so preparing must look like progress:
     a spinner and "Preparing local AI…" / "Loading local AI… N%". Ready is green; no file or turned off is a neutral grey;
     a file that can't work here (wrong file, too big) is amber; a real failure is red. The state comes from the same
     /api/assistant/status answer chat.js reads (its `local`, see ChatAPI), seen through fetch. */
  let local = null;
  const realFetch = window.fetch;
  window.fetch = async function (input, init) {
    const r = await realFetch.call(this, input, init);
    try {
      const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
      if (r.ok && (path === '/api/assistant/status' || path === '/api/assistant/local')) {
        r.clone().json().then(j => { local = path === '/api/assistant/status' ? j.local || null : j; paintStatus(); fixWorking() }).catch(() => { });
      }
    } catch { }
    return r;
  };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  const STILL = ' · search and reading still work';
  function statusLook(l) {
    const pct = l.pct != null ? ` ${l.pct}%` : '';
    switch (l.state) {
      case 'checking': return ['spin', `Preparing local AI…${pct}`];
      case 'measuring': return ['spin', 'Preparing local AI…'];
      case 'loading': return ['spin', `Loading local AI…${pct}`];
      case 'ready': return ['ready', 'Local AI ready'];
      case 'stopped': return ['ready', 'Local AI ready · it loads when you ask'];
      case 'absent': return ['none', 'No local AI on this ' + DEV + STILL];
      case 'off': return ['none', 'Local AI is off' + STILL];
      case 'wrong': return ['incompatible', (l.message || 'The local AI file on this ' + DEV + ' is not the right one.') + STILL];
      case 'too_big': return ['incompatible', (l.message || 'The local AI does not fit this ' + DEV + '’s memory.') + STILL];
      case 'error': return ['failed', (l.message || 'The local AI could not start.') + STILL];
      default: return null;
    }
  }
  function paintStatus() {
    const el = document.getElementById('chatStatus');
    if (!el || !local) return;
    const look = statusLook(local);
    if (!look) return;
    const key = look.join('|');
    if (el.dataset.lbai === key && el.querySelector('.lbai')) return;
    el.dataset.lbai = key;
    el.innerHTML = `<span class="lbai lbai-${look[0]}" aria-hidden="true"></span><span>${esc(look[1])}</span>`;
  }
  // The working line of an answer waiting for the model: chat.js says "Starting the local AI… N%".
  function fixWorking() {
    const st = local && local.state;
    for (const b of document.querySelectorAll('.working b')) {
      const m = /^Starting the local AI…( \d+%)?$/.exec(b.textContent);
      if (!m) continue;
      b.textContent = st === 'checking' ? `Preparing local AI…${m[1] || ''}` : st === 'measuring' ? 'Preparing local AI…' : `Loading local AI…${m[1] || ''}`;
    }
  }
  const css = document.createElement('style');
  css.textContent = `.lbai{flex:none;display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:2px;vertical-align:middle}
    .lbai-ready{background:#2e9d61}.lbai-none{background:#9aa0a6}.lbai-failed{background:#c0392b}
    .lbai-incompatible{background:transparent;border:2px solid #c98a00;width:7px;height:7px}
    .lbai-spin{width:10px;height:10px;border:2px solid currentColor;border-right-color:transparent;opacity:.55;animation:lbai-rot .8s linear infinite}
    @keyframes lbai-rot{to{transform:rotate(360deg)}}
    @media (prefers-reduced-motion:reduce){.lbai-spin{animation:none;border-right-color:currentColor;opacity:.35}}`;
  (document.head || document.documentElement).appendChild(css);

  const run = () => { fixText(document.body); fixCard(); paintStatus(); fixWorking() };
  new MutationObserver(() => { const box = document.getElementById('aiSetup'); if (box && box.dataset.ipad === '1' && !box.textContent.includes(MODEL)) box.dataset.ipad = ''; run() })
    .observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  document.addEventListener('DOMContentLoaded', run);
})();
