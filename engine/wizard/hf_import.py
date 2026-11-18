"""Bringing an AI model from Hugging Face: the person names a repository, Lastbook shows what is in it and whether each
version would run on this computer, and brings the chosen one into the library's models/ folder.

  parse('HauhauCS/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced')   -> ('HauhauCS/Gemma4-...', quantisation asked for or None)
  inspect(repo)       what the repository holds: each version of the model with its size, the projector (for pictures)
                      and draft (for speed) files, the licence, and for each version whether it fits this computer
  Import(...).run()   brings one version, with its projector and draft, checking every file against its published SHA-256

Whether a version fits is judged before anything large is downloaded: the first 64 KB of one file give the model's
shape (engine/ai/gguf.py), and the repository's own file list gives every version's size. It is an estimate; llama.cpp
decides when the model loads.

All of it goes through engine/setup/download.py: huggingface.co for the lists, and Hugging Face's own download hosts
(*.hf.co) for the files. Nothing is sent but the repository's name. A repository that needs an account (gated or
private) cannot be brought: Lastbook holds no Hugging Face login and asks for none.
"""
import json
import re
import shutil
import threading
import time
import urllib.parse
from pathlib import Path

from engine.ai import gguf, models
from engine.setup import download, hardware

SITE = 'https://huggingface.co'
REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}')
# Which version to offer first, best first: four bits a weight is where size and quality balance; then the nearest to it.
PREFER = ('Q4_K_M', 'Q4_K_S', 'Q4_0', 'IQ4_XS', 'IQ4_NL', 'Q5_K_M', 'Q5_K_S', 'Q4_1', 'Q3_K_L', 'Q3_K_M', 'IQ3_M', 'Q6_K', 'Q3_K_S', 'IQ3_S', 'IQ3_XS', 'IQ3_XXS', 'Q8_0')
SPARE = 2 * 2**30           # room left on the drive after a model is brought

# Three models offered without the person having to know a repository's name: small, middle and large. Each is the
# publisher's or a well-known converter's own repository; inspect() reads it live, so sizes and fit are never out of date.
PRESETS = [
    {'id': 'compact', 'repo': 'Qwen/Qwen3-VL-4B-Instruct-GGUF', 'quant': 'Q4_K_M', 'title': 'Compact', 'draft': False,
     'about': 'Qwen3-VL 4B. About 3.3 GB. For a computer with 8 GB of memory or more. Reads page images as well as text.'},
    {'id': 'balanced', 'repo': 'HauhauCS/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced', 'quant': 'Q4_K_M', 'title': 'Balanced', 'draft': True,
     'about': 'Gemma 4 12B (a community retraining that declines less). About 7.8 GB with its speed-up file. For 16 GB of memory or more. Reads page images.'},
    {'id': 'workstation', 'repo': 'unsloth/gemma-4-26B-A4B-it-GGUF', 'quant': 'UD-Q4_K_M', 'title': 'Workstation', 'draft': False,
     'about': 'Gemma 4 26B, the model Lastbook was made and tested with. About 18 GB. For 24 GB of memory or more. Reads page images.'},
]


class Refused(Exception):
    """A plain sentence saying why a repository cannot be read or brought."""


def parse(text):
    """What a person pastes -> (owner/name, quantisation or None). Takes 'owner/name', 'owner/name:Q4_K_M', and the
    repository's web address in any of its forms. Raises Refused for anything else."""
    t = (text or '').strip()
    t = re.sub(r'^(https?://)?(www\.)?(huggingface\.co|hf\.co)/', '', t, flags=re.I)
    t = t.split('?')[0].split('#')[0]
    quant = None
    if ':' in t:
        t, quant = t.split(':', 1)
        quant = quant.strip().upper() or None
    parts = [p for p in t.strip('/').split('/') if p]
    if len(parts) >= 2 and parts[0] in ('models',):
        parts = parts[1:]
    repo = '/'.join(parts[:2])
    # After the name, only what a repository's own pages are called (…/tree/main, …/blob/main/file.gguf): anything else is not an address of one.
    odd = len(parts) > 2 and parts[2] not in ('tree', 'blob', 'resolve', 'discussions', 'commits')
    if odd or '..' in parts or not REPO.fullmatch(repo) or any(p.startswith('.') for p in parts[:2]):
        raise Refused('That is not the name of a Hugging Face repository. It looks like owner/name, for example Qwen/Qwen3-VL-4B-Instruct-GGUF.')
    return repo, quant


def _api(path):
    status, body = download.get_small(SITE + path, limit=16 << 20)
    if status in (401, 403, 404):       # Hugging Face answers 401 for a name that does not exist, as it does for one that is private
        raise Refused('Hugging Face has no repository by that name that is open to everyone. Check the spelling, capital letters included. '
                      'One that asks for an account cannot be brought: Lastbook holds no Hugging Face login.')
    if status != 200:
        raise Refused(f'Hugging Face did not answer as expected (HTTP {status}). Try again in a little while.')
    try:
        return json.loads(body)
    except ValueError:
        raise Refused('Hugging Face sent something Lastbook could not read.')


def _file_url(repo, revision, path):
    return f'{SITE}/{repo}/resolve/{revision}/' + urllib.parse.quote(path)


def inspect(repo_text, hw=None, data=None):
    """What a repository holds and how each version fits this computer.
    {'repo', 'revision', 'license', 'versions': [...], 'projectors': [...], 'drafts': [...], 'recommended': name or None, 'free_bytes'}"""
    repo, wanted = parse(repo_text)
    info = _api(f'/api/models/{repo}')
    if info.get('private') or info.get('gated'):
        raise Refused('That repository is not open to everyone (it asks for a Hugging Face account), so Lastbook cannot bring it.')
    revision = info.get('sha') or 'main'
    tree = _api(f'/api/models/{repo}/tree/{revision}?recursive=1')
    files = [f for f in tree if isinstance(f, dict) and f.get('type') == 'file' and str(f.get('path', '')).lower().endswith('.gguf')]
    if not files:
        raise Refused('That repository has no GGUF files. Lastbook runs models in GGUF form: look for a repository of the same model whose name ends in GGUF.')
    versions, projectors, drafts = {}, [], []
    for f in files:
        path = f['path']
        name = path.rsplit('/', 1)[-1]
        size = int((f.get('lfs') or {}).get('size') or f.get('size') or 0)
        one = {'path': path, 'bytes': size, 'sha256': (f.get('lfs') or {}).get('oid')}
        kind = gguf.kind_of({}, name)
        if kind == 'projector':
            projectors.append(dict(one, name=name, quant=gguf.quant_of({}, name)))
        elif kind == 'draft':
            drafts.append(dict(one, name=name))
        elif kind == 'model':
            part = gguf.shard(name)
            key = re.sub(r'-\d{5}-of-\d{5}\.gguf$', '.gguf', path, flags=re.I)
            v = versions.setdefault(key, {'name': key.rsplit('/', 1)[-1], 'quant': gguf.quant_of({}, key.rsplit('/', 1)[-1]), 'files': [], 'bytes': 0})
            v['files'].append(dict(one, part=part[0] if part else 1))
            v['bytes'] += size
    versions = [v for v in versions.values() if v['bytes'] >= models.SMALL]
    if not versions:
        raise Refused('That repository has GGUF files, but none is a model to talk to.')
    for v in versions:
        v['files'].sort(key=lambda f: f['part'])
    projector = _pick_projector(projectors)
    draft = drafts[0] if drafts else None
    extra = (projector['bytes'] if projector else 0) + (draft['bytes'] if draft else 0)
    # One look at one file's first 64 KB tells the shape of every version: they are the same model at different precision.
    meta = {}
    probe = min(versions, key=lambda v: v['bytes'])['files'][0]
    try:
        meta = gguf.read(download.read_range(_file_url(repo, revision, probe['path']), 0, gguf.HEAD - 1))['meta']
    except (download.Failed, gguf.NotGGUF):
        pass
    hw = hw or hardware.detect()
    for v in versions:
        stand_in = {'meta': meta, 'model_bytes': v['bytes'], 'extra_bytes': extra, 'bytes': v['bytes'] + extra}
        v['fit'] = models.judge(stand_in, hw)
        v['total_bytes'] = v['bytes'] + extra
        v['coarse'] = coarse(v['quant'])
    versions.sort(key=lambda v: v['bytes'])
    d = gguf.describe(meta, None, probe['path'].rsplit('/', 1)[-1])
    free = None
    try:
        free = shutil.disk_usage(_existing(models.folder(data))).free
    except OSError:
        pass
    card = info.get('cardData') or {}
    return {'repo': repo, 'revision': revision, 'license': card.get('license') or d.get('license') or next((t[8:] for t in info.get('tags') or [] if t.startswith('license:')), None),
            'name': d.get('name') or repo.split('/')[1], 'arch': d.get('arch'), 'size_label': d.get('size_label'), 'tools': d.get('tools'),
            'versions': versions, 'projector': projector, 'draft': draft, 'vision': projector is not None,
            'recommended': recommend(versions, wanted), 'asked_for': wanted, 'free_bytes': free, 'spare_bytes': SPARE,
            'judged': bool(meta)}


def _existing(p):
    p = Path(p)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _pick_projector(projectors):
    """One projector when a repository offers several precisions: 16-bit, which every llama.cpp reads and costs little."""
    if not projectors:
        return None
    for want in ('F16', 'BF16', 'Q8_0', 'F32'):
        for p in projectors:
            if p.get('quant') == want:
                return p
    return min(projectors, key=lambda p: p['bytes'])


def _plain(quant):
    """A quantisation's family name: Unsloth's 'UD-Q4_K_XL' is, for choosing, a Q4_K."""
    return re.sub(r'_XL$', '_M', (quant or '').upper().replace('UD-', ''))


def coarse(quant):
    """Two bits a weight or fewer: it runs, and its answers are plainly worse. Said beside such a version, never chosen first."""
    return bool(re.match(r'I?Q[12](_|$)', _plain(quant)))


def recommend(versions, wanted=None):
    """The version to offer first: the one asked for if it can run; else the usual good one (Q4_K_M) or the nearest to it
    that fits; else the largest that fits. None when nothing fits, or only versions too coarse to recommend."""
    if wanted:
        for v in versions:
            if (v['quant'] or '').upper() == wanted and v['fit'].get('fits') is not False:
                return v['name']
    fits = [v for v in versions if v['fit'].get('fits') and not coarse(v['quant'])]
    if not fits:
        return None
    rank = {q: i for i, q in enumerate(PREFER)}
    known = [v for v in fits if _plain(v['quant']) in rank]
    if known:
        return min(known, key=lambda v: (rank[_plain(v['quant'])], (v['quant'] or '').startswith('UD-')))['name']
    return max(fits, key=lambda v: v['bytes'])['name']


def folder_for(repo):
    """Where a repository's files go in the library: models/<owner>--<name>/. The name is checked again: it becomes a path."""
    if not REPO.fullmatch(repo):
        raise Refused('That is not the name of a Hugging Face repository.')
    return repo.replace('/', '--')


class Import:
    """Brings one version of a model. progress(state) is called with {'state', 'file', 'done_bytes', 'total_bytes', 'message'}."""

    def __init__(self, found, version_name, data=None, control=None, progress=None, with_projector=True, with_draft=True):
        self.found = found
        self.version = next((v for v in found['versions'] if v['name'] == version_name), None)
        if self.version is None:
            raise Refused('That version is not in the repository.')
        self.dest = models.folder(data) / folder_for(found['repo'])
        self.control = control or download.Control()
        self.progress = progress or (lambda s: None)
        self.parts = list(self.version['files'])
        if with_projector and found.get('projector'):
            self.parts.append(found['projector'])
        if with_draft and found.get('draft'):
            self.parts.append(found['draft'])
        self.total = sum(p['bytes'] for p in self.parts)

    def run(self):
        """Returns the folder. Raises Refused (no room), download.Failed, download.Stopped."""
        repo, revision = self.found['repo'], self.found['revision']
        have = sum(_size(self.dest / p['path'].rsplit('/', 1)[-1]) for p in self.parts)
        try:
            free = shutil.disk_usage(_existing(self.dest)).free
        except OSError:
            free = None
        if free is not None and free < self.total - have + SPARE:
            raise Refused(f'The library’s drive has {free / 1e9:.1f} GB free, and this model needs {(self.total - have) / 1e9:.1f} GB with some to spare.')
        self.dest.mkdir(parents=True, exist_ok=True)
        try:        # what an earlier import of this repository brought and checked: not brought again
            kept = {f['name']: (f.get('bytes'), f.get('sha256')) for f in json.loads((self.dest / models.NOTE).read_text(encoding='utf-8')).get('files', [])}
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            kept = {}
        done = 0
        for p in self.parts:
            name = p['path'].rsplit('/', 1)[-1]
            if '/' in name or '\\' in name or name.startswith('.') or not name.lower().endswith('.gguf'):
                raise Refused('The repository lists a file with a name Lastbook will not write.')
            before = done
            if not (p.get('sha256') and kept.get(name) == (p['bytes'], p['sha256']) and _size(self.dest / name) == p['bytes']):
                def tell(got, total, phase, name=name, before=before):
                    self.progress({'state': 'downloading', 'file': name, 'done_bytes': before + got, 'total_bytes': self.total})
                download.Fetch(_file_url(repo, revision, p['path']), self.dest / name, control=self.control, progress=tell, sha256=p.get('sha256')).run()
                try:    # the downloader's own note names the address the file came from, signed for an hour: model.json keeps what matters
                    (self.dest / (name + '.fetched.json')).unlink()
                except OSError:
                    pass
            done += p['bytes']
            kept[name] = (p['bytes'], p.get('sha256'))
        note = {'repo': repo, 'revision': revision, 'license': self.found.get('license'), 'added': time.strftime('%Y-%m-%d'), 'source': f'{SITE}/{repo}',
                'files': [{'name': n, 'bytes': b, 'sha256': s} for n, (b, s) in sorted(kept.items()) if (self.dest / n).is_file()]}
        (self.dest / models.NOTE).write_text(json.dumps(note, indent=1), encoding='utf-8')
        self.progress({'state': 'done', 'done_bytes': self.total, 'total_bytes': self.total})
        return self.dest


def _size(p):
    try:
        return p.stat().st_size
    except OSError:
        return 0


class Jobs:
    """One import at a time, in a thread of its own; the page asks how it is going."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {'state': 'idle'}
        self.control = None
        self.thread = None

    def status(self):
        with self.lock:
            return dict(self.state)

    def start(self, found, version_name, data=None, on_done=None):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise Refused('A model is already being brought. Wait for it, or stop it first.')
            imp = Import(found, version_name, data=data, progress=self._tell)
            self.control = imp.control
            self.state = {'state': 'downloading', 'repo': found['repo'], 'version': version_name, 'done_bytes': 0, 'total_bytes': imp.total, 'started': time.time()}
            self.thread = threading.Thread(target=self._run, args=(imp, on_done), daemon=True, name='ai-import')
            self.thread.start()
            return dict(self.state)

    def task(self, what, title, work, on_done=None):
        """Any other long piece of work shown the same way (copying a model onto the drive): work(tell, cancelled) -> result.
        A failure that carries a plain sentence (an exception whose first argument is one) is shown as it is."""
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise Refused('Something is already being brought or copied. Wait for it, or stop it first.')
            self.control = download.Control()
            control = self.control
            self.state = {'state': what, 'repo': title, 'done_bytes': 0, 'total_bytes': 0, 'started': time.time()}

            def run():
                try:
                    result = work(self._tell, lambda: control.ev.is_set())
                    with self.lock:
                        self.state.update(state='done')
                    if on_done:
                        on_done(result)
                except Exception as e:
                    stopped = control.ev.is_set()
                    sentence = type(e).__name__ == 'Refused'        # this module's or engine/ai/carry.py's: its words are for the person
                    if not sentence and not stopped:
                        print('AI: a copy failed:', type(e).__name__, e, flush=True)
                    with self.lock:
                        self.state.update(state='stopped' if stopped else 'failed',
                                          message='Stopped. What was copied so far is kept.' if stopped else (str(e) if sentence else f'It could not be copied ({type(e).__name__}).'))
            self.thread = threading.Thread(target=run, daemon=True, name='ai-copy')
            self.thread.start()
            return dict(self.state)

    def _tell(self, s):
        with self.lock:
            self.state.update(s)

    def _run(self, imp, on_done):
        try:
            folder = imp.run()
            with self.lock:
                self.state.update(state='done', folder=folder.name)
            if on_done:
                on_done(folder)
        except download.Stopped:
            with self.lock:
                self.state.update(state='stopped', message='Stopped. What has arrived is kept: bring it again to carry on from there.')
        except (download.Failed, Refused) as e:
            with self.lock:
                self.state.update(state='failed', message=getattr(e, 'message', None) or str(e))
        except Exception as e:
            print('AI import: failed:', type(e).__name__, e, flush=True)
            with self.lock:
                self.state.update(state='failed', message=f'Lastbook could not bring the model ({type(e).__name__}).')

    def stop(self):
        with self.lock:
            if self.control is not None:
                self.control.stop('cancelled')


def remove(model_id, data=None):
    """Takes a model Lastbook brought out of the library: its whole folder under models/. Only ever inside models/."""
    if not model_id.startswith('drive:'):
        raise Refused('Only a model in this library can be removed here. One another program holds is that program’s to remove.')
    root = models.folder(data).resolve()
    target = (root / model_id[6:]).resolve()
    if root not in target.parents or not target.is_file():
        raise Refused('That model is not in this library.')
    folder = target.parent
    if folder == root:
        target.unlink()
    else:
        shutil.rmtree(folder)
    return True
