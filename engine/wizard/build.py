"""Building a library of chosen parts, on the person's own computer, from a published pack and open map data.

  Build(target, source, choice).run()

target   the LastbookData folder to make (on any disk; a drive that travels is marked so with choice['portable'])
source   where the pack's files come from:
           LocalSource(folder)   a copy of the pack already on a disk here
           RemoteSource(url)     the published pack, read in place: only the chosen files are downloaded
           None                  the library in target is kept as it is: only maps and an AI model are added
choice   {'documents': [...], 'encyclopedias': [...], 'videos': {...}      as engine/wizard/contents.py
          'maps': [{'id': 'great-britain', 'level': 'detail', 'terrain': True}, ...],
          'ai': {'mode': 'none'} | {'mode': 'computer', 'id': ..., 'copy': True} | {'mode': 'download', 'repo': ..., 'version': ...},
          'encoder': 'software' or 'hardware', 'threads': 6, 'video_jobs': 1, 'portable': True}

What it does, in this order, each step picking up where a run that stopped left off:
  1. maps      each region not already built (engine/mapbuild); what they were made from is then deleted
  2. files     everything brought as it is: documents, pages, transcripts, encyclopedias, videos that are small already
  3. library   catalog, search index and counts are written: from here the library opens in the app, with every video
               still to be made smaller shown by its words alone
  4. AI        the model chosen: copied from another program on this computer, or brought from Hugging Face
  5. videos    one at a time, each original read, re-encoded and (from a remote pack) deleted; the catalog is rewritten
               as they finish, so the library fills in while it is being used
  6. finish    the manifest of every file, with sizes and checksums

Nothing here opens a connection itself: RemoteSource goes through engine/wizard/packzip.py and engine/setup/download.py.
Polite by default: one video at a time at low priority on a limited number of threads, and whatever rate limit the caller
set on download.THROTTLE.
"""
import hashlib
import json
import os
import queue
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

    def original(self, pack_path, scratch, control=None, told=None):
        """(path of the file to read, whether it is a temporary copy to delete after)."""
        return self.local(pack_path), False

    def search_index(self, scratch, control=None, told=None):
        return self.local(LIB + 'search.sqlite')


class RemoteSource:
    """A published pack, read in place over the network. Only what is asked for is downloaded."""
    remote = True

    # Four connections. With ten, the download server gave 13 MB a second for three hours and then answered most requests
    # 503, the ten together getting 0.4 MB a second; started again with two, it gave 6.5 at once (18 Nov 2026).
    def __init__(self, url, workers=4, allowed=None, context=None):
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

    def original(self, pack_path, scratch, control=None, told=None):
        members, missing = self._members([pack_path])
        if missing:
            return None, False
        p = self._puller(Path(scratch) / 'originals', control, workers=1, progress=(lambda done, total, files, of: told(done, total)) if told else None)
        try:
            got = p.run(members)
            path = p.path(members[0])
        finally:
            p.close()
        return (path if not got['errors'] else None), True

    def search_index(self, scratch, control=None, told=None):
        path, _ = self.original(LIB + 'search.sqlite', scratch, control, told)
        return path


class _Either:
    """The build's own control, and a second reason to stop: a video being made beside this one has failed."""

    def __init__(self, control, abort):
        self.control, self.abort, self.ev = control, abort, control.ev

    @property
    def why(self):
        return self.control.why

    def stop(self, why):
        self.control.stop(why)

    def check(self):
        self.control.check()
        if self.abort.is_set():
            raise download.Stopped('paused')

    def sleep(self, s):
        end = time.time() + s
        while True:
            self.check()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.5, left))


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
        self.notes = threading.Lock()
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
        with self.notes:        # one line at a time, whichever job it is from
            self.done_videos[rid] = dict(result, id=rid)
            self.work.mkdir(parents=True, exist_ok=True)
            with open(self.work / 'videos.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(self.done_videos[rid]) + '\n')

    # --------------------------------------------------------------------------------------------------- run ----
    def run(self):
        """Returns the final status. Raises download.Stopped when paused or cancelled, Refused or download.Failed when it cannot go on."""
        self.target.mkdir(parents=True, exist_ok=True)
        if self.source is None:         # a library that is here already, kept as it is: maps and an AI model are added, nothing else is touched
            if not (self.lib / 'catalog.json').is_file():
                raise Refused('There is no library in that folder to add to.')
            self._maps()
            self._ai()
            self._set(phase='done', message='Added to the library.')
            return self.status()
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
        self._set(total_bytes=sum(b for _, b, _ in plan['copy']), videos_total=len(plan['shrink']))     # the bytes brought as they are: what the files step counts up to
        self._save_state()
        self._maps()
        self._files(plan)
        self._library(pack, plan, final=False)
        self._ai()
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
        self._set(phase='maps', maps_done=0, maps_total=len(wanted), message='Getting ready to build the maps')
        try:
            from engine.mapbuild import build as mapbuild
        except ImportError as e:        # no osmium in this copy: the library is still built, without its maps
            return self._problem(f'This copy of Lastbook cannot build maps ({e.name or "a part"} is missing). The rest of the library is built without them.')
        # What the maps are made from is kept beside the library being built, on its drive: never in the home folder
        named = self.sources_folder or os.environ.get('LASTBOOK_MAP_SOURCES')       # a folder someone keeps for this: used, and left alone
        own = not named
        folder = Path(named) if named else self.target.parent / '.lastbook' / 'map-sources'
        b = mapbuild.Builder(self.target, folder, say=self.say, control=self.control, workers=int(self.choice.get('map_workers') or 2))
        failed = 0
        try:
            for i, m in enumerate(wanted, 1):
                self.control.check()
                level = m.get('level') or 'detail'
                self._set(message=f'Map {i} of {len(wanted)}: {m.get("name") or m["id"]}')
                if not b.current(m['id'], level):
                    try:
                        b.build(m['id'], level=level, terrain=m.get('terrain', True))
                    except (download.Stopped, KeyboardInterrupt):
                        raise
                    except Exception as e:      # one region that fails does not cost the library
                        failed += 1
                        self._problem(f'The map of {m.get("name") or m["id"]} could not be built ({getattr(e, "message", None) or type(e).__name__ + ": " + str(e)}).')
                self._set(maps_done=i)
        finally:
            b.close()
        if own and not failed and not self.choice.get('keep_sources'):
            # Several times the size of the maps made from them, and of no use to the library once it is built
            shutil.rmtree(folder, ignore_errors=True)

    # ---------------------------------------------------------------------------------------------------- AI ----
    def _ai(self):
        """The model the person chose, into the library: {'mode': 'none'}, {'mode': 'computer', 'id', 'copy': bool} for one
        another program on this computer already holds, or {'mode': 'download', 'repo', 'version', 'draft': bool} from Hugging Face."""
        want = self.choice.get('ai') or {}
        mode = want.get('mode') or 'none'
        if mode not in ('computer', 'download'):
            return
        from engine.ai import carry, models, picker
        from . import hf_import
        self._set(phase='ai', message='Putting the AI model in the library', ai_done_bytes=0, ai_total_bytes=0)

        def tell(s):
            self._set(ai_done_bytes=s.get('done_bytes') or 0, ai_total_bytes=s.get('total_bytes') or 0)
        chosen = None
        try:
            if mode == 'computer':
                m = next((m for m in models.on_computer() if m['id'] == want.get('id')), None)
                if m is None:
                    return self._problem('The AI model chosen is not on this computer any more. Choose one in the app: Chat, then Model.')
                chosen = m['id']
                if want.get('copy', True):
                    chosen = carry.model(m, data=self.target, progress=tell, cancelled=lambda: self.control.ev.is_set())
            else:
                found = hf_import.inspect(str(want.get('repo') or ''), data=self.target)
                version = want.get('version') or found.get('recommended')
                if not want.get('draft'):
                    found = dict(found, draft=None)
                folder = hf_import.Import(found, version, data=self.target, control=self.control, progress=tell).run()
                there = [m for m in models.on_drive(self.target) if Path(m['path']).parent == folder]
                chosen = next((m['id'] for m in there if m['file'] == version), there[0]['id'] if len(there) == 1 else None)
            if self.choice.get('portable') and chosen and chosen.startswith('drive:'):
                try:        # the program that runs models, beside them: the drive then answers on a computer with nothing installed
                    carry.runtime(self.target)
                except (carry.Refused, OSError) as e:
                    if 'already' not in str(e):
                        self._problem(f'The program that runs AI models could not be copied to the drive ({e}). On another computer the model will need one installed.')
            if chosen:
                picker.Picker(self.target).select(chosen)
        except (carry.Refused, hf_import.Refused) as e:
            self.control.check()        # stopped by the person: that is not a problem with the model
            self._problem(f'The AI model could not be put in the library: {e}')
        except download.Failed as e:
            self._problem(f'The AI model could not be downloaded: {e.message}')
        except (KeyError, ValueError, OSError) as e:
            self._problem(f'The AI model could not be put in the library ({type(e).__name__}).')

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
        self._set(phase='finishing' if final else 'library', message='Writing the catalog and the search index', index_done_bytes=0, index_total_bytes=0)
        records = self._records(pack, plan)
        # The search index holds the words of exactly the records kept. It is written again only when that set has changed:
        # at the end of most builds it is the one written when the documents came in.
        ids = {r['id'] for r in records}
        mark = hashlib.sha256('\n'.join(sorted(ids)).encode()).hexdigest()
        try:
            same = (self.lib / 'search.sqlite').is_file() and (self.work / 'search.mark').read_text(encoding='utf-8') == mark
        except OSError:
            same = False
        if not same:
            if self.source.remote:
                self._set(message='Bringing the pack’s search index')
            search = self.source.search_index(self.scratch, self.control, lambda done, total: self._set(index_done_bytes=done, index_total_bytes=total))
            if search is None:
                raise Refused('The pack’s search index could not be had.')
            self._set(message='Writing the catalog and the search index', index_done_bytes=0, index_total_bytes=0)
            library.write_search(search, self.lib, ids, self.control)
            self.work.mkdir(parents=True, exist_ok=True)
            (self.work / 'search.mark').write_text(mark, encoding='utf-8')
        library.write_catalog(self.lib, records)
        library.write_summary(self.lib, records)
        counts = dict(plan['counts'], videos_pending=sum(1 for r in records if r.get('pending')))
        # The maps a library says it has are the ones that are there: one that was asked for and could not be built is not
        # among them, and one built before this choice was made is.
        library.write_pack(self.target, self.pack_was, {k: self.choice.get(k) for k in ('documents', 'encyclopedias', 'videos', 'encoder')}, counts,
                           library.regions_here(self.target))
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
        # Several videos at once, each on its share of the threads: at 480p the encoder cannot keep many threads busy on
        # one picture, so on a computer with cores to spare two videos side by side get through more than one alone.
        jobs = max(1, min(4, int(self.choice.get('video_jobs') or 1)))
        each = max(1, threads // jobs)
        waiting = [(r, profile) for r, profile in sorted(todo, key=lambda x: x[0].get('bytes') or 0)      # the small ones first: the library fills in fastest
                   if not self._video_done(r)]
        abort = threading.Event()
        control = self.control if jobs == 1 else _Either(self.control, abort)
        slots = queue.SimpleQueue()
        for i in range(jobs):
            slots.put(i)
        count = [done]
        times = {'rewritten': time.time(), 'refilled': time.time()}

        def one(r, profile):
            control.check()
            slot = slots.get()
            try:
                # Each job keeps the original it is reading in a place of its own
                src, temporary = self.source.original(LIB + r['path'], self.scratch if jobs == 1 else self.scratch / f'job-{slot}', control)
                if src is None:
                    self._problem(f'{r["path"]} could not be had from the pack.')
                    return
                dest = self.lib / r['path']
                with self.lock:
                    self.state['message'] = f'Making videos smaller: {count[0] + 1} of {len(todo)} ({r.get("title", "")[:60]})'
                try:
                    got = video.encode(src, dest, profile, encoder=encoder, threads=each, control=control)
                    if got['kept']:
                        if Path(src) != dest:
                            library.copy_file(src, dest, control)
                        self._note_video(r['id'], {'kept': True})
                    else:
                        self._note_video(r['id'], {'bytes': got['bytes'], 'sha256': library.sha256_of(dest), 'seconds': got['seconds'], 'kbps': got['kbps']})
                except video.Failed as e:
                    self._problem(f'{r["path"]}: {e} The original is kept.')
                    try:
                        if Path(src) != dest:
                            library.copy_file(src, dest, control)
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
            finally:
                slots.put(slot)
            with self.lock:
                count[0] += 1
                self.state['videos_done'] = count[0]

        def keep_up():
            """Every few minutes the catalog is written again, so the library that is open fills in as videos are done."""
            if time.time() - times['rewritten'] <= REWRITE_EVERY:
                return
            if self.missing and time.time() - times['refilled'] > REFILL_EVERY:
                # A pack folder that is itself still being filled: what was not there when this began may be there now
                with self.lock:
                    self.state['problems'] = [p for p in self.state['problems'] if 'could not be had' not in p and 'more files' not in p]
                self._files(plan)
                self._set(phase='videos')
                times['refilled'] = time.time()
            library.write_catalog(self.lib, self._records(pack, plan))
            library.write_summary(self.lib, self._records(pack, plan))
            self._save_state()
            times['rewritten'] = time.time()

        if jobs == 1:
            for r, profile in waiting:
                one(r, profile)
                keep_up()
            return
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix='video') as pool:
            running, rest = set(), iter(waiting)
            try:
                while True:
                    while len(running) < jobs:
                        nxt = next(rest, None)
                        if nxt is None:
                            break
                        running.add(pool.submit(one, *nxt))
                    if not running:
                        break
                    finished, running = wait(running, timeout=5, return_when=FIRST_COMPLETED)
                    for f in finished:
                        f.result()          # whatever stopped that job stops the build
                    keep_up()
            finally:
                abort.set()                 # the jobs still running stop at their next look, and leave nothing half-written

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
