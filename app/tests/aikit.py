"""Made-up AI models for tests: a GGUF header with chosen numbers, and a stand-in for llama-server. Never packaged."""
import json
import os
import stat
import struct
import sys
import unittest
from pathlib import Path

TYPES = {int: (4, '<I'), float: (6, '<f'), bool: (7, '<?')}


def _string(s):
    b = s.encode('utf-8')
    return struct.pack('<Q', len(b)) + b


def _value(v):
    if isinstance(v, bool):
        return struct.pack('<I', 7) + struct.pack('<?', v)
    if isinstance(v, int):
        return (struct.pack('<I', 10) + struct.pack('<Q', v)) if v > 0xffffffff else (struct.pack('<I', 4) + struct.pack('<I', v))
    if isinstance(v, float):
        return struct.pack('<I', 6) + struct.pack('<f', v)
    if isinstance(v, str):
        return struct.pack('<I', 8) + _string(v)
    if isinstance(v, (list, tuple)):
        first = v[0] if v else 0
        if isinstance(first, str):
            return struct.pack('<IIQ', 9, 8, len(v)) + b''.join(_string(x) for x in v)
        if isinstance(first, bool):
            return struct.pack('<IIQ', 9, 7, len(v)) + b''.join(struct.pack('<?', x) for x in v)
        return struct.pack('<IIQ', 9, 4, len(v)) + b''.join(struct.pack('<I', x) for x in v)
    raise TypeError(type(v))


def gguf_bytes(meta, tensors=3, version=3):
    """A GGUF file's opening: header and metadata, in the order given."""
    out = b'GGUF' + struct.pack('<IQQ', version, tensors, len(meta))
    for k, v in meta.items():
        out += _string(k) + _value(v)
    return out


def plain(name='Test Model', arch='testarch', layers=24, embed=2048, heads=16, kv_heads=8, context=32768, size_label='3B', template=None, **more):
    """A model's metadata as llama.cpp's converter writes it: general, then the architecture, then the tokenizer."""
    m = {'general.architecture': arch, 'general.type': 'model', 'general.name': name, 'general.size_label': size_label, 'general.file_type': 15,
         f'{arch}.block_count': layers, f'{arch}.context_length': context, f'{arch}.embedding_length': embed,
         f'{arch}.attention.head_count': heads, f'{arch}.attention.head_count_kv': kv_heads}
    m.update(more)
    m['tokenizer.ggml.model'] = 'gpt2'
    m['tokenizer.ggml.tokens'] = ['tok%d' % i for i in range(3000)]
    if template is not None:
        m['tokenizer.chat_template'] = template
    return m


def _hole(f, size):
    """The open file made `size` long with nothing written in between. On Windows truncate() is not that: asked for a 40 GB
    file, it ran out of disk on the first run of these tests there, and NTFS makes a hole only in a file marked sparse.
    So there the file is marked and its last byte written."""
    if os.name != 'nt':
        f.truncate(size)
        return
    import ctypes
    import msvcrt
    f.flush()
    done = ctypes.c_ulong()
    ctypes.windll.kernel32.DeviceIoControl(ctypes.c_void_p(msvcrt.get_osfhandle(f.fileno())), 0x900C4, None, 0, None, 0, ctypes.byref(done), None)     # FSCTL_SET_SPARSE
    f.seek(size - 1)
    f.write(b'\0')
    f.flush()


def write_model(path, meta=None, size=None):
    """A file that opens like a model. size: its length on disk (the rest is a hole: nothing is written)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gguf_bytes(meta if meta is not None else plain())
    try:
        with open(path, 'wb') as f:
            f.write(data)
            if size and size > len(data):
                _hole(f, size)
    except OSError as e:
        if e.errno == 28:       # a disk that cannot make a hole, and has no room for the real thing
            raise unittest.SkipTest('this disk cannot hold a file of that size, even one that is mostly a hole')
        raise
    return path


FAKE_SERVER = r'''#!%(python)s
"""A stand-in for llama-server: takes its options, listens where told, answers /health and /v1/chat/completions from a script."""
import http.server, json, os, socketserver, sys, time
a = sys.argv[1:]
if '--help' in a:
    print("""-m,    --model FNAME   model path
-c,    --ctx-size N   context
-np,   --parallel N
-ngl,  --gpu-layers N
--mmproj FILE
-md,   --model-draft FNAME
--spec-type none,draft-mtp
--jinja, --no-jinja
--no-webui
--offline
--no-mmap
--host HOST
--port PORT
--api-key KEY""")
    sys.exit(0)
arg = lambda n, d=None: a[a.index(n) + 1] if n in a else d
record = os.environ.get('FAKE_LLAMA_RECORD')
def note(d):
    if record:
        with open(record, 'a') as f:
            f.write(json.dumps(d) + '\n')
note({'argv': a, 'pid': os.getpid()})
if int(arg('-c', '0')) > int(os.environ.get('FAKE_LLAMA_MAX_CTX', '999999')):
    sys.exit(1)             # as a model too large for the memory there is
if os.environ.get('FAKE_LLAMA_NO_DRAFT') and '-md' in a:
    sys.exit(1)             # as a llama-server that cannot use this model's draft file
time.sleep(float(os.environ.get('FAKE_LLAMA_LOAD_S', '0.1')))
script = json.loads(os.environ.get('FAKE_LLAMA_SCRIPT', '[]'))
turn = [0]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *x):
        pass
    def address_string(self):
        return 'local'
    def do_GET(self):
        ok = self.path == '/health'
        body = b'{"status":"ok"}' if ok else b'{}'
        self.send_response(200 if ok else 404)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
        key = arg('--api-key')
        if key and self.headers.get('Authorization') != 'Bearer ' + key:
            self.send_response(401)
            self.end_headers()
            return
        note({'request': req})
        i = turn[0]
        turn[0] += 1
        if i >= len(script):
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        try:
            for delta in script[i]:
                if 'sleep' in delta:
                    time.sleep(delta['sleep'])
                    continue
                self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        except OSError:
            note({'gone': True})
host = arg('--host')
if host.endswith('.sock'):
    class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
    srv = S(host, H)
else:
    class T(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
    srv = T((host, int(arg('--port'))), H)
srv.serve_forever()
'''


def fake_server(folder):
    """Writes the stand-in and returns its path: set LASTBOOK_LLAMA_SERVER to it. A shell line starts it, so a folder
    with a space in its name does not matter."""
    folder = Path(folder)
    (folder / 'fake_llama.py').write_text(FAKE_SERVER % {'python': sys.executable}, encoding='utf-8')
    p = folder / 'llama-server'
    p.write_text(f'#!/bin/sh\nexec "{sys.executable}" -B "{folder / "fake_llama.py"}" "$@"\n', encoding='utf-8')
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def recorded(path):
    try:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    except OSError:
        return []


HW16 = {'os': 'macos', 'arch': 'arm64', 'ram_bytes': 16 * 2**30, 'unified_memory': True, 'gpus': [{'name': 'Apple M4', 'vram_bytes': 0}], 'disks': []}
HW8 = dict(HW16, ram_bytes=8 * 2**30)
HW_PC = {'os': 'windows', 'arch': 'x64', 'ram_bytes': 16 * 2**30, 'unified_memory': False, 'gpus': [{'name': 'RTX', 'vram_bytes': 12 * 2**30}], 'disks': []}
