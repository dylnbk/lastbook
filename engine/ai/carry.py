"""Putting on the library's drive what the AI needs, so the library answers questions on another computer too.

  model(m, data)     copies a model another program on this computer holds (with its projector and draft) into the
                     library's models/ folder. The other program's files are read, never moved or changed.
  runtime(data)      copies the llama-server this computer has, and the libraries beside it, into
                     models/runtime/<os>-<arch>/. It serves computers of the same kind as this one; a drive that should
                     serve other kinds carries one folder for each.

Nothing here downloads anything: it is one disk to another.
"""
import json
import os
import shutil
import time
from pathlib import Path

from . import models
from . import runtime as runtimes

LIBRARIES = ('.dylib', '.so', '.dll')
BESIDE = ('LICENSE', 'LICENSE.txt', 'LICENSE.md', 'COPYING', '.version', 'NOTICE')
CHUNK = 8 * 2**20
SPARE = 2**30


class Refused(Exception):
    """A plain sentence saying why something could not be copied."""


def _free(folder):
    p = Path(folder)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def _copy(src, dest, progress=None, done=0, total=0, cancelled=lambda: False):
    """One file, through a .part beside where it goes, so a copy that stops is never mistaken for a whole file."""
    part = dest.with_name(dest.name + '.part')
    try:
        with open(src, 'rb') as a, open(part, 'wb') as b:
            while chunk := a.read(CHUNK):
                if cancelled():
                    raise Refused('Stopped.')
                b.write(chunk)
                done += len(chunk)
                if progress:
                    progress({'state': 'copying', 'file': dest.name, 'done_bytes': done, 'total_bytes': total})
        os.replace(part, dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return done


def model(m, data=None, progress=None, cancelled=lambda: False):
    """Copies model m (as engine/ai/models.py describes it) into the library. Returns the id it has there."""
    if m.get('where') == 'drive':
        raise Refused('That model is already in this library.')
    files = [Path(p) for p in (m.get('path'), m.get('projector'), m.get('draft')) if p]
    if m.get('path') and models.gguf.shard(Path(m['path']).name):
        stem = models.gguf.SHARD.sub('', Path(m['path']).name)
        files = sorted(p for p in Path(m['path']).parent.iterdir() if models.gguf.shard(p.name) and models.gguf.SHARD.sub('', p.name) == stem) + files[1:]
    if not files or not all(p.is_file() for p in files):
        raise Refused('The model’s files are not where they were.')
    total = sum(p.stat().st_size for p in files)
    # An Ollama model's file is named by its digest: on the drive it takes a name that says what it is.
    named = lambda p: p.name if p.suffix.lower() == '.gguf' else (_slug(m['name']) + ('.mmproj' if str(p) == m.get('projector') else '') + '.gguf')
    dest = models.folder(data) / _slug(Path(m['path']).stem if Path(m['path']).suffix.lower() == '.gguf' else m['name'])
    have = sum(q.stat().st_size for q in (dest / named(p) for p in files) if q.is_file())
    if _free(dest) < total - have + SPARE:
        raise Refused(f'The library’s drive has {_free(dest) / 1e9:.1f} GB free, and this model is {total / 1e9:.1f} GB.')
    dest.mkdir(parents=True, exist_ok=True)
    done = 0
    for p in files:
        target = dest / named(p)
        if target.is_file() and target.stat().st_size == p.stat().st_size:
            done += p.stat().st_size        # already there from a copy that was stopped later
            continue
        done = _copy(p, target, progress, done, total, cancelled)
    note = {'copied_from': m.get('origin'), 'added': time.strftime('%Y-%m-%d'), 'license': m.get('license'),
            'files': [{'name': named(p), 'bytes': p.stat().st_size} for p in files]}
    (dest / models.NOTE).write_text(json.dumps(note, indent=1), encoding='utf-8')
    if progress:
        progress({'state': 'done', 'done_bytes': total, 'total_bytes': total})
    return 'drive:' + (dest / named(files[0])).relative_to(models.folder(data)).as_posix()


def _slug(name):
    out = ''.join(c if c.isalnum() or c in '._-' else '-' for c in name).strip('-.')
    while '--' in out:
        out = out.replace('--', '-')
    return out[:80] or 'model'


def runtime(data=None):
    """Copies this computer's llama-server and its libraries onto the drive. Returns {'folder', 'files', 'bytes', 'from'}."""
    data = Path(data or models.paths.DATA)
    dest = data / 'models' / 'runtime' / runtimes.platform_folder()
    found = runtimes.find(data)
    if not found:
        raise Refused('No program to run AI models is on this computer to copy.')
    src = Path(found['path']).resolve()
    if dest.resolve() == src.parent:
        raise Refused('The program on the drive is already the one in use.')
    wanted = [src] + sorted(p for p in src.parent.iterdir() if p.is_file() and (p.suffix.lower() in LIBRARIES or '.so.' in p.name or p.name in BESIDE))
    total = sum(p.stat().st_size for p in wanted)
    if total > 2 * 2**30:
        raise Refused('The folder that program is in holds far more than a model runner. Lastbook will not copy it.')
    if _free(dest) < total + SPARE // 4:
        raise Refused('The library’s drive has no room for it.')
    dest.mkdir(parents=True, exist_ok=True)
    for p in wanted:
        _copy(p, dest / p.name)
        if os.name != 'nt':
            os.chmod(dest / p.name, 0o755 if os.access(p, os.X_OK) else 0o644)
    (dest / 'copied-from.json').write_text(json.dumps({'from': found['origin'], 'platform': runtimes.platform_folder(), 'added': time.strftime('%Y-%m-%d'),
                                                        'files': [p.name for p in wanted]}, indent=1), encoding='utf-8')
    return {'folder': str(dest), 'files': len(wanted), 'bytes': total, 'from': found['origin']}
