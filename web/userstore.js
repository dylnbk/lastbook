'use strict';
/* What the person keeps: saved items, where they were in a video, chats, the page's own settings.

   It is kept in the library's own folder (LastbookData/user), not in this browser's storage, so it goes where the library
   goes, and a library on a USB drive leaves nothing behind on the computer it was opened on. This file asks the engine for
   all of it once, before the rest of the page loads. After that every read is from memory, as a localStorage read was, and
   every write is also sent on. A phone the library is shared with, or an engine without the store, keeps using the browser's
   own storage, as before.

     LBStorage.getItem / setItem / removeItem    the same three calls as localStorage
     LBStorage.where()                           'drive' (a library that travels), 'library' or 'browser'
     LBStorage.words()                           how the page says where things are kept

   The scripts named in this tag's data-then are loaded, in order, once the store is in memory. */
window.LBStorage = (() => {
  const CHATS = 'sg.chats', OWN = /^sg\./, MOVED = 'sg.moved', HINTS = ['sg.side-off'];
  // Copies of what the engine can give again. They stay in memory for this sitting and are never written to the drive:
  // the guides alone are over 230 KB once each item carries its source, more than one request may hold.
  const VOLATILE = ['sg.topicsCache'];
  const LIMIT = 230000;                     // bytes in one request: the app's bridge takes 256 KB, the engine 300,000
  const mem = new Map(), held = new Map();  // held: what the library has now, 'kv\n<name>' or 'chats\n<id>' -> string
  const bytes = s => new TextEncoder().encode(s).length;
  let remote = false, portable = false, queue = [], timer = 0, sending = false;
  const loader = document.currentScript, then = ((loader && loader.dataset.then) || '').split(/\s+/).filter(Boolean);
  const browser = {
    get(k) { try { return localStorage.getItem(k) } catch { return null } },
    set(k, v) { try { localStorage.setItem(k, v) } catch { } },
    del(k) { try { localStorage.removeItem(k) } catch { } },
    keys() { try { return Object.keys(localStorage) } catch { return [] } },
  };
  const tag = (ns, id) => ns + '\n' + id;

  function put(ns, id, value) {
    if (held.get(tag(ns, id)) === value) return;
    const op = { op: 'put', ns, id, value };
    if (bytes(JSON.stringify(op)) > LIMIT) return;   // more than one request can hold: kept in memory only, and nothing waits behind it
    held.set(tag(ns, id), value);
    queue.push(op);
    soon();
  }
  function drop(ns, id) {
    if (!held.has(tag(ns, id))) return;
    held.delete(tag(ns, id));
    queue.push({ op: 'delete', ns, id });
    soon();
  }
  function soon() { clearTimeout(timer); timer = setTimeout(flush, 150) }
  async function flush() {
    clearTimeout(timer);
    if (sending || !queue.length) return;
    sending = true;
    try {
      while (queue.length) {
        const ops = [];
        let size = 20;
        while (queue.length) {
          const n = bytes(JSON.stringify(queue[0])) + 1;
          if (ops.length && size + n > LIMIT) break;
          size += n;
          ops.push(queue.shift());
        }
        let status = 0;
        try { status = (await fetch('/api/user-store', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ops }) })).status } catch { }
        if (status >= 200 && status < 300) continue;
        for (const o of ops) held.delete(tag(o.ns, o.id));   // not in the library after all: the next save of each is sent again
        if (status >= 400 && status < 500) continue;         // refused as it is, and would be again: dropped, so it cannot hold up what follows
        queue = ops.concat(queue);                           // the drive is full or gone: tried again with the next thing saved
        break;
      }
    } finally { sending = false }
  }

  // One conversation, as the string that is saved. A very long one loses what it can do without, oldest answers first:
  // the passages it looked at and did not use, then the steps it took, then the passages found on the way.
  function fit(chat) {
    let s = JSON.stringify(chat);
    if (bytes(s) < LIMIT - 4000) return s;
    const c = JSON.parse(s), msgs = Array.isArray(c.messages) ? c.messages : [];
    for (const field of ['consulted', 'steps', 'sources', 'fallback']) {
      for (const m of msgs) {
        if (m && m[field]) { delete m[field]; s = JSON.stringify(c); if (bytes(s) < LIMIT - 4000) return s }
      }
    }
    return null;   // still too long for one request: it stays in memory for this sitting
  }
  function syncChats(json) {
    let list;
    try { list = JSON.parse(json) } catch { return }
    if (!Array.isArray(list)) return;
    const seen = new Set();
    for (const c of list) {
      if (!c || typeof c.id !== 'string' || !c.id) continue;
      seen.add(c.id);
      const s = fit(c);
      if (s) put('chats', c.id, s);
    }
    for (const k of [...held.keys()]) if (k.startsWith('chats\n') && !seen.has(k.slice(6))) drop('chats', k.slice(6));
  }

  function getItem(k) { return remote ? (mem.has(k) ? mem.get(k) : null) : browser.get(k) }
  function setItem(k, v) {
    v = String(v);
    if (!remote) return browser.set(k, v);
    mem.set(k, v);
    if (VOLATILE.includes(k)) return;
    if (k === CHATS) syncChats(v); else put('kv', k, v);
    if (!portable && HINTS.includes(k)) browser.set(k, v);   // read before the first paint (plat.js), before the store can answer
  }
  function removeItem(k) {
    if (!remote) return browser.del(k);
    mem.delete(k);
    if (k === CHATS) syncChats('[]'); else drop('kv', k);
    if (!portable && HINTS.includes(k)) browser.del(k);
  }

  const ready = (async () => {
    let d = null;
    try { const r = await fetch('/api/user-store'); if (r.ok) d = await r.json() } catch { }
    if (!d || !d.available || !d.data) return;
    remote = true;
    portable = !!d.portable;
    for (const [k, v] of Object.entries(d.data.kv || {})) { mem.set(k, v); held.set(tag('kv', k), v) }
    const chats = [];
    for (const [id, v] of Object.entries(d.data.chats || {})) {
      held.set(tag('chats', id), v);
      try { chats.push(JSON.parse(v)) } catch { }
    }
    chats.sort((a, b) => (b.created || 0) - (a.created || 0));   // newest first, as the page keeps them
    if (chats.length) mem.set(CHATS, JSON.stringify(chats));
    // Once, and never for a library that travels: what this browser kept before the library kept it itself. On a computer
    // a drive is plugged into, the browser's storage belongs to whatever library was open there before, not to this one.
    if (!portable && mem.get(MOVED) !== '1') {
      for (const k of browser.keys()) {
        const v = OWN.test(k) && !mem.has(k) && !VOLATILE.includes(k) ? browser.get(k) : null;
        if (v != null) setItem(k, v);
      }
      setItem(MOVED, '1');
    }
    for (const k of VOLATILE) if (held.has(tag('kv', k))) { mem.delete(k); drop('kv', k) }   // one written by an earlier version
    const c = document.documentElement.classList;
    c.toggle('side-off', mem.get('sg.side-off') === '1');
    if (window.LB_LAYOUT) window.LB_LAYOUT();
  })();
  ready.then(() => {
    for (const src of then) {
      const s = document.createElement('script');
      s.src = src;
      s.async = false;   // in the order given
      document.body.append(s);
    }
  });
  addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush() });

  const where = () => remote ? (portable ? 'drive' : 'library') : 'browser';
  const WORDS = {
    drive: { at: 'on this drive', kept: 'Kept on this drive with the library, so it goes where the drive goes. Nothing is kept on this computer.' },
    library: { at: 'in this library', kept: 'Kept in this library’s own folder on this computer.' },
    browser: { at: 'on this device', kept: 'Kept on this device only.' },
  };
  return { getItem, setItem, removeItem, where, words: () => WORDS[where()], ready, flush };
})();
