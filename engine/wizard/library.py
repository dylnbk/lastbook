"""Writing a library that holds part of a pack: the files that say what is in it.

A library is library-data/ with catalog.json (every record), search.sqlite (the words of every page, for searching),
summary.json (the counts the app shows) and, beside library-data/, pack.json (what this library is) and
pack-manifest.tsv (every file with its size and SHA-256). A library made of chosen parts needs each of them to say
only what is really there, or the app would list documents it cannot open.

  write_catalog(lib, records)                 catalog.json, whole or not at all
  write_search(source, lib, keep_ids)         search.sqlite with only the kept records' words
  write_summary(lib, records, search)         summary.json, counted from what is there
  write_pack(data, was, choice, counts)       pack.json
  write_manifest(data, entries)               pack-manifest.tsv

Each is written beside where it goes and renamed into place, so the app never reads half of one.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import time
import urllib.parse
from pathlib import Path

from engine import sqlitefs

CHUNK = 8 * 2**20


def _replace(path, write):
    """write(tmp path) then rename over path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    write(tmp)
    os.replace(tmp, path)


def write_json(path, value):
    _replace(path, lambda tmp: tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8'))


def write_catalog(lib, records):
    """library-data/catalog.json. The app reads it again when its time of change moves on."""
    write_json(Path(lib) / 'catalog.json', records)


def copy_file(src, dest, control=None, told=None):
    """One file to another disk through dest.part: a copy that stops is never taken for a whole file. Returns its SHA-256."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.part')
    h = hashlib.sha256()
    try:
        with open(src, 'rb') as a, open(part, 'wb') as b:
            while chunk := a.read(CHUNK):
                if control is not None:
                    control.check()
                b.write(chunk)
                h.update(chunk)
                if told:
                    told(len(chunk))
        os.replace(part, dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return h.hexdigest()


def sha256_of(path, control=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(CHUNK):
            if control is not None:
                control.check()
            h.update(chunk)
    return h.hexdigest()


def write_search(source, lib, keep_ids, control=None):
    """The pack's search.sqlite with only the rows of the records kept. `source` is the pack's own file, read and not changed.
    Returns the number of pages left."""
    dest = Path(lib) / 'search.sqlite'
    tmp = dest.with_name('search.sqlite.new')
    small = tmp.with_name('search.sqlite.small')
    for p in (tmp, tmp.with_name(tmp.name + '-journal'), small, small.with_name(small.name + '-journal')):
        try:
            p.unlink()
        except OSError:
            pass
    keep_ids = set(keep_ids)
    pages = _fresh_search(source, small, keep_ids, control)
    if pages is not None:
        os.replace(small, dest)
        return pages
    copy_file(source, tmp, control)
    db = sqlitefs.connect(tmp, synchronous='OFF')
    try:
        # One read through the table says which rows go (doc_id is kept but not indexed); they are then removed by row number.
        gone = [rowid for rowid, doc in db.execute('SELECT rowid, doc_id FROM pages') if doc not in keep_ids]
        total = db.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
        if gone:
            db.execute('BEGIN')
            for i in range(0, len(gone), 400):
                if control is not None:
                    control.check()
                part = gone[i:i + 400]
                db.execute('DELETE FROM pages WHERE rowid IN (%s)' % ','.join('?' * len(part)), part)
            db.execute('COMMIT')
            db.execute("INSERT INTO pages(pages) VALUES('optimize')")
        pages = total - len(gone)
        packed = len(gone) > 0.15 * max(1, total)
        if packed:      # the room the removed pages took is given back by writing the index out afresh, straight to its place: no temporary copy elsewhere
            db.execute('VACUUM INTO ?', (str(small),))
    finally:
        db.close()
    if packed:
        os.replace(small, dest)
        tmp.unlink()
    else:
        os.replace(tmp, dest)
    return pages


FRESH_UNDER = 0.5       # of the pack's pages kept: under this, the index is written anew from them instead of cut down


def _fresh_search(source, out, keep_ids, control=None):
    """When most of the pack's pages go, the kept ones are written into a new index: the pack's own is read where it is,
    not copied, and nothing is deleted a page at a time (removing a page from a full-text index costs as much as adding
    it). Measured on the All pack's index (140,513 pages, 0.76 GB) kept to 3,945 pages, on an exFAT drive: 4.6 s,
    against 55 s for copying it and cutting it down; the same pages, and the same answers to a search.
    Returns the number of pages, or None when enough is kept that cutting the pack's index down is the cheaper way."""
    src = sqlite3.connect(f'file:{urllib.parse.quote(str(source))}?mode=ro', uri=True)
    try:
        made = src.execute("SELECT sql FROM sqlite_master WHERE name='pages' AND type='table'").fetchone()
        if not made or 'fts5' not in made[0].lower():
            return None
        columns = [r[1] for r in src.execute('PRAGMA table_info(pages)')]
        keep, total = [], 0
        for rowid, doc in src.execute('SELECT rowid, doc_id FROM pages'):
            total += 1
            if doc in keep_ids:
                keep.append(rowid)
    finally:
        src.close()
    if len(keep) > FRESH_UNDER * max(1, total):
        return None
    names = ','.join(columns)
    db = sqlitefs.connect(out, lambda d: d.execute(made[0]), synchronous='OFF')
    try:
        db.execute('ATTACH DATABASE ? AS pack', (str(source),))     # read only: nothing below writes to it
        db.execute('BEGIN')
        for i in range(0, len(keep), 400):
            if control is not None:
                control.check()
            part = keep[i:i + 400]
            db.execute(f'INSERT INTO pages(rowid,{names}) SELECT rowid,{names} FROM pack.pages WHERE rowid IN (%s)' % ','.join('?' * len(part)), part)
        db.execute('COMMIT')
        db.execute("INSERT INTO pages(pages) VALUES('optimize')")
        db.execute('DETACH DATABASE pack')
    except BaseException:
        db.close()
        try:
            out.unlink()
        except OSError:
            pass
        raise
    db.close()
    return len(keep)


def write_summary(lib, records):
    """summary.json: the counts on the app's front page, of what this library really holds."""
    lib = Path(lib)
    kinds = {}
    for r in records:
        kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
    out = {'resources': len(records), 'pdfs': kinds.get('pdf', 0), 'pdf_pages': sum(r.get('pages') or 0 for r in records if r['kind'] == 'pdf'),
           'web_pages': kinds.get('html', 0), 'videos': sum(1 for r in records if r['kind'] == 'video' and not r.get('words_only')),
           'collections': kinds.get('zim', 0), 'document_bytes': sum(r.get('bytes') or 0 for r in records if not r.get('words_only'))}
    words = sum(1 for r in records if r['kind'] == 'video' and r.get('words_only'))
    if words:
        out['videos_words_only'] = words
    write_json(lib / 'summary.json', out)
    return out


def write_pack(data, was, choice, counts, maps=()):
    """pack.json: what this library is. `was` is the pack.json of the pack it was made from."""
    out = {'name': 'custom', 'size': 'custom', 'map': 'base', 'built': time.strftime('%Y-%m-%d'), 'sample': False, 'add_ons': [],
           'made_from': {k: was.get(k) for k in ('name', 'snapshot', 'built') if was.get(k)}, 'choice': choice, 'counts': counts, 'regions': list(maps),
           'known_gaps': was.get('known_gaps') or [], 'incomplete': 0}
    write_json(Path(data) / 'pack.json', out)
    return out


def write_manifest(data, entries):
    """pack-manifest.tsv, in the pack's own form: path, bytes, sha256, how, changes. entries: (path, bytes, sha256, how, changes)."""
    def write(tmp):
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write('path\tbytes\tsha256\thow\tchanges\n')
            for path, size, sha, how, changes in sorted(entries):
                f.write(f'{path}\t{size}\t{sha or ""}\t{how}\t{changes}\n')
    _replace(Path(data) / 'pack-manifest.tsv', write)


def free_bytes(folder):
    p = Path(folder)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free
