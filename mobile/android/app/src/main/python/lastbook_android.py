"""Lastbook on Android: the desktop's own engine (server.py and engine/), started inside the app and answering the app's
WebView at 127.0.0.1, on a port the system chooses.

Called from MainActivity.kt:
  libraries(roots)                    the LastbookData folders under these storage folders: [[path, what it holds], ...]
  start(data, pages, state, cache)    open that library; returns the port the pages are served on
  stop()

What is the same as the desktop: everything the engine does with a library (search, reading, videos, maps of any region,
the place finder). What is not here: encyclopedias (libzim has no build for Android), an AI model (there is no llama-server
for a phone in this app), setup and the builder (a library is made on a computer).
"""
import json
import os
import sys
import threading
import traceback

_srv = None
_lock = threading.Lock()


def _looks_like_library(folder):
    return os.path.isfile(os.path.join(folder, 'library-data', 'catalog.json'))


def libraries(roots):
    """A library is a folder called LastbookData holding library-data/catalog.json: at the top of a drive, or one folder down."""
    out, seen = [], set()
    for root in roots:
        root = str(root)
        candidates = [os.path.join(root, 'LastbookData')]
        try:
            candidates += [os.path.join(root, name, 'LastbookData') for name in sorted(os.listdir(root)) if not name.startswith('.')]
        except OSError:
            pass
        for c in candidates:
            if c in seen or not _looks_like_library(c):
                continue
            seen.add(c)
            what = ''
            try:
                with open(os.path.join(c, 'pack.json'), encoding='utf-8') as f:
                    pack = json.load(f)
                counts = pack.get('counts') or {}
                if counts:
                    what = f'{counts.get("documents", 0):,} documents, {counts.get("videos", 0):,} videos'
                    if pack.get('regions'):
                        what += f', maps of {len(pack["regions"])} regions'
                else:
                    what = str(pack.get('title') or pack.get('name') or '')
            except (OSError, ValueError):
                pass
            out.append([c, what])
    return out


def start(data, pages, state, cache):
    """Returns the port. Raises with a plain sentence when the library cannot be opened."""
    global _srv
    with _lock:
        if _srv is not None:
            return _srv.server_address[1]
        if not _looks_like_library(data):
            raise RuntimeError('That folder is not a Lastbook library: it has no library-data/catalog.json.')
        os.environ['LASTBOOK_DATA'] = data              # before the engine is imported: it reads these once
        os.environ['LASTBOOK_APP'] = pages              # web/ and content/, unpacked from the package by MainActivity
        os.environ['LASTBOOK_AI_PICKER'] = '0'          # no program to run a model on a phone in this app
        os.environ['LASTBOOK_NO_SETUP'] = '1'           # a library is made on a computer: no setup and no builder here
        os.environ['TMPDIR'] = cache
        if os.access(data, os.W_OK):
            # A drive that can be written to keeps what the person saves, as on a computer: it travels with the library
            os.environ['LASTBOOK_PORTABLE'] = '1'
            os.environ['LASTBOOK_PORTABLE_STATE'] = state
        try:
            import server
            import shell_browser
            server.init(no_index=True)
            _srv = shell_browser._server(server.Handler, 0)
        except Exception:
            traceback.print_exc()
            raise
        threading.Thread(target=_srv.serve_forever, daemon=True, name='lastbook-pages').start()
        print('Lastbook: pages on 127.0.0.1:%d, library %s' % (_srv.server_address[1], data), flush=True)
        return _srv.server_address[1]


def stop():
    global _srv
    with _lock:
        if _srv is not None:
            _srv.shutdown()
            _srv.server_close()
            _srv = None


def python_says():
    """What this Python can do, for the "about" line: whether its SQLite has what the search and the maps need."""
    import sqlite3
    db = sqlite3.connect(':memory:')
    have = {}
    for name, sql in (('search (FTS5)', "CREATE VIRTUAL TABLE t USING fts5(x)"), ('maps (R*Tree)', "CREATE VIRTUAL TABLE r USING rtree(id, a, b)")):
        try:
            db.execute(sql)
            have[name] = True
        except sqlite3.Error:
            have[name] = False
    db.close()
    return 'Python %s, SQLite %s: %s' % (sys.version.split()[0], sqlite3.sqlite_version, ', '.join(f'{k} {"yes" if v else "NO"}' for k, v in have.items()))
