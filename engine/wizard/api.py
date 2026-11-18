"""/api/wizard/*: choosing what a library holds, where it goes, its maps and its AI, and building it.

The page is web/wizard.html. It asks, in this order:

  GET  state                         this computer, the drives plugged in, the packs to choose from, a build under way or left unfinished
  POST pick                          the system's folder chooser; then as `target`
  POST target   {path}               whether a library can be built there: the disk's format, room, what is there already
  POST menu     {source}             what a pack holds to choose from, with sizes (engine/wizard/contents.py)
  POST regions  {target}             the regions of the world a map can be built of, and those already built there
  POST ai       {target}             the AI models on this computer, the three offered by name, whether each fits
  POST inspect  {repo}               what a Hugging Face repository holds (engine/wizard/hf_import.py)
  POST plan     {source, choice, target}    what a choice comes to: bytes, downloads, hours, whether it fits
  POST start    {source, choice, target, effort, rate}
  GET  status                        the build: phase, bytes, videos, problems
  POST stop     {how}                'pause' or 'cancel'

A source is {'kind': 'pack', 'id': 'all'} (a published pack, read in place), or {'kind': 'folder', 'path': ...} (a pack or a
library already on a disk here). A choice is what engine/wizard/build.py takes.

The build itself runs in a process of its own (engine/wizard/worker.py): making a map keeps a processor busy for an hour,
and the app must stay quick meanwhile. It ends when the app does, and carries on when asked to.

Only requests from this computer get an answer, as with setup: a phone the library is shared with gets 403.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from engine import portable
from engine.setup import download, hardware
from engine.setup import manifest as manifests
from . import build, contents, drives, hf_import, library, video, worker

DATA_NAME = portable.DATA_NAME
MAX_BODY = 256 * 1024
SPARE = 2 * 2**30           # room left on the disk when the build is done
# How fast videos are made smaller, as multiples of their own length: measured on one computer (Apple M4, ten cores, six
# threads, other work running beside it). An older computer is slower; the page says these hours are a guess.
SPEED = {'software': {'howto': 5.5, 'talk': 11.8}, 'hardware': {'howto': 20.0, 'talk': 25.0}}
MEASURED_CORES = 10
EFFORTS = ('gentle', 'normal', 'full')
MODES = contents.MODES


class Refused(Exception):
    """A plain sentence for the person, and the HTTP status that goes with it."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _app_root():
    return Path(__file__).resolve().parents[2]


def _regions_catalog():
    if getattr(sys, 'frozen', False):
        base = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    else:
        base = _app_root()
    try:
        return json.loads((base / 'content' / 'regions' / 'catalog.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'regions': []}


class Wizard:
    def __init__(self, setup):
        """setup: the app's engine.setup.api.Setup, for what the two share: the settings folder, the manifest of published
        packs, the folder chooser, the open library."""
        self.setup = setup
        self.dir = Path(setup.settings_dir) / 'wizard'
        self.lock = threading.RLock()
        self.proc = None
        self.packs = {}         # source key -> contents.Pack
        self.seen = {}          # Hugging Face repository -> what inspect found
        self._regions = None
        self._tools = None

    # ---------------------------------------------------------------------------------------------- this computer ----
    def tools(self):
        """What this copy can do: make videos smaller (an ffmpeg with HEVC), build maps (osmium), run a model (llama-server)."""
        if self._tools is None:
            import importlib.util
            t = video.tools()
            try:
                from engine.ai import runtime
                runner = runtime.find(self.setup.current) is not None
            except Exception:
                runner = False
            self._tools = {'video': bool(t and 'libx265' in t['encoders']), 'video_hardware': bool(t and 'hevc_videotoolbox' in t['encoders']),
                           'maps': importlib.util.find_spec('osmium') is not None and importlib.util.find_spec('numpy') is not None,
                           'ai_runtime': runner}
        return self._tools

    def state(self):
        self.setup.manifest.fetch_once()
        hw = hardware.detect()
        current = self.setup.current
        sources = []
        for p in self.setup.manifest.current.get('packs', []):
            zips = [f for f in p['files'] if f.get('kind', 'zip') == 'zip']
            if p['available'] and len(zips) == 1:
                sources.append({'kind': 'pack', 'id': p['id'], 'title': p['title'], 'summary': p['summary'], 'records': p['records'],
                                'download_bytes': p['download_bytes'], 'installed_bytes': p['installed_bytes']})
        if current and (Path(current) / 'pack-manifest.tsv').is_file():
            sources.append({'kind': 'folder', 'path': str(current), 'title': 'The library open now', 'summary': 'Nothing is downloaded: its files are copied.'})
        return {'os': hardware.os_name(), 'cores': os.cpu_count(), 'ram_bytes': hw.get('ram_bytes'), 'chip': (hw.get('gpus') or [{}])[0].get('name'),
                'tools': self.tools(), 'efforts': {k: {'threads': worker.threads_for(k)} for k in EFFORTS},
                'current': {'path': str(current), 'portable': portable.is_portable(current)} if current else None,
                'default_target': self._default_target(), 'drives': drives.listed(),
                'sources': sources, 'manifest': self.setup.manifest.source, 'build': self.status()}

    def _default_target(self):
        """Where 'on this computer' means: the app's usual place for a library, or a new folder beside the open library when
        that place is taken by it."""
        t = Path(self.setup.default_parent)
        t = t if t.name == DATA_NAME else t / DATA_NAME
        current = self.setup.current
        try:
            taken = current is not None and t.resolve() == Path(current).resolve()
        except OSError:
            taken = False
        if taken:
            i = 2
            while (t.parent / f'Lastbook {i}').exists():
                i += 1
            t = t.parent / f'Lastbook {i}' / DATA_NAME
        return str(t)

    # ---------------------------------------------------------------------------------------------------- where ----
    def target(self, path):
        """The folder a library would be built in, and whether one can be. A folder that is not itself called LastbookData,
        and does not hold a library, gets a LastbookData inside it: a library never spreads its files over someone's folder."""
        text = str(path or '').strip()
        if not text:
            raise Refused('Choose a folder or a drive first.')
        p = Path(text).expanduser()
        if not p.is_absolute():
            raise Refused('Give the whole path of the folder.')
        if p.name != DATA_NAME and not portable.has_library(p):
            p = p / DATA_NAME
        at = p
        while not at.exists() and at.parent != at:
            at = at.parent
        fs = drives.filesystem(at)
        verdict = drives.judge(fs)
        try:
            free, total = hardware.disk_of(at)
        except OSError:
            free = total = None
        writable = os.access(at, os.W_OK)
        out = {'path': str(p), 'exists': p.is_dir(), 'library': portable.has_library(p), 'portable': portable.is_portable(p), 'filesystem': fs,
               'disk': hardware.disk_name(at), 'free_bytes': free, 'total_bytes': total, 'writable': writable, **verdict}
        if not writable:
            out.update(ok=False, note=f'Lastbook cannot save in “{at}”. Choose a folder you can save to.')
        try:        # what a build left there: the page offers to carry on with the same choices
            was = json.loads((p / build.STATE / 'build.json').read_text(encoding='utf-8'))
            out['built'] = {'choice': was.get('choice'), 'phase': (was.get('state') or {}).get('phase'), 'at': was.get('at')}
        except (OSError, ValueError):
            out['built'] = None
        current = self.setup.current
        out['is_current'] = bool(current and Path(current).resolve() == p.resolve())
        return out

    def pick(self):
        got = self.setup.pick()
        if not got.get('path'):
            return {'cancelled': True}
        return self.target(got['path'])

    # ----------------------------------------------------------------------------------------------- the pack ----
    def _source(self, d):
        """The page's source as the build takes it: {'kind': 'folder', 'path'} or {'kind': 'url', 'url'}."""
        if not isinstance(d, dict):
            raise Refused('Choose which library to build from.')
        if d.get('kind') == 'keep':     # a library that is there already, left as it is: only maps and an AI model are added
            return {'kind': 'keep', 'title': 'The library as it is'}
        if d.get('kind') == 'pack':
            m = self.setup.manifest
            p = m.pack(str(d.get('id')))
            if not (p and p['available']):      # the list of what is published is fetched once, when first asked for: wait for it
                m.fetch_once()
                if m.fetching is not None:
                    m.fetching.join(12)
                p = m.pack(str(d.get('id')))
            zips = [f for f in (p or {}).get('files', []) if f.get('kind', 'zip') == 'zip']
            if not p or not p['available'] or len(zips) != 1:
                raise Refused('That library is not published to build from.')
            return {'kind': 'url', 'url': zips[0]['url'], 'title': p['title']}
        if d.get('kind') == 'folder':
            p = Path(str(d.get('path') or '')).expanduser()
            if not (p / 'library-data' / 'catalog.json').is_file() and (p / DATA_NAME / 'library-data' / 'catalog.json').is_file():
                p = p / DATA_NAME
            if not (p / 'library-data' / 'catalog.json').is_file():
                raise Refused('That folder does not hold a Lastbook library.')
            if not (p / 'pack-manifest.tsv').is_file():
                raise Refused('That library has no list of its files (pack-manifest.tsv), so another cannot be built from it.')
            return {'kind': 'folder', 'path': str(p), 'title': p.parent.name if p.name == DATA_NAME else p.name}
        raise Refused('Choose which library to build from.')

    def _pack(self, source):
        src = self._source(source)
        if src['kind'] == 'keep':
            return None, src
        key = src.get('path') or src['url']
        with self.lock:
            pack = self.packs.get(key)
        if pack is not None:
            return pack, src
        try:
            if src['kind'] == 'folder':
                records, manifest, _ = build.LocalSource(src['path']).meta()
            else:
                scratch = self.dir / 'packs' / hashlib.sha256(key.encode()).hexdigest()[:16]
                remote = build.RemoteSource(src['url'], allowed=self.setup.allowed, context=self.setup.context)
                records, manifest, _ = remote.meta(scratch, download.Control())
        except build.Refused as e:
            raise Refused(str(e))
        except download.Failed as e:
            raise Refused(e.message, 502)
        pack = contents.Pack(records, manifest)
        with self.lock:
            self.packs[key] = pack
        return pack, src

    def menu(self, source):
        pack, src = self._pack(source)
        if pack is None:
            return {'documents': [], 'encyclopedias': [], 'videos': [], 'always_bytes': 0, 'title': src['title'], 'remote': False, 'keep': True,
                    'modes': list(MODES), 'can_shrink': False, 'can_shrink_hardware': False}
        return dict(pack.menu(), title=src['title'], remote=src['kind'] == 'url', modes=list(MODES),
                    can_shrink=self.tools()['video'], can_shrink_hardware=self.tools()['video_hardware'])

    # ---------------------------------------------------------------------------------------------------- maps ----
    def regions(self, target=None):
        if self._regions is None:
            self._regions = _regions_catalog()
        from engine.mapbuild import catalog
        built = []
        if target:
            root = Path(self.target(target)['path']) / 'derived' / 'geo' / 'regions'
            try:
                folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith('.'))
            except OSError:
                folders = []
            for f in folders:
                try:
                    info = json.loads((f / 'region.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                built.append({'id': info.get('geofabrik') or info.get('id') or f.name, 'folder': f.name, 'name': info.get('name'), 'level': info.get('level'),
                              'bytes': info.get('bytes'), 'data_as_of': (info.get('data_as_of') or '')[:10]})
        return {'regions': self._regions.get('regions', []), 'sizes_as_of': self._regions.get('sizes_as_of'), 'built': built,
                'ratios': catalog.BUILT, 'land_mb': catalog.LAND_MB, 'can_build': self.tools()['maps']}

    def _maps(self, wanted):
        """The choice's maps, checked against the catalog and given sizes. Returns (clean list, bytes, download bytes, minutes).
        The download includes the world's coastline, once, however many regions there are."""
        if self._regions is None:
            self._regions = _regions_catalog()
        from engine.mapbuild import catalog
        by_id = {r['id']: r for r in self._regions.get('regions', [])}
        clean, size, fetched, minutes = [], 0, 0, 0
        for m in wanted or []:
            if not isinstance(m, dict) or m.get('id') not in by_id:
                raise Refused('One of the maps chosen is not a region Lastbook knows.')
            r = by_id[m['id']]
            level = 'overview' if m.get('level') == 'overview' else 'detail'
            terrain = bool(m.get('terrain', True))
            clean.append({'id': r['id'], 'name': r['name'], 'level': level, 'terrain': terrain})
            est = catalog.estimate(r, level, terrain)
            if est:
                size, fetched, minutes = size + est['bytes'], fetched + est['download_bytes'], minutes + est['minutes']
        if clean:
            fetched += int(catalog.LAND_MB * 1e6)
        return clean, size, fetched, minutes

    # ------------------------------------------------------------------------------------------------------ AI ----
    def ai(self, target=None):
        from engine.ai import models
        hw = hardware.detect()
        here = []
        for m in models.on_computer():
            m['fit'] = models.judge(m, hw)
            here.append(models.public(m))
        here.sort(key=lambda m: (not m['fit']['fits'], -m['model_bytes']))
        there = []
        if target:
            t = Path(self.target(target)['path'])
            if t.is_dir():
                there = [models.public(dict(m, fit=models.judge(m, hw))) for m in models.on_drive(t)]
        return {'ram_bytes': hw.get('ram_bytes'), 'runtime': self.tools()['ai_runtime'], 'here': here, 'there': there,
                'presets': [{k: p[k] for k in ('id', 'repo', 'quant', 'title', 'about', 'draft')} for p in hf_import.PRESETS]}

    def inspect(self, repo, target=None):
        data = Path(self.target(target)['path']) if target else None
        try:
            found = hf_import.inspect(str(repo or '')[:300], data=data)
        except hf_import.Refused as e:
            raise Refused(str(e))
        except download.Failed as e:
            raise Refused(e.message, 502)
        with self.lock:
            self.seen[found['repo']] = found
        return found

    def _ai(self, want):
        """The choice's AI, checked. Returns (clean dict, bytes it adds to the library, bytes downloaded)."""
        if not isinstance(want, dict) or want.get('mode') in (None, 'none'):
            return {'mode': 'none'}, 0, 0
        if want.get('mode') == 'computer':
            from engine.ai import models
            m = next((m for m in models.on_computer() if m['id'] == want.get('id')), None)
            if m is None:
                raise Refused('The AI model chosen is not on this computer any more.')
            copy = want.get('copy') is not False
            return {'mode': 'computer', 'id': m['id'], 'name': m['name'], 'copy': copy}, m['bytes'] if copy else 0, 0
        if want.get('mode') == 'download':
            try:
                repo, _ = hf_import.parse(str(want.get('repo') or ''))
            except hf_import.Refused as e:
                raise Refused(str(e))
            found = self.seen.get(repo)
            if found is None:
                raise Refused('Look at the repository first, so Lastbook can say whether the model fits.')
            version = str(want.get('version') or found.get('recommended') or '')
            v = next((v for v in found['versions'] if v['name'] == version), None)
            if v is None:
                raise Refused('That version is not in the repository.')
            draft = bool(want.get('draft')) and bool(found.get('draft'))
            size = v['bytes'] + (found['projector']['bytes'] if found.get('projector') else 0) + (found['draft']['bytes'] if draft else 0)
            return {'mode': 'download', 'repo': repo, 'version': version, 'draft': draft, 'name': f'{found.get("name") or repo} {v.get("quant") or ""}'.strip()}, size, size
        raise Refused('Choose how the library gets its AI.')

    # --------------------------------------------------------------------------------------------------- plan ----
    def _choice(self, choice):
        """Only what a build understands, each part checked: nothing the page sent goes to the worker unread."""
        if not isinstance(choice, dict):
            raise Refused('Choose what the library holds.')
        out = {}
        for key in ('documents', 'encyclopedias'):
            v = choice.get(key)
            if v == 'all' or v is None:
                out[key] = 'all' if v == 'all' else []
            elif isinstance(v, list) and all(isinstance(x, str) and len(x) < 300 for x in v) and len(v) < 5000:
                out[key] = list(v)
            else:
                raise Refused('The choice of documents could not be read.')
        videos = choice.get('videos') or {'default': 'none'}
        if not isinstance(videos, dict) or not all(isinstance(k, str) and v in MODES for k, v in videos.items()):
            raise Refused('The choice of videos could not be read.')
        out['videos'] = dict(videos)
        out['encoder'] = 'hardware' if choice.get('encoder') == 'hardware' and self.tools()['video_hardware'] else 'software'
        out['portable'] = bool(choice.get('portable'))
        out['keep_sources'] = bool(choice.get('keep_sources'))
        return out

    def plan(self, source, choice, target=None, _full=False):
        pack, src = self._pack(source)
        clean = self._choice(choice)
        clean['maps'], map_bytes, map_fetch, map_minutes = self._maps(choice.get('maps'))
        clean['ai'], ai_bytes, ai_fetch = self._ai(choice.get('ai'))
        p = pack.plan(clean) if pack is not None else {'bytes': 0, 'copy': [], 'shrink': [], 'counts': {}, 'index_whole': 0}
        shrink = p['shrink']
        if shrink and not self.tools()['video']:
            raise Refused('No ffmpeg that can write HEVC is on this computer, so videos cannot be made smaller here. Keep them as they are, keep their words only, or leave them out.')
        speed = SPEED[clean['encoder']]
        scale = min(1.5, max(0.2, (os.cpu_count() or 4) / MEASURED_CORES)) if clean['encoder'] == 'software' else 1.0
        seconds = {k: sum(r.get('duration') or 0 for r, prof in shrink if prof == k) for k in ('howto', 'talk')}
        encode_hours = sum(seconds[k] / (speed[k] * scale) for k in seconds) / 3600
        originals = sum(r.get('bytes') or 0 for r, _ in shrink)
        remote = src['kind'] == 'url'
        # From a published pack: every file kept, every original of a video to be made smaller, and the pack's whole search
        # index, which the library's own is made from
        fetch = (sum(b for _, b, _ in p['copy']) + originals + p['index_whole'] if remote else 0) + map_fetch + ai_fetch
        # Room the build needs beyond what it leaves behind: what the maps are made from (the coastline unpacked as well),
        # the pack's search index, and one original video at a time
        from engine.mapbuild import catalog
        working = ((map_fetch + int(catalog.LAND_UNPACKED_MB * 1e6)) if clean['maps'] else 0) \
            + ((max((r.get('bytes') or 0 for r, _ in shrink), default=0) + p['index_whole']) if remote else 0)
        total = p['bytes'] + map_bytes + ai_bytes
        out = {'bytes': total, 'pack_bytes': p['bytes'], 'map_bytes': map_bytes, 'ai_bytes': ai_bytes, 'download_bytes': fetch, 'working_bytes': working,
               'counts': p['counts'], 'videos_to_shrink': len(shrink), 'video_hours': round(sum(seconds.values()) / 3600, 1),
               'encode_hours': round(encode_hours, 1), 'map_minutes': map_minutes, 'maps': len(clean['maps']), 'encoder': clean['encoder'],
               'coast_bytes': int(catalog.LAND_MB * 1e6) if clean['maps'] else 0,
               'free_bytes': None, 'fits': None, 'short_bytes': 0}
        if target:
            t = self.target(target)
            # A build carried on: what is there already is not needed again. A library kept as it is: none of it counts.
            have = _tree_bytes(Path(t['path'])) if t['exists'] and pack is not None else 0
            out.update(free_bytes=t['free_bytes'], already_bytes=have, target=t)
            if t['free_bytes'] is not None:
                need = max(0, total - have) + working + SPARE
                out.update(fits=t['free_bytes'] >= need, short_bytes=max(0, need - t['free_bytes']))
        return (out, clean, src) if _full else out

    # --------------------------------------------------------------------------------------------------- build ----
    def _read(self, name):
        try:
            return json.loads((self.dir / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _running(self):
        """Whether a build's process is alive: the one this run of the app started, or one a run before it started that the
        app has come back to (it restarts in place when a library is opened, and the build goes on through that)."""
        with self.lock:
            proc = self.proc
        if proc is not None:
            return proc.poll() is None
        st = self._read('status.json')
        return bool(st and st.get('state') == 'running' and time.time() - (st.get('at') or 0) < 15 and worker.alive(st.get('pid')))

    def status(self):
        """None when nothing was ever started. state: running, stopping, paused, cancelled, failed, done."""
        job = self._read('job.json')
        if job is None:
            return None
        st = self._read('status.json') or {'state': 'running' if self._running() else 'paused', 'phase': 'starting', 'message': '', 'problems': []}
        running = self._running()
        if running:
            st['state'] = 'stopping' if (self.dir / 'stop').is_file() else 'running'
        elif st.get('state') == 'running':
            with self.lock:
                proc = self.proc
            if proc is not None and proc.returncode not in (0, 1, None):       # it ended without saying how: the log has what it said last
                st.update(state='failed', error=f'The build stopped unexpectedly. What it said last is in {self.dir / "log.txt"}')
            else:       # the app that started it was closed, and the build stopped itself: it waits to be carried on
                st['state'] = 'paused'
        st['job'] = {'target': job['target'], 'source': job.get('asked'), 'choice': job['choice'], 'effort': job.get('effort'), 'rate': job.get('rate'),
                     'title': job.get('title'), 'started': job.get('started')}
        st['log'] = str(self.dir / 'log.txt')
        return st

    def start(self, source, choice, target, effort='normal', rate=None):
        with self.lock:
            if self._running():
                raise Refused('A build is under way. Pause it first.', 409)
            plan, clean, src = self.plan(source, choice, target, _full=True)
            t = plan['target']
            if not t['ok']:
                raise Refused(t['note'])
            if src['kind'] == 'folder' and Path(src['path']).resolve() == Path(t['path']).resolve():
                raise Refused('A library cannot be built from itself. Choose another folder to build in.')
            if src['kind'] == 'keep':
                if not t['library']:
                    raise Refused('There is no library in that folder to add to. Choose what the library holds first.')
                if not clean['maps'] and clean['ai']['mode'] == 'none':
                    raise Refused('Nothing is chosen to add: choose a map or an AI model.')
            elif t['is_current']:
                raise Refused('The library that is open cannot be rebuilt while it is open. Build in another folder, or keep this one as it is and add only maps or an AI model.')
            if plan['fits'] is False:
                raise Refused(f'This choice needs about {plan["short_bytes"] / 1e9:.0f} GB more than the disk has free.', 507)
            if clean['maps'] and not self.tools()['maps']:
                raise Refused('This copy of Lastbook cannot build maps. Take the maps out of the choice, or use a copy that can.')
            if effort not in EFFORTS:
                effort = 'normal'
            try:
                rate = float(rate) if rate not in (None, '', 0) else None
            except (TypeError, ValueError):
                rate = None
            if rate is not None and not 0.1 <= rate <= 10000:
                rate = None
            self.dir.mkdir(parents=True, exist_ok=True)
            for name in ('status.json', 'stop'):
                try:
                    (self.dir / name).unlink()
                except OSError:
                    pass
            job = {'target': t['path'], 'source': src, 'asked': source, 'choice': clean, 'effort': effort, 'rate': rate, 'parent': os.getpid(),
                   'title': src['title'], 'started': time.strftime('%Y-%m-%dT%H:%M:%S')}
            library.write_json(self.dir / 'job.json', job)
            self.proc = self._spawn()
        return self.status()

    def _spawn(self):
        frozen = getattr(sys, 'frozen', False)
        cmd = [sys.executable, '--wizard-worker', str(self.dir)] if frozen else [sys.executable, '-B', '-m', 'engine.wizard.worker', str(self.dir)]
        more = {'creationflags': 0x08000000 | 0x00004000} if os.name == 'nt' else {}      # no console window, below normal priority
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / 'worker-errors.txt', 'ab') as err:
            return subprocess.Popen(cmd, cwd=None if frozen else str(_app_root()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, **more)

    def resume(self):
        """Carries on the build that was paused, with the choices it had."""
        job = self._read('job.json')
        if job is None:
            raise Refused('There is no build to carry on.')
        return self.start(job.get('asked'), job['choice'], job['target'], job.get('effort') or 'normal', job.get('rate'))

    def stop(self, how='pause'):
        with self.lock:
            if not self._running():
                if how == 'cancel' and self._read('job.json') is not None:      # a paused build, given up: forgotten, its folder left as it is
                    for name in ('job.json', 'status.json', 'stop'):
                        try:
                            (self.dir / name).unlink()
                        except OSError:
                            pass
                return self.status() or {'state': None}
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / 'stop').write_text('cancelled' if how == 'cancel' else 'paused', encoding='utf-8')
        return self.status()

    def reopen(self):
        """The library being built is about to be opened, which starts the app again. Where that ends the build (Windows:
        a new process), it is carried on once the app is back (after_restart)."""
        job = self._read('job.json')
        if job is not None and self._running():
            library.write_json(self.dir / 'job.json', dict(job, reopen=True))
        return {'ok': True}

    def after_restart(self):
        """Called once when the app starts: a build that was running when the person opened its library is carried on."""
        job = self._read('job.json')
        if not job or not job.get('reopen'):
            return False
        library.write_json(self.dir / 'job.json', {k: v for k, v in job.items() if k != 'reopen'})
        time.sleep(6)       # long enough for a build that lost the app it belonged to to have stopped itself
        if self._running():
            return False
        try:
            self.resume()
        except Refused as e:
            print('wizard: the build was not carried on:', e, flush=True)
            return False
        return True


def _tree_bytes(folder, limit=400_000):
    """Bytes under a folder, without following links. Stops counting after `limit` files: by then the answer is 'a lot'."""
    total = n = 0
    stack = [str(folder)]
    while stack and n < limit:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.name.startswith('._'):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                        n += 1
        except OSError:
            continue
    return total


# ---------------------------------------------------------------------------------------------------------- HTTP ----
WIZARD = None

ROUTES = {
    ('GET', 'state'): lambda w, b: w.state(),
    ('GET', 'status'): lambda w, b: w.status() or {'state': None},
    ('POST', 'pick'): lambda w, b: w.pick(),
    ('POST', 'target'): lambda w, b: w.target(b.get('path')),
    ('POST', 'menu'): lambda w, b: w.menu(b.get('source')),
    ('POST', 'regions'): lambda w, b: w.regions(b.get('target')),
    ('POST', 'ai'): lambda w, b: w.ai(b.get('target')),
    ('POST', 'inspect'): lambda w, b: w.inspect(b.get('repo'), b.get('target')),
    ('POST', 'plan'): lambda w, b: w.plan(b.get('source'), b.get('choice'), b.get('target')),
    ('POST', 'start'): lambda w, b: w.start(b.get('source'), b.get('choice'), b.get('target'), b.get('effort') or 'normal', b.get('rate')),
    ('POST', 'resume'): lambda w, b: w.resume(),
    ('POST', 'reopen'): lambda w, b: w.reopen(),
    ('POST', 'stop'): lambda w, b: w.stop('cancel' if b.get('how') == 'cancel' else 'pause'),
}


def wizard():
    global WIZARD
    if WIZARD is None:
        from engine.setup import api as setup_api
        WIZARD = Wizard(setup_api.SETUP or setup_api._default())
    return WIZARD


def app_started():
    """For app/main.py, once setup is configured: carries on a build the person left running when they opened its library."""
    def go():
        try:
            wizard().after_restart()
        except Exception:
            traceback.print_exc()
    threading.Thread(target=go, daemon=True, name='wizard-carry-on').start()


def _send_json(h, obj, status=200):
    data = json.dumps(obj, separators=(',', ':')).encode()
    h.send_response(status)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Content-Length', str(len(data)))
    h.send_header('Cache-Control', 'no-store')
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(data)


def handle(h, method, path):
    """Answers one /api/wizard/ request on a BaseHTTPRequestHandler h."""
    if h.client_address[0] not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return _send_json(h, {'error': 'A library is built only on the computer Lastbook is open on.'}, 403)
    name = path[len('/api/wizard/'):].strip('/')
    fn = ROUTES.get(('GET' if method == 'HEAD' else method, name))
    if not fn:
        return _send_json(h, {'error': 'not found'}, 404)
    body = {}
    if method == 'POST':
        try:
            n = int(h.headers.get('Content-Length') or 0)
            if n < 0 or n > MAX_BODY:
                return _send_json(h, {'error': 'request too large'}, 413)
            body = json.loads(h.rfile.read(n) or b'{}') if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _send_json(h, {'error': 'bad request'}, 400)
    try:
        return _send_json(h, fn(wizard(), body))
    except Refused as e:
        return _send_json(h, {'error': str(e)}, e.status)
    except Exception as e:
        traceback.print_exc()
        return _send_json(h, {'error': f'The builder stopped on an internal error ({type(e).__name__}).'}, 500)
