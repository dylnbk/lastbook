"""llama.cpp's llama-server, run for the model the person chose: started when someone asks a question, stopped when idle.

Where it listens. On a Mac and on Linux: a UNIX socket in a folder only this user can open, in the system's temporary
folder. There is no port, so nothing else on the computer or the network can reach the model, and it is gone when
Lastbook closes. On Windows, where Python cannot open such a socket: 127.0.0.1 on a free port, behind a key made for
this run that only Lastbook holds.

Where the program comes from, first found wins:
  LASTBOOK_LLAMA_SERVER                           a path the person gives
  <library>/models/runtime/<os>-<arch>/           carried on the drive, one folder per kind of computer (the setup wizard fills it)
  <app>/llm/                                      shipped inside the app
  AnythingLLM's own copy, Homebrew's, the PATH    already on this computer

  find()                 -> {'path', 'origin'} or None
  Runner().ensure(model) -> None when ready, else a sentence saying why not
  Runner().stream(body)  -> the answer to POST /v1/chat/completions, a line at a time
"""
import atexit
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from engine import paths
from engine.setup import hardware
from . import gguf

IDLE_STOP = int(os.environ.get('LASTBOOK_AI_IDLE_S') or 15 * 60)    # as llm_local: seconds unused before the model's memory is given back
START_WAIT = 600
ENOUGH_NOW = 0.75       # a model is started when this share of what it uses is free at the moment; the rest the system finds by compressing
EXE = 'llama-server.exe' if os.name == 'nt' else 'llama-server'


def platform_folder():
    return f'{hardware.os_name()}-{hardware.arch()}'


def candidates(data=None):
    """(path, where it is from), in the order they are tried."""
    out = []
    if os.environ.get('LASTBOOK_LLAMA_SERVER'):
        out.append((Path(os.environ['LASTBOOK_LLAMA_SERVER']), 'the path you gave'))
    out.append((Path(data or paths.DATA) / 'models' / 'runtime' / platform_folder() / EXE, 'this library'))
    out.append((paths.APP / 'llm' / EXE, 'Lastbook'))
    home = Path.home()
    if sys.platform == 'darwin':
        out += [(Path('/Applications/AnythingLLM.app/Contents/Resources/lemond-server/llamacpp/metal') / EXE, 'AnythingLLM'),
                (home / 'Applications/AnythingLLM.app/Contents/Resources/lemond-server/llamacpp/metal' / EXE, 'AnythingLLM'),
                (Path('/opt/homebrew/bin') / EXE, 'Homebrew'), (Path('/usr/local/bin') / EXE, 'Homebrew')]
    elif os.name == 'nt':
        local = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData/Local')
        out += [(local / 'Programs/AnythingLLM/resources/lemond-server/llamacpp' / kind / EXE, 'AnythingLLM') for kind in ('cuda', 'vulkan', 'cpu')]
    else:
        out += [(Path('/usr/local/bin') / EXE, 'this computer'), (Path('/usr/bin') / EXE, 'this computer')]
    which = shutil.which('llama-server')
    if which:
        out.append((Path(which), 'this computer'))
    return out


def find(data=None):
    for path, origin in candidates(data):
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return {'path': str(path), 'origin': origin}
        except OSError:
            continue
    return None


_flags = {}


def flags(server):
    """The options this copy of llama-server knows, from its own --help: they come and go between versions."""
    try:
        key = (server, os.stat(server).st_mtime_ns)
    except OSError:
        return set()
    if key not in _flags:
        try:
            out = subprocess.run([server, '--help'], stdin=subprocess.DEVNULL, capture_output=True, timeout=20, **_quiet())
            text = (out.stdout + out.stderr).decode('utf-8', 'replace')
        except (OSError, subprocess.SubprocessError):
            text = ''
        import re
        _flags[key] = set(re.findall(r'(?<![\w-])(--?[a-z][a-z0-9-]*)', text))
    return _flags[key]


def _quiet():
    return {'creationflags': 0x08000000} if os.name == 'nt' else {}     # CREATE_NO_WINDOW


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(self.timeout)
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


class Runner:
    """One model at a time. States: stopped, loading, ready, error (message)."""

    def __init__(self, data=None):
        self.data = Path(data or paths.DATA)
        self.lock = threading.RLock()
        self.busy = threading.Lock()            # one answer at a time
        self.state, self.message = 'stopped', ''
        self.proc = self.model_id = self.sock = self.port = self.key = self.folder = None
        self.context = None
        self.draft = False          # is the running model using its draft file
        self.last_used = 0
        self.started_at = 0
        self.load_s = None
        self._watching = False

    # ---------------------------------------------------------------------------------------------------- status ----
    def status(self):
        with self.lock:
            return {'state': self.state, 'message': self.message, 'model': self.model_id, 'context': self.context, 'load_s': self.load_s,
                    'draft': bool(self.draft) and self.state in ('ready', 'loading')}

    def running(self, model_id=None):
        with self.lock:
            return self.state == 'ready' and self.proc is not None and self.proc.poll() is None and (model_id is None or model_id == self.model_id)

    # ------------------------------------------------------------------------------------------------- starting ----
    @staticmethod
    def drafts(server, model):
        """Can this llama-server use the model's draft file (the small model that lets a large one write faster)?"""
        known = flags(server)
        return bool(model.get('draft')) and '--spec-type' in known and '-md' in known

    def argv(self, server, model, ctx, draft=True):
        known = flags(server)
        a = [server, '-m', model['path'], '-c', str(ctx), '-np', '1']
        if model.get('projector'):
            a += ['--mmproj', model['projector']]
        if draft and self.drafts(server, model):
            a += ['-md', model['draft'], '--spec-type', 'draft-mtp']
        for flag, value in (('-ngl', '999'), ('--jinja', None), ('--no-webui', None), ('--offline', None), ('--no-slots', None)):
            if flag in known:
                a += [flag] if value is None else [flag, value]
        if '--no-mmap' in known and self._removable(model['path']):
            a.append('--no-mmap')       # a model on a drive that travels is read in whole: it may be unplugged, and exFAT serves mapped pages slowly
        if self.sock:
            a += ['--host', self.sock]
        else:
            a += ['--host', '127.0.0.1', '--port', str(self.port), '--api-key', self.key]
        return a

    @staticmethod
    def _removable(path):
        try:
            return os.stat(path).st_dev != os.stat(Path.home()).st_dev
        except OSError:
            return False

    @staticmethod
    def short_of_memory(model):
        """A sentence if the memory free this moment is well short of what the model uses, else None.

        Whether a model fits the computer is judged from all its memory; this is about now. Loading a model into memory
        other programs are using does work, by pushing them into the swap file, and the whole computer crawls while it
        does (measured: a 9 GB model started beside a map build left 16% free and slowed the build to a stop). Saying so
        and letting the person close something is kinder than that."""
        need = (model.get('fit') or {}).get('uses_bytes')
        have = hardware.available_ram()
        if not need or have is None or have >= ENOUGH_NOW * need:
            return None
        gb = lambda n: f'{n / 2**30:.0f} GB' if n >= 10 * 2**30 else f'{n / 2**30:.1f} GB'
        return (f'This model uses about {gb(need)} of memory, and {gb(have)} is free right now. '
                'Close other programs and ask again, or choose a smaller model.')

    def _listen_at(self):
        """A socket no one else can open, or (Windows) a free loopback port and a key."""
        self._forget_listening()
        if hasattr(socket, 'AF_UNIX') and os.name != 'nt':
            self.folder = tempfile.mkdtemp(prefix='lb-ai-')         # made readable by this user only
            self.sock, self.port, self.key = os.path.join(self.folder, 'ai.sock'), None, None       # llama-server takes a host ending .sock as a socket's path
            if len(self.sock.encode()) > 100:                       # a socket's path may be 104 bytes on a Mac
                shutil.rmtree(self.folder, ignore_errors=True)
                self.folder = tempfile.mkdtemp(prefix='lb-ai-', dir='/tmp')
                self.sock = os.path.join(self.folder, 'ai.sock')
        else:
            s = socket.socket()
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
            s.close()
            self.sock, self.key = None, secrets.token_urlsafe(32)

    def _forget_listening(self):
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)
        self.folder = self.sock = self.port = self.key = None

    def ensure(self, model, on_wait=None, cancelled=lambda: False):
        """Runs `model` (stopping another), waits until it answers. None when ready, 'stopped' if cancelled, else why not."""
        with self.lock:
            if self.running(model['id']):
                self.last_used = time.time()
                return None
            if self.state != 'loading' or self.model_id != model['id']:
                server = find(self.data)
                if not server:
                    self.state, self.message = 'error', 'No program to run AI models is here. Add one in the setup wizard, or install AnythingLLM or llama.cpp.'
                    return self.message
                if not Path(model['path']).is_file():
                    self.state, self.message = 'error', 'The model file is not where it was. Choose a model again.'
                    return self.message
                short = self.short_of_memory(model)
                if short:
                    self.state, self.message = 'error', short
                    return short
                self._stop_locked('another model was chosen')
                contexts = [c for c in gguf.CONTEXTS if c <= ((model.get('fit') or {}).get('context') or gguf.CONTEXTS[0])] or [gguf.CONTEXTS[-1]]
                # What is tried, in turn, until the model loads: first as wanted; then without its draft file (a speed-up this
                # llama-server may not manage for this model); then with smaller and smaller contexts (memory).
                tries = [(c, False) for c in contexts]
                if self.drafts(server['path'], model):
                    tries.insert(0, (contexts[0], True))
                self._spawn(server['path'], model, tries)
        t0 = time.time()
        while time.time() - t0 < START_WAIT:
            with self.lock:
                if self.state == 'ready':
                    self.last_used = time.time()
                    return None
                if self.state != 'loading':
                    return self.message or 'The AI model did not start.'
            if cancelled():
                return 'stopped'
            if on_wait:
                on_wait(None)
            time.sleep(0.25)
        self.stop('took too long to start')
        return 'The AI model took too long to start.'

    def _spawn(self, server, model, tries):
        """tries: [(context, with the draft file)], the first of which is started now."""
        ctx, draft = tries[0]
        self.state, self.message, self.model_id, self.context, self.draft = 'loading', '', model['id'], ctx, draft
        self.started_at = time.time()
        self._listen_at()
        logs = self.data / 'derived' / 'logs'
        try:
            logs.mkdir(parents=True, exist_ok=True)
            err = open(logs / 'ai-runtime.log', 'ab')
        except OSError:
            err = open(os.devnull, 'wb')
        argv = self.argv(server, model, ctx, draft)
        err.write(f'\n--- {time.strftime("%Y-%m-%d %H:%M:%S")} start: {Path(model["path"]).name}, context {ctx}{", with its draft" if draft else ""}\n'.encode())
        err.flush()
        try:
            if os.name == 'nt':
                self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=err, stderr=err, **_quiet())
                import llm_local
                llm_local._tie_to_this_process(self.proc)
            else:
                # A Mac has no way to tell a child "end when your parent does". So a shell stands between: it starts the model
                # and looks every two seconds whether Lastbook is still there; when it is not, however it went, the model goes too.
                # Asked to end, a model holding gigabytes can take its time over it (measured: still there twenty seconds on, with the
                # computer short of memory). It is given three seconds, then ended outright: its memory is wanted back now.
                watch = ('"$@" & child=$!; trap "kill $child 2>/dev/null" TERM INT HUP; '
                         f'while kill -0 {os.getpid()} 2>/dev/null && kill -0 $child 2>/dev/null; do sleep 2; done; '
                         'kill $child 2>/dev/null; n=0; while kill -0 $child 2>/dev/null && [ $n -lt 3 ]; do sleep 1; n=$((n+1)); done; '
                         'kill -9 $child 2>/dev/null; wait $child 2>/dev/null')
                self.proc = subprocess.Popen(['/bin/sh', '-c', watch, 'lastbook-ai', *argv], stdin=subprocess.DEVNULL, stdout=err, stderr=err, start_new_session=True)
        except OSError as e:
            self.state, self.message = 'error', f'The program that runs AI models could not start ({type(e).__name__}).'
            return
        finally:
            err.close()
        threading.Thread(target=self._wait_ready, args=(self.proc, server, model, tries), daemon=True, name='ai-runtime-wait').start()
        if not self._watching:
            self._watching = True
            atexit.register(self.stop, 'Lastbook is closing')
            threading.Thread(target=self._idle_watch, daemon=True, name='ai-runtime-idle').start()

    def _wait_ready(self, proc, server, model, tries):
        """Asks /health until it says ok. If the program ends first, the next way is tried: without the draft file, then with a
        smaller context (memory was the likeliest cause)."""
        while True:
            if proc.poll() is not None:
                with self.lock:
                    if self.proc is not proc:
                        return
                    self.proc = None
                    if len(tries) > 1 and self.state == 'loading':
                        was, now = tries[0], tries[1]
                        print('AI runtime: the model did not load ' + (f'with its draft file; trying without' if was[1] and not now[1] and was[0] == now[0]
                              else f'with a context of {was[0]}; trying {now[0]}'), flush=True)
                        self._spawn(server, model, tries[1:])
                    elif self.state == 'loading':
                        self.state = 'error'
                        self.message = 'The model could not be loaded. It may need more memory than is free: close other programs and ask again.'
                        self._forget_listening()
                return
            try:
                status, _ = self.ask('GET', '/health', timeout=3)
            except (OSError, http.client.HTTPException):
                status = None
            if status == 200:
                with self.lock:
                    if self.proc is proc:
                        self.state, self.message = 'ready', ''
                        self.load_s = round(time.time() - self.started_at, 1)
                        self.last_used = time.time()
                        print(f'AI runtime: ready in {self.load_s} s, context {self.context}', flush=True)
                return
            time.sleep(0.4)

    # ---------------------------------------------------------------------------------------------------- talking ----
    def _connection(self, timeout):
        if self.sock:
            return _UnixConnection(self.sock, timeout)
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)

    def _headers(self):
        h = {'Content-Type': 'application/json'}
        if self.key:
            h['Authorization'] = 'Bearer ' + self.key
        return h

    def ask(self, method, path, body=None, timeout=30):
        c = self._connection(timeout)
        try:
            c.request(method, path, None if body is None else json.dumps(body).encode(), self._headers())
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def stream(self, body, timeout=180):
        """POST /v1/chat/completions; returns the response to read a line at a time, with .close(). Raises OSError if it cannot be reached."""
        self.last_used = time.time()
        c = self._connection(timeout)
        c.request('POST', '/v1/chat/completions', json.dumps(body).encode(), self._headers())
        r = c.getresponse()
        r._lastbook_connection = c      # the connection lives as long as its response
        if r.status != 200:
            detail = r.read(2000).decode('utf-8', 'replace')
            c.close()
            raise OSError(f'HTTP {r.status}: {detail[:300]}')
        return r

    # --------------------------------------------------------------------------------------------------- stopping ----
    def stop(self, why=''):
        with self.lock:
            self._stop_locked(why)

    def _stop_locked(self, why):
        proc, self.proc = self.proc, None
        if self.state in ('ready', 'loading'):
            self.state, self.message = 'stopped', ''
        if proc and proc.poll() is None:
            print(f'AI runtime: stopping ({why or "asked"})', flush=True)
            try:
                if os.name == 'nt':
                    proc.terminate()
                else:
                    os.killpg(proc.pid, 15)         # the shell and the model it started
                proc.wait(4)                        # a model slow to let go is ended outright: the memory is wanted back
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill() if os.name == 'nt' else os.killpg(proc.pid, 9)
                except OSError:
                    pass
        self._forget_listening()

    def _idle_watch(self):
        while True:
            time.sleep(min(30, IDLE_STOP / 4))
            with self.lock:
                idle = self.state == 'ready' and not self.busy.locked() and time.time() - self.last_used > IDLE_STOP
            if idle:
                self.stop(f'not used for {IDLE_STOP} s')
