"""SQLite on a drive that does not fix a new file's number at once (exFAT on a Mac).

On such a drive a file has a provisional file number while it is first open and is given its lasting one a few seconds
later. SQLite notes a database's file number when it opens it and, before every write, checks that the path still leads
to that number. When it does not, SQLite takes the file to have been renamed under it and refuses each write from then
on with SQLITE_READONLY_DBMOVED ("attempt to write a readonly database"). The database is not damaged and nothing was
moved: the connection is simply holding the old number.

Measured on an exFAT drive under macOS's FSKit driver (18 Nov 2026, SQLite 3.45.1), in rollback-journal mode:
  one connection kept from the moment the file was made     the number changed after about 10 s; every write after that failed
  the file made and closed, then a connection opened        the number was already the lasting one; no write failed
APFS never changes a file's number, so nothing here costs it anything.

  connect(path, init)      makes the database with init(connection) if it is not there, closes it, and returns a
                           connection opened afterwards. Every database Lastbook writes on a library's drive is opened this way.
  Database(path, init)     the same, kept: a write that still meets the error (a driver that changes the number later)
                           is tried once more on a fresh connection.

Everything is opened with a rollback journal: one file, with no -wal or -shm beside it to be left behind or copied without.
"""
import sqlite3
import threading
from pathlib import Path


def moved(error):
    """Is this SQLite saying the database file is not where it was? Any of the read-only family counts: all are mended the same way."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)     # Python 3.11 and later
    return (code & 0xff) == 8 if code is not None else 'readonly database' in str(error)


def _prepare(db, synchronous):
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute(f'PRAGMA synchronous={synchronous}')


def connect(path, init=None, synchronous='FULL', timeout=30, check_same_thread=False):
    """A read-write connection in autocommit mode (transactions are begun and ended by the caller)."""
    path = Path(path)
    try:
        fresh = path.stat().st_size == 0
    except OSError:
        fresh = True
    if fresh:
        if init is None:
            raise ValueError(f'{path.name} is not there yet and no way to make it was given')
        path.parent.mkdir(parents=True, exist_ok=True)
        first = sqlite3.connect(path, timeout=timeout, isolation_level=None)
        try:
            _prepare(first, synchronous)
            init(first)
        finally:
            first.close()       # made, and closed: only now does the drive give it the number it will keep
    db = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=check_same_thread)
    _prepare(db, synchronous)
    if init is not None and not fresh:
        init(db)                # an older file may lack what a newer version adds: init is written to be run again
    return db


class Database:
    """One database, kept open and shared between threads. run(fn) calls fn(connection) under the lock and returns what it returns."""

    def __init__(self, path, init, synchronous='FULL'):
        self.path, self.init, self.synchronous = Path(path), init, synchronous
        self.lock = threading.RLock()
        self.db = None

    def _open(self):
        if self.db is None:
            self.db = connect(self.path, self.init, self.synchronous)
        return self.db

    def run(self, fn):
        with self.lock:
            try:
                return fn(self._open())
            except sqlite3.OperationalError as e:
                if not moved(e):
                    raise
                self._drop()
            return fn(self._open())     # once more, on a connection that holds the file's number as it is now

    def _drop(self):
        db, self.db = self.db, None
        if db is not None:
            try:
                if db.in_transaction:
                    db.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            try:
                db.close()
            except sqlite3.Error:
                pass

    def close(self):
        with self.lock:
            self._drop()
