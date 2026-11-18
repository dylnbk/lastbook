"""One build, in a process of its own, so the app stays quick while a map or a video is being made.

  python -m engine.wizard.worker <job folder>          (a packaged app: its own program with --wizard-worker <job folder>)

The job folder holds:
  job.json      written by engine/wizard/api.py: {'target', 'source', 'choice', 'rate', 'effort', 'parent'}
  status.json   written here every two seconds: the build's status, with 'state' running, paused, cancelled, failed or done
  stop          written by the app to end the build: the one word 'paused' or 'cancelled'
  log.txt       what the build said, for whoever has to work out what went wrong

The build ends when it is done, when `stop` appears, or when the app that started it is gone: it is never left running
with nobody to see it. Whatever ended it, starting the same job again carries on from where it was.
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

EVERY = 2.0
LOG_LIMIT = 4 * 2**20
EFFORT = {'gentle': {'nice': 15, 'threads': 2, 'map_workers': 1, 'background': True},
          'normal': {'nice': 10, 'threads': None, 'map_workers': 2, 'background': False},
          'full': {'nice': 5, 'threads': 0, 'map_workers': 4, 'background': False}}


def threads_for(effort, cores=None):
    """Processor threads the video encoder may use: 2 when gentle, a little over half the cores normally, all of them at full."""
    cores = cores or os.cpu_count() or 4
    t = EFFORT.get(effort, EFFORT['normal'])['threads']
    return cores if t == 0 else t if t else max(2, min(cores - 2, cores * 6 // 10))


_writing = threading.Lock()


def write_json(path, obj):
    """Whole or not at all, and one writer at a time: the build's last word and the two-second note are written by
    different threads. On Windows a file cannot be replaced while someone is reading it (the app, asking how the build is
    doing), so that is tried again; without this the last word was lost and the build seemed to be running for ever
    (found by the first run of the tests on Windows)."""
    with _writing:
        part = path.with_name(path.name + '.part')
        part.write_text(json.dumps(obj), encoding='utf-8')
        for attempt in range(40):
            try:
                os.replace(part, path)
                return
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)


def alive(pid):
    if not pid:
        return True
    if os.name == 'nt':
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p         # a handle is as wide as a pointer: not to be cut to 32 bits
        h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        return bool(ok) and code.value == 259           # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def run(folder):
    folder = Path(folder)
    job = json.loads((folder / 'job.json').read_text(encoding='utf-8'))
    effort = EFFORT.get(job.get('effort'), EFFORT['normal'])
    try:
        os.nice(effort['nice'])
    except (OSError, AttributeError):
        pass
    if effort['background'] and sys.platform == 'darwin':
        # macOS background priority: the efficiency cores and the back of the queue for the disk; whatever is started from here inherits it
        subprocess.run(['/usr/sbin/taskpolicy', '-b', '-p', str(os.getpid())], check=False, capture_output=True)
    os.environ['LASTBOOK_DATA'] = str(job['target'])       # before the engine is imported: it reads this once
    from engine.setup import download
    from . import build
    if job.get('rate'):
        download.THROTTLE.set(float(job['rate']) * 1e6)
    src = job['source']
    if src.get('kind') == 'keep':       # the library is left as it is: only maps and an AI model are added to it
        source = None
    elif src.get('kind') == 'folder':
        source = build.LocalSource(src['path'])
    else:                               # whatever the address, the downloader goes only to the hosts it knows (engine/setup/download.py)
        source = build.RemoteSource(src['url'])
    choice = dict(job['choice'])
    choice.setdefault('threads', threads_for(job.get('effort')))
    choice.setdefault('map_workers', effort['map_workers'])
    control = download.Control()
    log = open(folder / 'log.txt', 'a', encoding='utf-8')
    if log.tell() > LOG_LIMIT:
        log.truncate(0)

    def say(*words):
        print(time.strftime('%Y-%m-%d %H:%M:%S'), *words, file=log, flush=True)
    b = build.Build(job['target'], source, choice, control=control, say=say)
    ended = {'state': 'running', 'error': None}
    started = time.time()

    def tell():
        write_json(folder / 'status.json', dict(b.status(), state=ended['state'], error=ended['error'], pid=os.getpid(), target=str(job['target']),
                                                 seconds=round(time.time() - started), at=time.time()))

    def watch():
        while ended['state'] == 'running':
            try:
                why = (folder / 'stop').read_text(encoding='utf-8').strip()
            except OSError:
                why = None
            if why is None and not alive(job.get('parent')):
                why = 'paused'      # the app is closed: the build waits for it
            if why:
                control.stop('cancelled' if why == 'cancelled' else 'paused')
            try:
                tell()
            except OSError:
                pass
            time.sleep(EVERY)
    # A `stop` left by an earlier build was removed by whoever started this one. One that is here now was asked for since:
    # it is honoured, at the first place the build can stop.
    say('build:', job['target'], 'from', src.get('path') or src.get('url') or 'nothing: the library is kept as it is')
    tell()
    threading.Thread(target=watch, daemon=True, name='wizard-watch').start()
    try:
        b.run()
        ended['state'] = 'done'
    except download.Stopped as e:
        ended['state'] = 'cancelled' if e.why == 'cancelled' else 'paused'
    except build.Refused as e:
        ended.update(state='failed', error=str(e))
    except download.Failed as e:
        ended.update(state='failed', error=e.message)
    except Exception as e:
        traceback.print_exc(file=log)
        ended.update(state='failed', error=f'The build stopped on an internal error ({type(e).__name__}: {str(e)[:200]}).')
    say('ended:', ended['state'], ended['error'] or '')
    tell()
    log.close()
    return 0 if ended['state'] == 'done' else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    return run(argv[0])


if __name__ == '__main__':
    sys.exit(main())
