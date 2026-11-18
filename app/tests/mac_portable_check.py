#!/usr/bin/env python3
"""A library that travels, in the real Mac shell: what the person keeps goes on the drive and comes back from it, and
nothing of Lastbook's is left on this Mac.

  <venv python> -B app/tests/mac_portable_check.py /Volumes/SOMEDRIVE/some-empty-folder [--keep]

Makes a small marked library in that folder (a copy of the made-up one from fixture.py), then opens it twice in the
product's own shell and bridge, off-screen (mac_offscreen.py): the first sitting saves things, the second finds them.
Between the two it lists every file under this Mac's Library folders that changed. Prints one JSON report and exits 0
when every check holds. Never packaged; runs no model.
"""
import argparse, itertools, json, os, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import fixture  # noqa: E402
from engine import portable  # noqa: E402

HELP = r"""
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (f, ms = 15000) => { const t = performance.now(); while (performance.now() - t < ms) { try { const v = f(); if (v) return v } catch {} await sleep(50) } return null };
"""
# Where a Mac program's own leftovers go. The names a Python run from source gets are listed with the product's.
HOME = Path.home()
WATCHED = [HOME / 'Library' / d for d in ('Application Support/Lastbook', 'WebKit', 'Caches', 'HTTPStorages', 'Cookies', 'Saved Application State', 'Preferences')]
OURS = ('lastbook', 'org.python.python')     # the product's name, and the name a Python run from source goes by
_n = itertools.count(1)


class Sitting:
    """One run of the off-screen app, talked to through files (the protocol of mac_offscreen.py)."""

    def __init__(self, name, data, env):
        self.out = ROOT / 'derived' / 'app-build' / 'mac-run' / name
        shutil.rmtree(self.out, ignore_errors=True)
        self.ctl = self.out / 'ctl'
        self.ctl.mkdir(parents=True)
        self.log = open(self.out / 'stdout.txt', 'ab')
        self.p = subprocess.Popen([sys.executable, '-B', str(HERE / 'mac_offscreen.py'), '--run', name, '--data', str(data)], stdout=self.log,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', **env})

    def cmd(self, kind, timeout=60, **k):
        i = f'{time.time_ns()}-{next(_n)}'
        t = self.ctl / f'{i}.tmp'
        t.write_text(json.dumps(dict(k, id=i, kind=kind)))
        t.replace(self.ctl / f'{i}.cmd')
        o = self.ctl / f'{i}.out'
        end = time.time() + timeout
        while time.time() < end:
            if self.p.poll() is not None:
                return {'error': f'app exited {self.p.returncode}: ' + (self.out / 'stdout.txt').read_text(errors='replace')[-1500:]}
            if o.exists():
                v = json.loads(o.read_text())
                o.unlink()
                return v
            time.sleep(0.02)
        return {'error': 'timeout'}

    def js(self, code, timeout=60):
        v = self.cmd('js', timeout=timeout, code=HELP + code)
        if v.get('error'):
            return {'__error': v['error']}
        r = v.get('result')
        return json.loads(r) if r not in (None, 'None') else None

    def up(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end and self.p.poll() is None:
            if self.js("return document.readyState === 'complete' && typeof route === 'function' && !document.querySelector('#main .loading')", timeout=5) is True:
                return True
            time.sleep(0.3)
        return False

    def quit(self):
        self.cmd('quit', timeout=10)
        try:
            self.p.wait(20)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.log.close()


def changed_since(stamp):
    """Files under this Mac's Library folders that are ours by name (anywhere in their path) and were written after stamp."""
    out = []
    for top in WATCHED:
        for root, _, files in os.walk(top):
            for f in files:
                p = Path(root) / f
                if not any(w in str(p.relative_to(HOME)).lower() for w in OURS):
                    continue
                try:
                    if p.stat().st_mtime >= stamp:
                        out.append(str(p.relative_to(HOME)))
                except OSError:
                    pass
    return sorted(out)


def make_library(folder):
    """The made-up library, copied onto the drive and marked as one that travels. The copy leaves out what a drive without
    links cannot hold (the fixture's hard link and its link out of the library are checks for another test)."""
    src = fixture.ensure()
    lib = folder / 'LastbookData'
    shutil.rmtree(folder, ignore_errors=True)
    lib.mkdir(parents=True)
    skip = {'huge.mkv', 'hl2.txt', 'out.txt', 'big.bin', 'over.bin', 'big.pdf'}
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        if rel.parts[:1] in (('user',), ('tmp',)):
            dirs[:] = []
            continue
        (lib / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            p = Path(root) / f
            if f in skip or p.is_symlink() or f == 'ready':
                continue
            shutil.copyfile(p, lib / rel / f)
    portable.mark(lib)
    return lib


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('folder', help='an empty or new folder on the drive to test on')
    ap.add_argument('--keep', action='store_true', help='leave the test library on the drive afterwards')
    a = ap.parse_args()
    folder = Path(a.folder).resolve()
    os.environ['COPYFILE_DISABLE'] = '1'
    lib = make_library(folder)
    report = {'library': str(lib), 'checks': {}}
    ok = report['checks']
    stamp = time.time() - 1
    env = {'LASTBOOK_PORTABLE': '1'}

    one = Sitting('portable-1', lib, env)
    try:
        report['first_up'] = one.up()
        r = one.js("""
          toggleSave({key: 'd:pdfsmall', title: 'Kept on the drive \u2713', href: '#/read/pdfsmall?p=1', sub: 'Test'});
          setVprog('vida', {t: 73, d: 600, title: 'vida'});
          LBStorage.setItem('sg.chats', JSON.stringify([{id: 'c1', title: 'A chat', created: 5, updated: 5, messages: [{role: 'user', content: 'how do I boil water?'}]}]));
          LBStorage.setItem('sg.side-off', '1');
          localStorage.setItem('probe.left-behind', 'only until this window closes');
          await LBStorage.flush();
          const d = await (await fetch('/api/user-store')).json();
          return {where: LBStorage.where(), at: LBStorage.words().at, portable: d.portable, kv: Object.keys(d.data.kv).sort(), chats: Object.keys(d.data.chats),
                  browser: Object.keys(localStorage).sort(), persistent: await (navigator.storage && navigator.storage.persisted ? navigator.storage.persisted().catch(() => null) : null)};
        """)
        report['first'] = r
    finally:
        one.quit()
    ok['the page says the drive'] = isinstance(r, dict) and r.get('where') == 'drive' and r.get('portable') is True
    ok['saved, progress and the page setting reached the engine'] = isinstance(r, dict) and r.get('kv') == ['sg.saved', 'sg.side-off', 'sg.vprog'] and r.get('chats') == ['c1']

    db = lib / 'user' / 'userdata.sqlite'
    on_drive = sorted(p.name for p in (lib / 'user').iterdir()) if (lib / 'user').is_dir() else []
    rows = sqlite3.connect(db).execute('SELECT ns, id FROM item ORDER BY ns, id').fetchall() if db.is_file() else []
    mode = sqlite3.connect(db).execute('PRAGMA journal_mode').fetchone()[0] if db.is_file() else None
    report['on_drive'] = {'user': on_drive, 'rows': rows, 'journal_mode': mode,
                          'beside': sorted(p.name for p in folder.iterdir()), 'dot_underscore': sorted(str(p.relative_to(folder)) for p in folder.rglob('._*'))}
    rules = folder / '.lastbook' / 'webkit-rules'
    report['on_drive']['rule_list'] = sorted(p.name for p in rules.rglob('*') if p.is_file()) if rules.is_dir() else []
    ok['the network rule list was compiled onto the drive, not into this Mac’s Library'] = bool(report['on_drive']['rule_list'])
    ok['one file on the drive, with a rollback journal'] = on_drive == ['userdata.sqlite'] and mode == 'delete'
    ok['the rows are there'] = rows == [('chats', 'c1'), ('kv', 'sg.saved'), ('kv', 'sg.side-off'), ('kv', 'sg.vprog')]
    ok['no ._ files were made on the drive'] = not report['on_drive']['dot_underscore']

    two = Sitting('portable-2', lib, env)
    try:
        report['second_up'] = two.up()
        r2 = two.js("""
          return {where: LBStorage.where(), saved: store.get('saved', []).map(x => x.title), vprog: (vprog().vida || {}).t, chats: JSON.parse(LBStorage.getItem('sg.chats') || '[]').map(c => c.title),
                  sideOff: document.documentElement.classList.contains('side-off'), browser: Object.keys(localStorage).sort()};
        """)
        report['second'] = r2
    finally:
        two.quit()
    ok['the second sitting finds it all'] = isinstance(r2, dict) and r2.get('saved') == ['Kept on the drive \u2713'] and r2.get('vprog') == 73 and r2.get('chats') == ['A chat'] and r2.get('sideOff') is True
    ok['the web view kept nothing between sittings'] = isinstance(r2, dict) and r2.get('browser') == []

    report['left_on_this_mac'] = changed_since(stamp)
    ok['nothing of ours was written under this Mac’s Library'] = not report['left_on_this_mac']
    report['ok'] = all(ok.values())
    if not a.keep:
        shutil.rmtree(folder, ignore_errors=True)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
