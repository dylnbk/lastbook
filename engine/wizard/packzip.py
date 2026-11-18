"""A published pack, read in place.

A pack on the download host is one zip file, up to several hundred gigabytes. Its members are stored (or deflated), so
each one is a run of bytes at a place the zip's own directory names. This reads that directory with three small Range
requests, then brings down only the members that are wanted: a library need not take the maps or the videos it will
not keep, and never needs room for the zip itself.

  pack = open_pack(url, cache)             the directory, fetched once and kept in `cache`, tied to the file's ETag
  pack.members                             [Member], in the order they lie in the file
  Puller(pack, dest, ...).run(wanted)      members -> files under dest, over several connections, carrying on where it stopped

What is checked: every member against the CRC-32 the zip records for it, and against the SHA-256 the pack's own
manifest gives when the caller passes one (`verify`). The whole zip's published SHA-256 cannot be checked without
reading all of it, so the caller should also compare pack.size with the size the release manifest publishes.

Nothing here opens a connection: reads go through engine/setup/download.py.
"""
import hashlib
import json
import os
import queue
import sqlite3
import struct
import threading
import time
import zlib
from collections import namedtuple
from pathlib import Path

from engine import sqlitefs
from engine.setup import download

Member = namedtuple('Member', 'name size csize method crc lho')     # lho: where its local header begins in the zip
EOCD, EOCD64_LOCATOR, EOCD64, CENTRAL, LOCAL = b'PK\x05\x06', b'PK\x06\x07', b'PK\x06\x06', b'PK\x01\x02', b'PK\x03\x04'
TAIL = 22 + 65535 + 20              # the end record, the longest comment it may carry, and the ZIP64 locator before it
BIG = 4 * 1024 * 1024               # a member this size or more has a connection to itself and can be carried on mid-file
RUN_MAX = 48 * 1024 * 1024          # smaller members that lie together come down in one request, up to this much
GAP = 256 * 1024                    # ...and across gaps up to this: cheaper to read and drop than to ask again
STATE = 'pull-state.sqlite'


class Pack:
    def __init__(self, url, size, etag, members, directory_at):
        self.url, self.size, self.etag, self.members, self.directory_at = url, size, etag, members, directory_at
        self.by_name = {m.name: m for m in members}
        ordered = sorted(members, key=lambda m: m.lho)
        self._next = {a.lho: b.lho for a, b in zip(ordered, ordered[1:])}
        if ordered:
            self._next[ordered[-1].lho] = directory_at

    def span_end(self, m):
        """The last byte that can belong to m: the one before whatever follows it in the file."""
        return self._next[m.lho] - 1


def _directory(url, control=None, allowed=None, context=None):
    info = download.head(url, allowed=allowed, context=context)
    size, etag = info['size'], info['etag']
    if not size or not info['ranges']:
        raise download.Failed('network', 'The download server would not send part of the pack, so Lastbook cannot read it in place.')
    tail_at = max(0, size - TAIL)
    tail = download.read_range(url, tail_at, size - 1, control, allowed=allowed, context=context, etag=etag)
    i = tail.rfind(EOCD)
    if i < 0:
        raise download.Failed('unpack', 'The pack on the download server is not a zip file Lastbook can read.')
    _, _, _, _, entries, cd_size, cd_at, _ = struct.unpack_from('<4sHHHHIIH', tail, i)
    j = i - 20
    if j >= 0 and tail[j:j + 4] == EOCD64_LOCATOR:      # a large pack: the true numbers are in the ZIP64 record this points to
        _, _, at64, _ = struct.unpack_from('<4sIQI', tail, j)
        rec = tail[at64 - tail_at:at64 - tail_at + 56] if at64 >= tail_at else download.read_range(url, at64, at64 + 55, control, allowed=allowed, context=context, etag=etag)
        if rec[:4] != EOCD64:
            raise download.Failed('unpack', 'The pack on the download server is not a zip file Lastbook can read.')
        entries, cd_size, cd_at = struct.unpack_from('<QQQ', rec, 32)
    if cd_at + cd_size > size or cd_size > 512 * 1024 * 1024:
        raise download.Failed('unpack', 'The pack on the download server is not a zip file Lastbook can read.')
    cd = download.read_range(url, cd_at, cd_at + cd_size - 1, control, allowed=allowed, context=context, etag=etag)
    members, at = [], 0
    while at + 46 <= len(cd) and cd[at:at + 4] == CENTRAL:
        (_, _, _, flags, method, _, _, crc, csize, usize, n, e, c, _, _, _, lho) = struct.unpack_from('<4sHHHHHHIIIHHHHHII', cd, at)
        name = cd[at + 46:at + 46 + n].decode('utf-8' if flags & 0x800 else 'cp437', 'replace')
        extra, k = cd[at + 46 + n:at + 46 + n + e], 0
        while k + 4 <= len(extra):
            eid, esz = struct.unpack_from('<HH', extra, k)
            if eid == 1:                                # ZIP64: only the fields that overflowed, in this order
                o = k + 4
                if usize == 0xFFFFFFFF:
                    usize, = struct.unpack_from('<Q', extra, o)
                    o += 8
                if csize == 0xFFFFFFFF:
                    csize, = struct.unpack_from('<Q', extra, o)
                    o += 8
                if lho == 0xFFFFFFFF:
                    lho, = struct.unpack_from('<Q', extra, o)
            k += 4 + esz
        if flags & 0x1:
            raise download.Failed('unpack', 'The pack is encrypted.')
        if not name.endswith('/'):
            members.append(Member(name, usize, csize, method, crc, lho))
        at += 46 + n + e + c
    if at != len(cd):
        raise download.Failed('unpack', 'The pack’s list of files could not be read to its end.')
    return {'url': info['url'], 'size': size, 'etag': etag, 'directory_at': cd_at, 'entries': entries, 'members': members}


def open_pack(url, cache=None, control=None, allowed=None, context=None, refresh=False):
    """The pack's directory. cache: a folder; the directory is kept there and used again while the server's file is the same."""
    kept = Path(cache) / 'pack-directory.json' if cache else None
    if kept and kept.is_file() and not refresh:
        try:
            d = json.loads(kept.read_text(encoding='utf-8'))
            if d.get('asked') == url:
                now = download.head(url, allowed=allowed, context=context)
                if now['etag'] == d['etag'] and now['size'] == d['size']:
                    return Pack(url, d['size'], d['etag'], [Member(*m) for m in d['members']], d['directory_at'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    d = _directory(url, control, allowed, context)
    if kept:
        kept.parent.mkdir(parents=True, exist_ok=True)
        tmp = kept.with_suffix('.tmp')
        tmp.write_text(json.dumps({'asked': url, 'size': d['size'], 'etag': d['etag'], 'directory_at': d['directory_at'], 'members': d['members']}), encoding='utf-8')
        os.replace(tmp, kept)
    return Pack(url, d['size'], d['etag'], d['members'], d['directory_at'])


class Refused(Exception):
    """A member that arrived and is not what the zip, or the pack's manifest, says it should be."""


class Puller:
    """Brings members of a pack down as files under dest. strip: a leading folder every name loses (the pack's own top folder).
    verify: {member name: sha256} from the pack's manifest. progress(done_bytes, total_bytes, done_files, total_files).
    A second run does only what the first did not finish; a large member carries on from the byte it reached."""

    def __init__(self, pack, dest, control=None, workers=8, progress=None, strip='', verify=None, allowed=None, context=None):
        self.pack, self.dest = pack, Path(dest)
        self.control = control or download.Control()
        self.workers, self.progress, self.strip, self.verify = max(1, workers), progress, strip, verify or {}
        self.allowed, self.context = allowed, context
        self.lock = threading.Lock()
        self.made = set()
        self.errors = []
        self.pending, self.noted_at = [], time.time()
        self.done_bytes = self.total_bytes = self.done_files = self.total_files = 0
        self._told = 0
        self.dest.mkdir(parents=True, exist_ok=True)

        def make(db):
            db.execute('CREATE TABLE IF NOT EXISTS done(name TEXT PRIMARY KEY, size INTEGER, crc INTEGER, sha256 TEXT) WITHOUT ROWID')
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID')
        # Kept open for hours, on the library's own drive: opened through sqlitefs, which see for why.
        self.state = sqlitefs.Database(self.dest / STATE, make, synchronous='NORMAL')
        was = dict(self.state.run(lambda db: db.execute('SELECT key, value FROM meta').fetchall()))
        if was.get('etag') and was['etag'] != pack.etag:
            raise download.Failed('changed', 'The pack on the download server is not the one this folder was begun from. Start in an empty folder.')
        self.state.run(lambda db: db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [('etag', pack.etag or ''), ('url', pack.url)]))

    def path(self, m):
        rel = m.name[len(self.strip):] if self.strip and m.name.startswith(self.strip) else m.name
        parts = [p for p in rel.split('/') if p not in ('', '.')]
        if not parts or any(p == '..' or '\\' in p or ':' in p or '\0' in p for p in parts):
            raise Refused(f'a member with a name Lastbook does not accept ({m.name[:80]!r})')
        return self.dest.joinpath(*parts)

    def have(self, m):
        """Is this member already here, whole and checked?"""
        row = self.state.run(lambda db: db.execute('SELECT size, crc FROM done WHERE name=?', (m.name,)).fetchone())
        if not row or row[0] != m.size or row[1] != m.crc:
            return False
        try:
            return self.path(m).stat().st_size == m.size
        except OSError:
            return False

    def noted(self):
        """{member name: (size, crc, sha256)} for everything brought down and checked so far."""
        return {n: (s, c, h) for n, s, c, h in self.state.run(lambda db: db.execute('SELECT name, size, crc, sha256 FROM done').fetchall())}

    def sha256(self, name):
        row = self.state.run(lambda db: db.execute('SELECT sha256 FROM done WHERE name=?', (name,)).fetchone())
        return row[0] if row else None

    # ---- planning
    def plan(self, wanted):
        """[(kind, members)] for what is not here yet: ('big', [m]) or ('run', [m, ...]) in the order of `wanted`'s first of each."""
        need = [m for m in wanted if not self.have(m)]
        order = {m.name: i for i, m in enumerate(need)}
        tasks = [('big', [m]) for m in need if m.csize >= BIG]
        run = []
        for m in sorted((m for m in need if m.csize < BIG), key=lambda m: m.lho):
            if run and (m.lho - self.pack.span_end(run[-1]) - 1 > GAP or self.pack.span_end(m) - run[0].lho > RUN_MAX):
                tasks.append(('run', run))
                run = []
            run.append(m)
        if run:
            tasks.append(('run', run))
        tasks.sort(key=lambda t: min(order[m.name] for m in t[1]))
        return need, tasks

    def run(self, wanted):
        """Returns {'files', 'bytes', 'skipped', 'errors'}. Raises Stopped when paused or cancelled, Failed when the line or the disk gives out."""
        wanted = list(wanted)
        need, tasks = self.plan(wanted)
        self.total_files, self.total_bytes = len(wanted), sum(m.size for m in wanted)
        self.done_files, self.done_bytes = len(wanted) - len(need), self.total_bytes - sum(m.size for m in need)
        self._tell(force=True)
        q = queue.Queue()
        for t in tasks:
            q.put(t)
        fatal = []

        def work():
            while not fatal:
                try:
                    kind, members = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    (self._big if kind == 'big' else self._run)(members)
                except download.Stopped as e:
                    fatal.append(e)
                except download.Failed as e:
                    if e.code in ('no_space', 'write', 'changed'):
                        fatal.append(e)             # nothing more can be saved, or the file is no longer the one begun
                        self.control.stop('paused')
                    else:
                        with self.lock:
                            self.errors.append((members[0].name, e.message))
                except Refused as e:
                    with self.lock:
                        self.errors.append((members[0].name, str(e)))
                except Exception as e:              # one member must not end the others
                    with self.lock:
                        self.errors.append((members[0].name, f'{type(e).__name__}: {e}'))

        threads = [threading.Thread(target=work, daemon=True, name=f'pull-{i}') for i in range(min(self.workers, max(1, len(tasks))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self._note()
        self._tell(force=True)
        if fatal:
            raise fatal[0]
        return {'files': self.done_files, 'bytes': self.done_bytes, 'skipped': len(wanted) - len(need), 'errors': list(self.errors)}

    # ---- writing
    def _folder(self, path):
        parent = path.parent
        if parent not in self.made:
            parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                self.made.add(parent)

    def _check(self, m, size, crc, digest):
        if size != m.size or crc != m.crc:
            raise Refused(f'{m.name}: not the file the pack lists (size or CRC differs)')
        want = self.verify.get(m.name)
        if want and want.lower() != digest:
            raise Refused(f'{m.name}: not the file the pack’s manifest lists (SHA-256 differs)')

    def _record(self, m, digest):
        """Noted in memory and written a few hundred at a time: one commit a file would cost more than the small files do.
        What is lost if the program is killed between writes is only the note, and the file is then fetched again."""
        with self.lock:
            self.pending.append((m.name, m.size, m.crc, digest))
            self.done_files += 1
            due = len(self.pending) >= 300 or time.time() - self.noted_at > 3
        if due:
            self._note()
        self._tell()

    def _note(self):
        with self.lock:
            rows, self.pending, self.noted_at = self.pending, [], time.time()
        if not rows:
            return

        def write(db):
            db.execute('BEGIN')
            try:
                db.executemany('INSERT OR REPLACE INTO done VALUES (?,?,?,?)', rows)
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        try:
            self.state.run(write)
        except sqlite3.Error:
            with self.lock:
                self.pending = rows + self.pending      # still to be noted; the files themselves are already whole
            raise

    def _add(self, n):
        with self.lock:
            self.done_bytes += n
        self._tell()

    def _tell(self, force=False):
        if self.progress and (force or time.time() - self._told > 0.5):
            self._told = time.time()
            self.progress(self.done_bytes, self.total_bytes, self.done_files, self.total_files)

    def _store(self, m, raw):
        """A small member, whole in memory."""
        data = raw if m.method == 0 else zlib.decompress(raw, -15) if m.method == 8 else None
        if data is None:
            raise Refused(f'{m.name}: a kind of compression Lastbook does not read')
        digest = hashlib.sha256(data).hexdigest()
        self._check(m, len(data), zlib.crc32(data), digest)
        path = self.path(m)
        self._folder(path)
        part = path.with_name(path.name + '.partial')
        try:
            with open(part, 'wb') as f:
                f.write(data)
            os.replace(part, path)
        except OSError as e:
            raise download.Failed('no_space' if download.disk_full(e) else 'write',
                                  'The disk filled up while downloading. Free some space, then try again.' if download.disk_full(e)
                                  else f'Lastbook could not write to the library’s disk ({type(e).__name__}).')
        self._add(m.size)
        self._record(m, digest)

    def _run(self, members):
        """Several small members that lie together: one request from the first one's header to the end of the last."""
        start, end = members[0].lho, self.pack.span_end(members[-1])
        state = {'buf': bytearray(), 'pos': start, 'i': 0, 'stage': 'seek', 'bad': []}

        def sink(b):
            buf = state['buf']
            buf += b
            while state['i'] < len(members):
                m = members[state['i']]
                if state['stage'] == 'seek':
                    skip = m.lho - state['pos']
                    if skip < 0:
                        raise download.Failed('unpack', 'The pack lists two files in the same place.')
                    if skip > len(buf):
                        state['pos'] += len(buf)
                        buf.clear()
                        return
                    del buf[:skip]
                    state['pos'], state['stage'] = m.lho, 'head'
                if state['stage'] == 'head':
                    if len(buf) < 30:
                        return
                    if buf[:4] != LOCAL:
                        raise download.Failed('unpack', 'The pack does not hold a file where its list says one is.')
                    n, e = struct.unpack_from('<HH', buf, 26)
                    if len(buf) < 30 + n + e:
                        return
                    del buf[:30 + n + e]
                    state['pos'] += 30 + n + e
                    state['stage'] = 'data'
                if state['stage'] == 'data':
                    if len(buf) < m.csize:
                        return
                    raw = bytes(buf[:m.csize])
                    del buf[:m.csize]
                    state['pos'] += m.csize
                    state['i'] += 1
                    state['stage'] = 'seek'
                    try:
                        self._store(m, raw)
                    except Refused as e:     # this one member; the rest of the run is still good
                        state['bad'].append((m.name, str(e)))
            buf.clear()                      # past the last member wanted: whatever follows is not kept

        download.stream_range(self.pack.url, start, end, sink, self.control, allowed=self.allowed, context=self.context, etag=self.pack.etag)
        if state['i'] < len(members):
            raise download.Failed('unpack', 'The pack ended before the files its list names.')
        if state['bad']:
            with self.lock:
                self.errors.extend(state['bad'])        # each under its own name: the caller leaves out that file, not its neighbour

    def _big(self, members):
        """One large member on a connection of its own, written as it arrives. A part already here is carried on from."""
        m, = members
        if m.method not in (0, 8):
            raise Refused(f'{m.name}: a kind of compression Lastbook does not read')
        path = self.path(m)
        self._folder(path)
        part = path.with_name(path.name + '.partial')
        head = download.read_range(self.pack.url, m.lho, m.lho + 29, self.control, allowed=self.allowed, context=self.context, etag=self.pack.etag)
        if head[:4] != LOCAL:
            raise download.Failed('unpack', 'The pack does not hold a file where its list says one is.')
        n, e = struct.unpack_from('<HH', head, 26)
        data_at = m.lho + 30 + n + e
        h, crc, have = hashlib.sha256(), 0, 0
        if m.method == 0:
            try:
                have = min(os.path.getsize(part), m.csize)
            except OSError:
                have = 0
            if have:
                with open(part, 'r+b') as f:
                    f.truncate(have)
                    f.seek(0)
                    while chunk := f.read(8 * 1024 * 1024):
                        self.control.check()
                        h.update(chunk)
                        crc = zlib.crc32(chunk, crc)
                self._add(have)
        inflate = zlib.decompressobj(-15) if m.method == 8 else None
        got = {'crc': crc, 'size': have, 'raw': have}
        try:
            with open(part, 'ab' if have else 'wb') as f:
                def sink(b):
                    got['raw'] += len(b)
                    out = inflate.decompress(b) if inflate else b
                    if out:
                        f.write(out)
                        h.update(out)
                        got['crc'] = zlib.crc32(out, got['crc'])
                        got['size'] += len(out)
                        self._add(len(out))
                if have < m.csize:
                    download.stream_range(self.pack.url, data_at + have, data_at + m.csize - 1, sink, self.control,
                                          allowed=self.allowed, context=self.context, etag=self.pack.etag)
                if inflate:
                    rest = inflate.flush()
                    if rest:
                        f.write(rest)
                        h.update(rest)
                        got['crc'] = zlib.crc32(rest, got['crc'])
                        got['size'] += len(rest)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            raise download.Failed('no_space' if download.disk_full(e) else 'write',
                                  'The disk filled up while downloading. Free some space, then try again.' if download.disk_full(e)
                                  else f'Lastbook could not write to the library’s disk ({type(e).__name__}).')
        digest = h.hexdigest()
        try:
            self._check(m, got['size'], got['crc'], digest)
        except Refused:
            self._add(-got['size'])
            try:
                os.unlink(part)             # not the file: it is begun again from nothing the next time
            except OSError:
                pass
            raise
        os.replace(part, path)
        self._record(m, digest)

    def close(self):
        self._note()
        self.state.close()
