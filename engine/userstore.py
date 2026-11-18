"""What the person keeps: saved items, where they were in a video, the chats. One small SQLite file in the library's own
folder (LastbookData/user/userdata.sqlite), so it goes where the library goes and nothing is left in a web view's storage.

  kv     the page's own settings, by name, each a string exactly as the page would have put it in localStorage
  chats  one conversation each, by its id, so a long history is never sent or rewritten as a whole

The file is made for a drive that can be pulled out: a rollback journal (one file, no -wal or -shm beside it) and a full
sync at each commit. It is opened through engine/sqlitefs.py, because on exFAT a connection kept from the moment a
database is made stops being able to write a few seconds later. A file that will not open is moved aside and a new one
begun, because losing the list of saved pages must never be what stops the library opening.
"""
import sqlite3
import threading
import time
from pathlib import Path

from engine import sqlitefs

NAMESPACES = ('kv', 'chats')
MAX_VALUE = 1_000_000                       # one setting, or one conversation
MAX_ID = 200
MAX_OPS = 500                               # in one request
KEEP = {'kv': 400, 'chats': 200}            # the most of each kind; the oldest go first


class Refused(ValueError):
    pass


def _make(db):
    db.execute('CREATE TABLE IF NOT EXISTS item(ns TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, updated REAL NOT NULL, '
               'PRIMARY KEY(ns, id)) WITHOUT ROWID')
    db.execute('SELECT count(*) FROM item').fetchone()     # a damaged file says so here, not at the first save


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / 'userdata.sqlite'
        self.base = sqlitefs.Database(self.path, _make)
        self.lock = threading.Lock()
        self.set_aside = False

    def _run(self, fn):
        """fn(connection), with a file that is not a database (a drive pulled out mid-write, long ago) moved aside once and begun again."""
        with self.lock:
            try:
                return self.base.run(fn)
            except sqlite3.OperationalError:
                raise                               # the disk is full, or gone: not something a new file mends
            except sqlite3.DatabaseError as e:
                if self.set_aside:
                    raise
                self.set_aside = True
                self.base.close()
                aside = self.path.with_name(f'userdata.unreadable-{time.strftime("%Y%m%d-%H%M%S")}.sqlite')
                print(f'user store: {type(e).__name__}: {e}; the file is kept as {aside.name} and a new one begun', flush=True)
                for side in ('', '-journal'):
                    old = Path(str(self.path) + side)
                    if old.exists():
                        old.replace(Path(str(aside) + side))
                return self.base.run(fn)

    def all(self):
        """{'kv': {name: string}, 'chats': {id: string}}"""
        def read(db):
            out = {ns: {} for ns in NAMESPACES}
            for ns, key, value in db.execute('SELECT ns, id, value FROM item'):
                if ns in out:
                    out[ns][key] = value
            return out
        return self._run(read)

    def apply(self, ops):
        """ops: [{'op': 'put', 'ns', 'id', 'value'} | {'op': 'delete', 'ns', 'id'} | {'op': 'clear', 'ns'}]. All of them, or none."""
        if not isinstance(ops, list) or len(ops) > MAX_OPS:
            raise Refused('ops must be a list of at most %d' % MAX_OPS)
        checked = []
        for o in ops:
            if not isinstance(o, dict) or o.get('op') not in ('put', 'delete', 'clear') or o.get('ns') not in NAMESPACES:
                raise Refused('each op needs op (put, delete or clear) and ns (%s)' % ' or '.join(NAMESPACES))
            key, value = o.get('id'), o.get('value')
            if o['op'] != 'clear' and not (isinstance(key, str) and 0 < len(key) <= MAX_ID):
                raise Refused('id must be a string of 1 to %d characters' % MAX_ID)
            if o['op'] == 'put' and not (isinstance(value, str) and len(value) <= MAX_VALUE):
                raise Refused('value must be a string of at most %d characters' % MAX_VALUE)
            checked.append((o['op'], o['ns'], key, value))
        now = time.time()

        def write(db):
            db.execute('BEGIN IMMEDIATE')
            try:
                for i, (op, ns, key, value) in enumerate(checked):
                    if op == 'put':     # each a moment after the one before, so "the oldest" has one answer even within a request
                        db.execute('INSERT INTO item(ns, id, value, updated) VALUES (?,?,?,?) '
                                   'ON CONFLICT(ns, id) DO UPDATE SET value=excluded.value, updated=excluded.updated', (ns, key, value, now + i * 1e-6))
                    elif op == 'delete':
                        db.execute('DELETE FROM item WHERE ns=? AND id=?', (ns, key))
                    else:
                        db.execute('DELETE FROM item WHERE ns=?', (ns,))
                for ns, keep in KEEP.items():
                    db.execute('DELETE FROM item WHERE ns=? AND id NOT IN (SELECT id FROM item WHERE ns=? ORDER BY updated DESC LIMIT ?)', (ns, ns, keep))
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        self._run(write)
        return len(checked)

    def journal_mode(self):
        return self._run(lambda db: db.execute('PRAGMA journal_mode').fetchone()[0])

    def close(self):
        with self.lock:
            self.base.close()
