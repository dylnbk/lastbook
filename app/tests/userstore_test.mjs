// Runs web/userstore.js in fresh contexts against a made-up engine and browser, and prints what happened in each as JSON.
// Usage: node userstore_test.mjs <userstore.js>
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(process.argv[2], 'utf8');
const wait = ms => new Promise(r => setTimeout(r, ms));

// engine: what GET /api/user-store answers (null: the request fails). statuses: what each POST answers, in turn (then 200).
// browser: what this browser's own storage holds before the page opens.
async function page({ engine, statuses = [], browser = {} }) {
  const posts = [], loaded = [], classes = {};
  const ls = { ...browser };
  for (const [name, fn] of Object.entries({
    getItem(k) { return Object.prototype.hasOwnProperty.call(ls, k) ? ls[k] : null },
    setItem(k, v) { ls[k] = String(v) },
    removeItem(k) { delete ls[k] },
  })) Object.defineProperty(ls, name, { value: fn, enumerable: false });
  const fetch = async (url, opts = {}) => {
    if (url !== '/api/user-store') throw new Error('unexpected ' + url);
    if (opts.method === 'POST') {
      const body = JSON.parse(opts.body), status = statuses.length ? statuses.shift() : 200;
      posts.push({ status, bytes: Buffer.byteLength(opts.body), ops: body.ops.map(o => [o.op, o.ns, o.id, o.value === undefined ? null : o.value.length]) });
      if (status === 0) throw new Error('the drive is gone');
      return { ok: status >= 200 && status < 300, status };
    }
    if (engine === null) throw new Error('no engine');
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(engine)) };
  };
  const ctx = vm.createContext({
    fetch, localStorage: ls, TextEncoder, setTimeout, clearTimeout, console,
    addEventListener() { },
    document: {
      currentScript: { dataset: { then: '/ui/first.js?v=1 /ui/second.js?v=2' } },
      createElement: () => ({}),
      body: { append(s) { loaded.push([s.src, s.async]) } },
      documentElement: { classList: { toggle(name, on) { classes[name] = !!on } } },
      addEventListener() { }, visibilityState: 'visible',
    },
  });
  ctx.window = ctx;
  const before = loaded.length;
  vm.runInContext(source, ctx, { filename: 'userstore.js' });
  const S = ctx.LBStorage;
  const early = loaded.length - before;     // scripts loaded before the store was in memory: must be none
  await S.ready;
  await wait(0);
  return { S, posts, loaded, classes, ls, early };
}
const kept = ls => Object.keys(ls).sort();
const chat = (id, created, extra = {}) => ({ id, title: id, created, updated: created, messages: [], ...extra });
const out = {};

{   // An ordinary library: the engine has some, this browser has some from before the library kept its own.
  const p = await page({
    engine: { available: true, portable: false, data: { kv: { 'sg.saved': '[1]', 'sg.side-off': '1' }, chats: { a: JSON.stringify(chat('a', 1)), b: JSON.stringify(chat('b', 5)) } } },
    browser: { 'sg.vprog': '{"v":1}', 'sg.saved': '["the browser\'s older copy"]', 'sg.topicsCache': '"a copy of the guides"', 'other.app': 'not ours' },
  });
  await p.S.flush();
  out.library = {
    where: p.S.where(), at: p.S.words().at, early: p.early, loaded: p.loaded, sideOff: p.classes['side-off'],
    saved: p.S.getItem('sg.saved'), vprog: p.S.getItem('sg.vprog'), cache: p.S.getItem('sg.topicsCache'), other: p.S.getItem('other.app'), missing: p.S.getItem('sg.none'),
    chatOrder: JSON.parse(p.S.getItem('sg.chats')).map(c => c.id),
    sent: p.posts.flatMap(x => x.ops.map(o => o.slice(0, 3).join(' '))).sort(),
  };
  p.S.setItem('sg.side-off', '0');
  out.library.hint = p.ls['sg.side-off'];     // the one thing mirrored to the browser: read before the first paint
}

{   // A library that travels: nothing is taken from, or left in, the browser of the computer it is plugged into.
  const p = await page({
    engine: { available: true, portable: true, data: { kv: {}, chats: {} } },
    browser: { 'sg.vprog': '{"from":"the library that was open here before"}', 'sg.chats': '[{"id":"theirs","created":1}]' },
  });
  p.S.setItem('sg.saved', '[2]');
  p.S.setItem('sg.side-off', '1');
  p.S.setItem('sg.chats', JSON.stringify([chat('mine', 9)]));
  await p.S.flush();
  out.portable = {
    where: p.S.where(), at: p.S.words().at, vprog: p.S.getItem('sg.vprog'), chats: p.S.getItem('sg.chats') && JSON.parse(p.S.getItem('sg.chats')).map(c => c.id),
    browserAfter: kept(p.ls), sent: p.posts.flatMap(x => x.ops.map(o => o.slice(0, 3).join(' '))).sort(),
  };
}

for (const [name, engine] of [['guest', { available: false }], ['noengine', null]]) {   // a phone it is shared with; an engine without the store
  const p = await page({ engine, browser: { 'sg.saved': '[7]' } });
  p.S.setItem('sg.vprog', '{}');
  p.S.removeItem('sg.saved');
  await p.S.flush();
  out[name] = { where: p.S.where(), read: p.S.getItem('sg.vprog'), browserAfter: kept(p.ls), posts: p.posts.length, loaded: p.loaded.length };
}

{   // What cannot be saved must never hold up what can.
  const p = await page({ engine: { available: true, portable: true, data: { kv: { 'sg.topicsCache': '"written by an earlier version"' }, chats: {} } }, statuses: [200, 400, 200, 500, 0, 200] });
  await p.S.flush();                                   // 200: the earlier version's copy of the guides is taken off the drive
  const first = p.posts.map(x => x.ops.map(o => o.slice(0, 3).join(' ')));
  p.S.setItem('sg.topicsCache', 'g'.repeat(400000));   // a copy of what the engine can give again: memory only
  p.S.setItem('sg.huge', 'h'.repeat(300000));          // more than one request can hold
  await p.S.flush();
  const none = p.posts.length;
  p.S.setItem('sg.refused', 'r');
  await p.S.flush();                                   // 400: dropped
  p.S.setItem('sg.after', 'a');
  await p.S.flush();                                   // 200: goes alone, nothing was waiting in front of it
  p.S.setItem('sg.later', 'l');
  await p.S.flush();                                   // 500: kept for the next time
  p.S.setItem('sg.more', 'm');
  await p.S.flush();                                   // the request fails: both kept
  await p.S.flush();                                   // 200: both go
  out.stuck = {
    first, none, cacheInMemory: p.S.getItem('sg.topicsCache').length, hugeInMemory: p.S.getItem('sg.huge').length,
    posts: p.posts.slice(none).map(x => [x.status, x.ops.map(o => o[2]).join(',')]),
  };
}

{   // Conversations: one at a time, only what changed, and a very long one loses what it can do without.
  const p = await page({ engine: { available: true, portable: true, data: { kv: {}, chats: {} } } });
  const a = chat('a', 1), b = chat('b', 2);
  p.S.setItem('sg.chats', JSON.stringify([b, a]));
  await p.S.flush();
  p.S.setItem('sg.chats', JSON.stringify([b, a]));     // nothing changed
  await p.S.flush();
  a.messages.push({ role: 'user', content: 'héllo ✓' });
  p.S.setItem('sg.chats', JSON.stringify([a]));        // one changed, one deleted
  await p.S.flush();
  const pad = 'p'.repeat(60000);
  const long = chat('long', 3, { messages: [1, 2, 3, 4].map(n => ({ role: 'assistant', content: 'answer ' + n, cited: [{ key: 'S1' }], consulted: [{ excerpt: pad }], steps: [{ x: pad }], sources: [{ excerpt: pad }] })) });
  p.S.setItem('sg.chats', JSON.stringify([long, a]));
  await p.S.flush();
  const longPost = p.posts[p.posts.length - 1];
  p.S.removeItem('sg.chats');
  await p.S.flush();
  out.chats = {
    posts: p.posts.map(x => x.ops.map(o => o.slice(0, 3).join(' '))),
    longWhole: JSON.stringify(long).length,                           // as the page holds it
    longSaved: longPost.ops.find(o => o[2] === 'long')[3],            // as it was sent to be saved
    longRequest: longPost.bytes,
  };
}

{   // Many saves at once go in as few requests as hold them, each under the limit.
  const p = await page({ engine: { available: true, portable: true, data: { kv: {}, chats: {} } } });
  for (let i = 0; i < 7; i++) p.S.setItem('sg.k' + i, String(i).repeat(90000));
  await p.S.flush();
  out.batches = { sizes: p.posts.map(x => x.ops.length), biggest: Math.max(...p.posts.map(x => x.bytes)), total: p.posts.reduce((n, x) => n + x.ops.length, 0) };
}

process.stdout.write(JSON.stringify(out));
