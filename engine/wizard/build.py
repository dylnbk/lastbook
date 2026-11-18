"""Building a library of chosen parts, on the person's own computer, from a published pack and open map data.

  Build(target, source, choice).run()

target   the LastbookData folder to make (on any disk; a drive that travels is marked so with choice['portable'])
source   where the pack's files come from:
           LocalSource(folder)   a copy of the pack already on a disk here
           RemoteSource(url)     the published pack, read in place: only the chosen files are downloaded
choice   {'documents': [...], 'encyclopedias': [...], 'videos': {...}      as engine/wizard/contents.py
          'maps': [{'id': 'great-britain', 'level': 'detail', 'terrain': True}, ...],
          'encoder': 'software' or 'hardware', 'threads': 6, 'portable': True}

What it does, in this order, each step picking up where a run that stopped left off:
  1. maps      each region not already built (engine/mapbuild)
  2. files     everything brought as it is: documents, pages, transcripts, encyclopedias, videos that are small already
  3. library   catalog, search index and counts are written: from here the library opens in the app, with every video
               still to be made smaller shown by its words alone
  4. videos    one at a time, each original read, re-encoded and (from a remote pack) deleted; the catalog is rewritten
               as they finish, so the library fills in while it is being used
  5. finish    the manifest of every file, with sizes and checksums

Nothing here opens a connection itself: RemoteSource goes through engine/wizard/packzip.py and engine/setup/download.py.
Polite by default: one video at a time at low priority on a limited number of threads, and whatever rate limit the caller
set on download.THROTTLE.
"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

from engine import portable
from engine.setup import download
from . import contents, library, packzip, video

LIB = contents.LIB
STATE = Path('derived') / 'wizard'
REWRITE_EVERY = 300         # seconds between rewrites of the catalog while videos are being made smaller
REFILL_EVERY = 1800         # ...and between looks for files that were not in the pack folder yet
TOP = 'LastbookData/'       # the folder every member of a published pack is under


class Refused(Exception):
    """A plain sentence saying why a build cannot go ahead."""


class LocalSource:
    """A pack already on a disk of this computer: <folder>/library-data/..., pack.json, pack-manifest.tsv."""
    remote = False

    def __init__(self, folder):
        self.folder = Path(folder)

    def meta(self, scratch=None, control=None):
        try:
            records = json.loads((self.folder / 'library-data' / 'catalog.json').read_text(encoding='utf-8'))
            manifest = contents.read_manifest((self.folder / 'pack-manifest.tsv').read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise Refused(f'That folder does not hold a pack ({type(e).__name__}).')
        try:
            was = json.loads((self.folder / 'pack.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            was = {}
        return records, manifest, was

    def local(self, pack_path):
        p = self.folder / pack_path
        return p if p.is_file() else None

    def bring(self, items, target, control=None, told=None):
        """Copies (pack path, bytes, sha256) into target. What is already there at its full size is left. Returns paths that could not be had."""
        missing = []
        for path, size, _ in items:
            dest = Path(target) / path
            try:
                if dest.stat().st_size == size:
                    if told:
                        told(size)
                    continue
            except OSError:
                pass
            src = self.local(path)
            if src is None or src.stat().st_size != size:
                missing.append(path)
                continue
            library.copy_file(src, dest, control, told)
        return missing

    def original(self, pack_path, scratch, control=None):
        """(path of the file to read, whether it is a temporary copy to delete after)."""
        return self.local(pack_path), False

    def search_index(self, scratch, control=None):
        return self.local(LIB + 'search.sqlite')


class RemoteSource:
    """A published pack, read in place over the network. Only what is asked for is downloaded."""
    remote = True

    def __init__(self, url, workers=8, allowed=None, context=None):
        self.url, self.workers, self.allowed, self.context = url, workers, allowed, context
        self.pack = None
        self.manifest = {}

    def _puller(self, dest, control, progress=None, workers=None):
        return packzip.Puller(self.pack, dest, control=control, workers=workers or self.workers, progress=progress, strip=TOP,
                              verify={TOP + p: h for p, (_, h) in self.manifest.items() if h}, allowed=self.allowed, context=self.context)

    def _members(self, paths):
        out, missing = [], []
        for p in paths:
            m = self.pack.by_name.get(TOP + p)
            (out if m is not None else missing).append(m if m is not None else p)
        return out, missing

    def meta(self, scratch, control=None):
        scratch = Path(scratch)
        self.pack = packzip.open_pack(self.url, cache=scratch, control=control, allowed=self.allowed, context=self.context)
        first, _ = self._members(['pack-manifest.tsv', 'pack.json', LIB + 'catalog.json'])
        p = self._puller(scratch / 'meta', control)
        try:
            p.run(first)
        finally:
            p.close()
        try:
            self.manifest = contents.read_manifest((scratch / 'meta' / 'pack-manifest.tsv').read_text(encoding='utf-8'))
            records = json.loads((scratch / 'meta' / 'library-data' / 'catalog.json').read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise Refused(f'The pack on the download server could not be read ({type(e).__name__}).')
        try:
            was = json.loads((scratch / 'meta' / 'pack.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            was = {}
        return records, self.manifest, was

    def local(self, pack_path):
        return None

    def bring(self, items, target, control=None, told=None):
        members, missing = self._members([p for p, _, _ in items])
        last = [0]

        def progress(done, total, files, of):
            if told:
                told(done - last[0])
                last[0] = done
        p = self._puller(target, control, progress)
        try:
            p.run(members)
            # Asked of the files themselves, not of the list of what went wrong: whatever is not here whole and checked is missing
            missing += [m.name[len(TOP):] for m in members if not p.have(m)]
        finally:
            p.close()
        return missing

    def original(self, pack_path, scratch, control=None):
        members, missing = self._members([pack_path])
        if missing:
            return None, False
        p = self._puller(Path(scratch) / 'originals', control, workers=1)
        try:
            got = p.run(members)
            path = p.path(members[0])
        finally:
            p.close()
        return (path if not got['errors'] else None), True

    def search_index(self, scratch, control=None):
        path, _ = self.original(LIB + 'search.sqlite', scratch, control)
        return path


class Build:
    """One build. status() can be read from another thread at any time; control.stop('paused') or ('cancelled') ends it cleanly."""

    def __init__(self, target, source, choice, control=None, say=None, sources_folder=None):
        self.target = Path(target)
        self.source = source
        self.choice = dict(choice)
        self.control = control or download.Control()
        self.say = say or (lambda *a: None)
        self.sources_folder = sources_folder
        self.lib = self.target / 'library-data'
        self.work = self.target / STATE
        self.scratch = self.target.parent / '.lastbook' / 'build' if self.choice.get('portable') else self.target / '.build'
        self.lock = threading.Lock()
        self.state = {'phase': 'starting', 'done_bytes': 0, 'total_bytes': 0, 'videos_done': 0, 'videos_total': 0, 'message': '', 'problems': []}
        self.done_videos = {}        # record id -> {'bytes', 'sha256'} or {'kept': True}

    # ------------------------------------------------------------------------------------------------- status ----
    def status(self):
        with self.lock:
            return dict(self.state, problems=list(self.state['problems']))

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _problem(self, text):
        self.say('  problem:', text)
        with self.lock:
            if len(self.state['problems']) < 200:
                self.state['problems'].append(text)

    def _save_state(self):
        self.work.mkdir(parents=True, exist_ok=True)
        library.write_json(self.work / 'build.json', {'choice': self.choice, 'state': self.status(), 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})

    def _load_videos(self):
        try:
            with open(self.work / 'videos.jsonl', encoding='utf-8') as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        self.done_videos[d['id']] = d
                    except (ValueError, KeyError):
                        continue        # a line cut short by a stop: that video is done again
        except OSError:
            pass

    def _note_video(self, rid, result):
        self.done_videos[rid] = dict(result, id=rid)
        self.work.mkdir(parents=True, exist_ok=True)
        with open(self.work / 'videos.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps(self.done_videos[rid]) + '\n')

    # --------------------------------------------------------------------------------------------------- run ----
    def run(self):
        """Returns the final status. Raises download.Stopped when paused or cancelled, Refused or download.Failed when it cannot go on."""
        self.target.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        if self.choice.get('portable'):
            portable.mark(self.target)
        self._load_videos()
        self._set(phase='reading', message='Reading what the pack holds')
        records, manifest, was = self.source.meta(self.scratch, self.control)
        pack = contents.Pack(records, manifest)
        plan = pack.plan(self.choice)
        self.plan, self.pack_was = plan, was
        need = plan['bytes'] - sum(self._size(self.target / p) for p, _, _ in plan['copy'])
        free = library.free_bytes(self.target)
        if free < need + 2 * 2**30:
            raise Refused(f'This choice needs about {need / 1e9:.0f} GB more, and the disk has {free / 1e9:.0f} GB free.')
        self._set(total_bytes=plan['bytes'], videos_total=len(plan['shrink']))
        self._save_state()
        self._maps()
        self._files(plan)
        self._library(pack, plan, final=False)
        self._videos(pack, plan)
        self._finish(pack, plan)
        return self.status()

    @staticmethod
    def _size(p):
        try:
            return p.stat().st_size
        except OSError:
            return 0

    # -------------------------------------------------------------------------------------------------- maps ----
    def _maps(self):
        wanted = self.choice.get('maps') or []
        if not wanted:
            return
        from engine.mapbuild import build as mapbuild
        b = mapbuild.Builder(self.target, self.sources_folder, say=self.say, control=self.control, workers=int(self.choice.get('map_workers') or 2))
        try:
            for i, m in enumerate(wanted, 1):
                self.control.check()
                level = m.get('level') or 'detail'
                self._set(phase='maps', message=f'Map {i} of {len(wanted)}: {m["id"]}')
                if b.current(m['id'], level):
                    continue
                try:
                    b.build(m['id'], level=level, terrain=m.get('terrain', True))
                except (download.Stopped, KeyboardInterrupt):
                    raise
                except Exception as e:      # one region that fails does not cost the library
                    self._problem(f'The map of {m["id"]} could not be built ({getattr(e, "message", None) or type(e).__name__ + ": " + str(e)}).')
        finally:
            b.close()

    # ------------------------------------------------------------------------------------------------- files ----
    def _files(self, plan):
        self._set(phase='files', message='Bringing documents, pages and encyclopedias')
        counted = [0]

        def told(n):
            counted[0] += n
            with self.lock:
                self.state['done_bytes'] = counted[0]
        missing = self.source.bring(plan['copy'], self.target, self.control, told)
        self.missing = set(missing)
        for p in missing[:50]:
            self._problem(f'{p} could not be had from the pack.')
        if len(missing) > 50:
            self._problem(f'...and {len(missing) - 50} more files.')
        self._save_state()

    # ----------------------------------------------------------------------------------------------- library ----
    def _records(self, pack, plan):
        """The catalog as it stands now: what is kept, minus what could not be had, each video to be made smaller shown by its
        words alone until its smaller self is here."""
        waiting = {r['id']: profile for r, profile in plan['shrink']}
        out = []
        for r in plan['records']:
            if LIB + r['path'] in self.missing and not r.get('words_only'):
                continue
            rid = r['id']
            if rid in waiting:
                done = self.done_videos.get(rid)
                if done and done.get('kept'):       # making it smaller was not worth it: the original is here
                    out.append(r)
                elif done and self._size(self.lib / r['path']) == done.get('bytes'):
                    out.append(dict(r, bytes=done['bytes'], sha256=done['sha256'], made_smaller=waiting[rid]))
                else:
                    out.append(dict(r, words_only=True, pending=True))
            else:
                out.append(r)
        return out

    def _library(self, pack, plan, final):
        self._set(phase='library', message='Writing the catalog and the search index')
        records = self._records(pack, plan)
        search = self.source.search_index(self.scratch, self.control)
        if search is None:
            raise Refused('The pack’s search index could not be had.')
        if final or not (self.lib / 'search.sqlite').is_file():
            library.write_search(search, self.lib, {r['id'] for r in records}, self.control)
        library.write_catalog(self.lib, records)
        library.write_summary(self.lib, records)
        counts = dict(plan['counts'], videos_pending=sum(1 for r in records if r.get('pending')))
        library.write_pack(self.target, self.pack_was, {k: self.choice.get(k) for k in ('documents', 'encyclopedias', 'videos', 'encoder')}, counts,
                           [m['id'] for m in self.choice.get('maps') or []])
        self._save_state()
        return records

    # ------------------------------------------------------------------------------------------------ videos ----
    def _videos(self, pack, plan):
        todo = [(r, profile) for r, profile in plan['shrink'] if LIB + r['path'] not in self.missing]
        done = sum(1 for r, _ in todo if self._video_done(r))
        self._set(phase='videos', videos_done=done, message=f'Making videos smaller: {done} of {len(todo)}')
        if not todo:
            return
        if video.tools() is None:
            raise Refused('No ffmpeg that can write HEVC is on this computer, so videos cannot be made smaller. Choose "original" or "words only" for videos, or install ffmpeg.')
        encoder, threads = self.choice.get('encoder') or 'software', int(self.choice.get('threads') or video.THREADS)
        rewritten = refilled = time.time()
        for r, profile in sorted(todo, key=lambda x: x[0].get('bytes') or 0):       # the small ones first: the library fills in fastest
            self.control.check()
            if self._video_done(r):
                continue
            src, temporary = self.source.original(LIB + r['path'], self.scratch, self.control)
            if src is None:
                self._problem(f'{r["path"]} could not be had from the pack.')
                continue
            dest = self.lib / r['path']
            self._set(message=f'Making videos smaller: {done + 1} of {len(todo)} ({r.get("title", "")[:60]})')
            try:
                got = video.encode(src, dest, profile, encoder=encoder, threads=threads, control=self.control)
                if got['kept']:
                    if Path(src) != dest:
                        library.copy_file(src, dest, self.control)
                    self._note_video(r['id'], {'kept': True})
                else:
                    self._note_video(r['id'], {'bytes': got['bytes'], 'sha256': library.sha256_of(dest), 'seconds': got['seconds'], 'kbps': got['kbps']})
            except video.Failed as e:
                self._problem(f'{r["path"]}: {e} The original is kept.')
                try:
                    if Path(src) != dest:
                        library.copy_file(src, dest, self.control)
                    self._note_video(r['id'], {'kept': True})
                except OSError as e2:
                    self._problem(f'{r["path"]} could not be copied either ({type(e2).__name__}).')
            finally:
                if temporary:
                    for p in (Path(src), Path(src).with_name('._' + Path(src).name)):
                        try:
                            p.unlink()
                        except OSError:
                            pass
            done += 1
            with self.lock:
                self.state['videos_done'] = done
            if time.time() - rewritten > REWRITE_EVERY:
                if self.missing and time.time() - refilled > REFILL_EVERY:
                    # A pack folder that is itself still being filled: what was not there when this began may be there now
                    with self.lock:
                        self.state['problems'] = [p for p in self.state['problems'] if 'could not be had' not in p and 'more files' not in p]
                    self._files(plan)
                    self._set(phase='videos')
                    refilled = time.time()
                library.write_catalog(self.lib, self._records(pack, plan))
                library.write_summary(self.lib, self._records(pack, plan))
                self._save_state()
                rewritten = time.time()

    def _video_done(self, r):
        d = self.done_videos.get(r['id'])
        if not d:
            return False
        size = self._size(self.lib / r['path'])
        return size > 0 and (d.get('kept') or size == d.get('bytes'))

    # ------------------------------------------------------------------------------------------------ finish ----
    def _finish(self, pack, plan):
        records = self._library(pack, plan, final=True)
        self._set(phase='finishing', message='Writing the list of every file')
        entries = []
        by_path = {LIB + r['path']: r for r in records}
        for path, size, sha in plan['copy']:
            if path not in self.missing:
                entries.append((path, size, sha, 'copy', ''))
        for r, profile in plan['shrink']:
            d = self.done_videos.get(r['id'])
            if d and not d.get('kept') and LIB + r['path'] in by_path:
                entries.append((LIB + r['path'], d['bytes'], d['sha256'], 'encode', f'HEVC {video.PROFILES[profile]["height"]}p from {r.get("sha256") or "the original"}'))
            elif d and d.get('kept'):
                entries.append((LIB + r['path'], r.get('bytes') or 0, r.get('sha256'), 'copy', ''))
        for name in ('catalog.json', 'search.sqlite', 'summary.json'):
            p = self.lib / name
            entries.append((LIB + name, p.stat().st_size, library.sha256_of(p, self.control), 'made', 'for the parts this library holds'))
        library.write_manifest(self.target, entries)
        if not self.source.remote:
            pass
        shutil.rmtree(self.scratch, ignore_errors=True)
        for stray in (self.target / packzip.STATE, self.target / (packzip.STATE + '-journal')):
            try:
                stray.unlink()
            except OSError:
                pass
        pending = sum(1 for r in records if r.get('pending'))
        self._set(phase='done', message='The library is ready.' if not pending else f'The library is ready. {pending} videos could not be made smaller and are here by their words alone.')
        self._save_state()
