"""The AI models here: those in the library's own models/ folder, which travel with it, and those other programs on this
computer already hold, which are used where they are and never copied or changed.

  on_drive(data)      models under <library>/models/
  on_computer()       AnythingLLM, LM Studio, Ollama, the Hugging Face cache, and LASTBOOK_MODEL_DIR
  found(data)         both, each judged against this computer's memory

A model is a .gguf file. Beside it there may be a projector (a file with 'mmproj' in its name: it lets the model look at
pictures) and a draft model ('mtp-...': it lets a large model write faster). A model split into numbered parts is one model.

Each is described by a dict:
  id          stable for this file on this computer ('drive:' + its path inside models/, or 'here:' + a digest of its path)
  name        what its header calls it, with its size and quantisation
  path, projector, draft
  bytes       all its files together
  where       'drive' (travels with the library) or 'computer' (only on this computer)
  origin      'This library', 'AnythingLLM', 'LM Studio', 'Ollama', 'Hugging Face cache', ...
  vision      has a projector
  tools       its chat template speaks of tools (None: not known)
  fit         {'fits', 'context', 'needs_bytes', 'ram_bytes', 'why'} on this computer
  source      where it was brought from, when Lastbook brought it (models/<folder>/model.json)
"""
import hashlib
import json
import os
import sys
from pathlib import Path

from engine import paths
from engine.setup import hardware
from . import gguf

MODELS = 'models'
NOTE = 'model.json'
DEPTH = 7               # folders deep a search goes: the Hugging Face cache nests five
LIMIT = 400             # files looked at in one place
SMALL = 50 * 2**20      # a .gguf smaller than this is not a model to talk to (vocabularies, test files)


def folder(data=None):
    return Path(data or paths.DATA) / MODELS


def _gb(n):
    """Memory as a computer's own label gives it: a "16 GB" Mac has 16 x 2^30 bytes, and what a model needs is said in the same measure."""
    g = n / 2**30
    return f'{g:.0f} GB' if g >= 10 or abs(g - round(g)) < 0.05 else f'{g:.1f} GB'


def _walk(root, depth=DEPTH, limit=LIMIT):
    """Every .gguf under root, not following links out of it, not into hidden folders."""
    out, stack = [], [(Path(root), 0)]
    while stack and len(out) < limit:
        here, d = stack.pop()
        try:
            entries = sorted(os.scandir(here), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.name.startswith('.') and e.name not in ('.cache',):
                continue
            try:
                if e.is_dir():           # follows links: the Hugging Face cache keeps its files behind them
                    if d < depth:
                        stack.append((Path(e.path), d + 1))
                elif e.name.lower().endswith('.gguf') and e.is_file():
                    out.append(Path(e.path))
            except OSError:
                continue
    return out


def _header(path):
    try:
        with open(path, 'rb') as f:
            return gguf.read(f.read(gguf.HEAD))['meta']
    except (OSError, gguf.NotGGUF):
        return None


def _group(files):
    """Files of one folder -> [(model path, projector or None, draft or None, total bytes, header)]."""
    seen = {}
    for p in files:
        meta = _header(p)
        if meta is None:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        seen[p] = (gguf.kind_of(meta, p.name), meta, size)
    projectors = [p for p, (k, _, _) in seen.items() if k == 'projector']
    drafts = [p for p, (k, _, _) in seen.items() if k == 'draft']
    out = []
    for p, (kind, meta, size) in sorted(seen.items()):
        if kind != 'model':
            continue
        part = gguf.shard(p.name)
        if part:
            if part[0] != 1:
                continue        # the first part stands for the whole
            stem = gguf.SHARD.sub('', p.name)
            size = sum(s for q, (_, _, s) in seen.items() if gguf.shard(q.name) and gguf.SHARD.sub('', q.name) == stem)
        if size < SMALL:
            continue
        out.append((p, _closest(p, projectors), _closest(p, drafts), size, meta))
    return out


def _closest(model, others):
    """The projector (or draft) that goes with a model: the one whose name shares most with it, when a folder holds several."""
    if not others:
        return None
    words = set(_words(model.name))
    return max(others, key=lambda o: (len(words & set(_words(o.name))), -len(o.name)))


def _words(name):
    return [w for w in name.lower().replace('.gguf', '').replace('_', '-').replace('.', '-').split('-') if w and w not in ('mmproj', 'mtp', 'f16', 'bf16', 'f32')]


def _describe(model, projector, draft, size, meta, where, origin, key):
    d = gguf.describe(meta, size, model.name)
    extra = sum(p.stat().st_size for p in (projector, draft) if p)
    label = ' '.join(x for x in (d['name'] or model.stem, d['quant']) if x)
    return {'id': key, 'name': label, 'file': model.name, 'path': str(model), 'projector': str(projector) if projector else None,
            'draft': str(draft) if draft else None, 'bytes': size + extra, 'model_bytes': size, 'extra_bytes': extra, 'where': where, 'origin': origin,
            'arch': d['arch'], 'size_label': d['size_label'], 'quant': d['quant'], 'vision': bool(projector), 'tools': d['tools'],
            'license': d['license'], 'meta': meta}


def on_drive(data=None):
    """Models in the library's own folder. model.json beside one says where Lastbook brought it from."""
    root = folder(data)
    by_folder = {}
    for p in _walk(root, depth=3):
        by_folder.setdefault(p.parent, []).append(p)
    out = []
    for where, files in sorted(by_folder.items()):
        note = {}
        try:
            note = json.loads((where / NOTE).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        for model, projector, draft, size, meta in _group(files):
            m = _describe(model, projector, draft, size, meta, 'drive', 'This library', 'drive:' + model.relative_to(root).as_posix())
            if note:
                m['source'] = {k: note.get(k) for k in ('repo', 'revision', 'license', 'added') if note.get(k)}
            out.append(m)
    return out


def places():
    """(folder, the program whose it is) for every place another program keeps models on this computer."""
    home = Path.home()
    out = []
    if os.environ.get('LASTBOOK_MODEL_DIR'):
        out.append((Path(os.environ['LASTBOOK_MODEL_DIR']), 'Your model folder'))
    if sys.platform == 'darwin':
        out.append((home / 'Library/Application Support/anythingllm-desktop/storage/models', 'AnythingLLM'))
    elif os.name == 'nt':
        out.append((Path(os.environ.get('APPDATA') or home / 'AppData/Roaming') / 'anythingllm-desktop/storage/models', 'AnythingLLM'))
    else:
        out.append((home / '.config/anythingllm-desktop/storage/models', 'AnythingLLM'))
    out += [(home / '.lmstudio/models', 'LM Studio'), (home / '.cache/lm-studio/models', 'LM Studio'),
            (Path(os.environ.get('HF_HOME') or home / '.cache/huggingface') / 'hub', 'Hugging Face cache')]
    return out


def _ollama():
    """Ollama keeps a model as a manifest naming its parts, and each part as a file named by its digest."""
    root = Path(os.environ.get('OLLAMA_MODELS') or Path.home() / '.ollama/models')
    out = []
    try:
        manifests = [p for p in (root / 'manifests').rglob('*') if p.is_file()][:LIMIT]
    except OSError:
        return out
    for man in manifests:
        try:
            layers = json.loads(man.read_text(encoding='utf-8')).get('layers') or []
        except (OSError, ValueError):
            continue
        part = lambda kind: next((root / 'blobs' / l['digest'].replace(':', '-') for l in layers if l.get('mediaType') == kind and l.get('digest')), None)
        model, projector = part('application/vnd.ollama.image.model'), part('application/vnd.ollama.image.projector')
        if not model or not model.is_file():
            continue
        meta = _header(model)
        if meta is None or gguf.kind_of(meta) != 'model':
            continue
        size = model.stat().st_size
        if size < SMALL:
            continue
        m = _describe(model, projector if projector and projector.is_file() else None, None, size, meta, 'computer', 'Ollama', 'here:' + _digest(model))
        tag = '/'.join(man.parts[-2:]) if len(man.parts) >= 2 else man.name
        m['name'] = f"{m['name']} ({tag.replace('/', ':')})"
        out.append(m)
    return out


def _digest(path):
    return hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]


def on_computer():
    """Models other programs on this computer hold. They are read where they are; nothing is copied, moved or changed."""
    out, seen = [], set()
    for root, origin in places():
        if not root.is_dir():
            continue
        by_folder = {}
        for p in _walk(root):
            by_folder.setdefault(p.parent, []).append(p)
        for where, files in sorted(by_folder.items()):
            for model, projector, draft, size, meta in _group(files):
                try:
                    real = model.resolve()
                except OSError:
                    real = model
                if real in seen:
                    continue
                seen.add(real)
                out.append(_describe(model, projector, draft, size, meta, 'computer', origin, 'here:' + _digest(real)))
    for m in _ollama():
        real = Path(m['path'])
        if real not in seen:
            seen.add(real)
            out.append(m)
    return out


def judge(model, hw=None):
    """Does it fit this computer, and with how large a context? An estimate from the file's header and the computer's memory:
    llama.cpp decides for certain when it loads, and the runtime steps the context down if it must."""
    hw = hw or hardware.detect()
    ram, gpus, unified = hw.get('ram_bytes'), hw.get('gpus') or [], hw.get('unified_memory')
    out = {'ram_bytes': ram, 'estimate': True}
    if not ram:
        return dict(out, fits=None, context=gguf.CONTEXTS[-1], why='Lastbook could not tell how much memory this computer has.')
    room = ram - hardware.RESERVE
    if not unified and gpus:
        room += max(0, max(g.get('vram_bytes') or 0 for g in gpus) - hardware.VRAM_KEPT)      # what goes on the graphics card is not in the computer's own memory
    ctx, need = gguf.best_context(model['meta'], model['model_bytes'], room, model['extra_bytes'])
    if need is None:        # a header that does not give its numbers: judge by the file alone
        need = int(model['bytes'] * 1.2 + gguf.WORKING)
        ctx = gguf.CONTEXTS[-2] if need <= room else None
    # uses_bytes: what the model itself holds while it is loaded. Whether it fits also keeps RESERVE back for the system and the rest of Lastbook.
    out.update(uses_bytes=int(need), needs_bytes=int(need + hardware.RESERVE), context=ctx, fits=ctx is not None)
    if ctx is None:
        out['why'] = f'It would use about {_gb(need)} of memory, and this computer has {_gb(ram)} for everything.'
    else:
        # Measured: a model taking 10 of 13.5 GB of room answered a short question in 79 s with a map build running beside it. That is "little to spare".
        tight = need > 0.7 * room
        out['tight'] = tight
        out['why'] = (f'Uses about {_gb(need)} of this computer’s {_gb(ram)} of memory while loaded'
                      + ('. That leaves little for other programs: close them before asking.' if tight else '.'))
    return out


def public(model):
    """A model as the page is told of it: no header, and no folder names beyond the last."""
    out = {k: v for k, v in model.items() if k not in ('meta', 'path', 'projector', 'draft')}
    out['folder'] = _folder_name(model['path'])
    return out


def _folder_name(p):
    p = Path(p).parent
    try:
        rel = p.relative_to(Path.home())
        parts = rel.parts
        return str(Path(*parts[:3])) + ('/…' if len(parts) > 3 else '') if parts else 'your home folder'
    except ValueError:
        return p.name or str(p)


def found(data=None, computer=True, hw=None):
    """Every model here, judged. Those that travel with the library first; then by how well they fit, larger first."""
    hw = hw or hardware.detect()
    out = on_drive(data) + (on_computer() if computer else [])
    for m in out:
        m['fit'] = judge(m, hw)
    out.sort(key=lambda m: (m['where'] != 'drive', not m['fit']['fits'], -m['model_bytes']))
    return out
