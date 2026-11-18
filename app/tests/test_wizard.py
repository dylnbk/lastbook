#!/usr/bin/env python3
"""Building a library to order: open-data downloads and a published pack read in place.

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_wizard.py [-v]

A small HTTPS server on this computer stands in for the download hosts (needs openssl on PATH, as test_setup does).
Nothing here reaches the network. Never packaged."""
import hashlib, http.server, io, os, shutil, socketserver, ssl, struct, subprocess, sys, tempfile, threading, time, unittest, zipfile, zlib
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
from engine.setup import download  # noqa: E402
from engine.wizard import packzip  # noqa: E402

KiB = 1024


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Files:
    """What the stand-in server holds and how it misbehaves. log: (name, Range) for every request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.data, self.etag, self.redirect, self.cut_after, self.no_range, self.no_names, self.log = {}, {}, {}, {}, set(), set(), []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        F = self.server.files
        name = self.path.split('?')[0].lstrip('/')
        rng = self.headers.get('Range')
        with F.lock:
            F.log.append((name, rng))
        if name in F.redirect:
            self.send_response(307)
            self.send_header('Location', F.redirect[name])
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if name not in F.data:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = F.data[name]
        first, last = 0, len(body) - 1
        if rng and name not in F.no_range:
            a, _, b = rng.split('=')[1].partition('-')
            first, last = int(a), min(int(b) if b else last, last)
            if first > last:
                self.send_response(416)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {first}-{last}/{len(body)}')
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(last - first + 1))
        if name not in F.no_names:
            self.send_header('ETag', F.etag.get(name, '"e1"'))
            self.send_header('Last-Modified', 'Wed, 18 Nov 2026 00:00:00 GMT')
        self.end_headers()
        with F.lock:
            cut = F.cut_after[name].pop(0) if F.cut_after.get(name) else None
        piece = body[first:last + 1]
        try:
            if cut is not None and cut < len(piece):
                self.wfile.write(piece[:cut])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(2)
                return
            self.wfile.write(piece)
        except OSError:
            return


class TLSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exception(), (ConnectionError, ssl.SSLError)):
            super().handle_error(request, client_address)


def start_server(folder, files):
    key, crt = folder / 'key.pem', folder / 'cert.pem'
    if not crt.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1', '-keyout', str(key), '-out', str(crt)], check=True, capture_output=True)
    srv = TLSServer(('127.0.0.1', 0), Handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(crt, key)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    srv.files = files
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, ssl.create_default_context(cafile=str(crt)), f'https://127.0.0.1:{srv.server_address[1]}'


ALLOWED = frozenset({'127.0.0.1', 'localhost'})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.top = Path(tempfile.mkdtemp(prefix='lb-wizard-srv-'))
        cls.files, cls.files2 = Files(), Files()
        cls.srv, cls.ctx, cls.base = start_server(cls.top, cls.files)
        cls.srv2, _, cls.base2 = start_server(cls.top, cls.files2)       # "another host": a mirror, or somewhere not allowed
        for p in (mock.patch.object(download, 'BACKOFF', (0, 0, 0, 0, 0, 0)), mock.patch.object(download, 'SAVE_EVERY', 0)):
            p.start()
            cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv2.shutdown()
        shutil.rmtree(cls.top, ignore_errors=True)

    def setUp(self):
        self.files.reset()
        self.files2.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-wizard-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.net = dict(allowed=ALLOWED, context=self.ctx)

    def asked(self, name=None):
        with self.files.lock:
            return [r for n, r in self.files.log if name is None or n == name]


class OpenData(Base):
    def test_only_the_publishers_own_hosts(self):
        ok = ['https://download.geofabrik.de/europe/great-britain-latest.osm.pbf', 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip',
              'https://copernicus-dem-30m.s3.amazonaws.com/x/x.tif', 'https://download.geonames.org/export/zip/GB_full.csv.zip',
              'https://download.kiwix.org/zim/wikipedia/w.zim', 'https://lb.download.kiwix.org/zim/wikipedia/w.zim', 'https://huggingface.co/a/b/resolve/main/m.gguf']
        bad = ['http://download.geofabrik.de/x', 'https://download.geofabrik.de.evil.com/x', 'https://evil.s3.amazonaws.com/x', 'https://geofabrik.de/x',
               'https://ftp5.gwdg.de/pub/misc/openstreetmap/download.geofabrik.de/germany-latest.osm.pbf',     # where Geofabrik sends Germany: outside roots.pem, so it is built from its states
               'https://www.mirrorservice.org/sites/download.kiwix.org/zim/w.zim']                              # a mirror: only by way of Kiwix, and only with a checksum
        for u in ok:
            self.assertTrue(download.url_allowed(u), u)
        for u in bad:
            self.assertFalse(download.url_allowed(u), u)

    def test_where_a_publisher_may_send_a_checked_download_on_to(self):
        for u in ('https://www.mirrorservice.org/sites/download.kiwix.org/zim/w.zim', 'https://ftp.nluug.nl/pub/kiwix/w.zim', 'https://mirror.accum.se/x'):
            self.assertTrue(download.mirror_allowed(u), u)
        for u in ('http://www.mirrorservice.org/x', 'https://www.mirrorservice.org:8443/x', 'https://user:pw@mirror.example.org/x', 'https://192.168.1.1/x', 'https://127.0.0.1/x',
                  'https://[::1]/x', 'https://localhost/x', 'https://nas.local/x', 'https://router.lan/x', 'https://intranet/x', 'file:///etc/passwd', 'https:///x', 'ftp://mirror.example.org/x'):
            self.assertFalse(download.mirror_allowed(u), u)

    def test_head_and_ranges(self):
        body = os.urandom(300 * KiB)
        self.files.data['f.bin'] = body
        self.files.redirect['latest'] = '/f.bin'
        h = download.head(f'{self.base}/latest', **self.net)
        self.assertEqual((h['size'], h['ranges'], h['etag'], h['url']), (len(body), True, '"e1"', f'{self.base}/f.bin'))
        self.assertEqual(download.read_range(f'{self.base}/f.bin', 1000, 1999, **self.net), body[1000:2000])
        self.assertEqual(download.read_range(f'{self.base}/f.bin', len(body) - 22, len(body) - 1, etag='"e1"', **self.net), body[-22:])
        with self.assertRaises(download.Failed) as changed:
            download.read_range(f'{self.base}/f.bin', 0, 9, etag='"another file"', **self.net)
        self.assertEqual(changed.exception.code, 'changed')
        self.files.no_range.add('f.bin')
        self.assertFalse(download.head(f'{self.base}/f.bin', **self.net)['ranges'])
        with self.assertRaises(download.Failed):       # the whole file to a request for part of it: not used as if it were the part
            download.read_range(f'{self.base}/f.bin', 1000, 1999, **self.net)

    def test_a_stream_carries_on_after_the_line_breaks(self):
        body = os.urandom(900 * KiB)
        self.files.data['f.bin'] = body
        self.files.cut_after['f.bin'] = [200 * KiB, 50 * KiB]
        got = bytearray()
        n = download.stream_range(f'{self.base}/f.bin', 100, len(body) - 101, got.extend, **self.net)
        self.assertEqual((n, bytes(got)), (len(body) - 200, body[100:-100]))
        self.assertEqual(self.asked('f.bin'), [f'bytes=100-{len(body) - 101}', f'bytes={100 + 200 * KiB}-{len(body) - 101}', f'bytes={100 + 250 * KiB}-{len(body) - 101}'])

    def test_a_sink_that_cannot_write_ends_it_at_once(self):
        self.files.data['f.bin'] = os.urandom(100 * KiB)

        def full(b):
            raise OSError(28, 'No space left on device')
        with self.assertRaises(download.Failed) as e:
            download.stream_range(f'{self.base}/f.bin', 0, 100 * KiB - 1, full, **self.net)
        self.assertEqual(e.exception.code, 'no_space')
        self.assertEqual(len(self.asked('f.bin')), 1)          # not asked for again: the server was not what failed

    def test_a_whole_file_with_no_size_or_checksum_known(self):
        body = os.urandom(700 * KiB)
        self.files.data['dated.pbf'] = body
        self.files.redirect['latest.pbf'] = '/dated.pbf'
        got = download.Fetch(f'{self.base}/latest.pbf', self.tmp / 'x.pbf', **self.net).run()
        self.assertEqual((got['size'], got['sha256'], got['url']), (len(body), sha(body), f'{self.base}/dated.pbf'))
        self.assertEqual((self.tmp / 'x.pbf').read_bytes(), body)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['x.pbf', 'x.pbf.fetched.json'])
        before = len(self.asked())
        again = download.Fetch(f'{self.base}/latest.pbf', self.tmp / 'x.pbf', **self.net).run()
        self.assertEqual((again['sha256'], len(self.asked())), (sha(body), before))          # whole already: nothing asked

    def test_it_carries_on_from_where_it_stopped_if_the_file_is_the_same(self):
        body = os.urandom(900 * KiB)
        self.files.data['f.bin'] = body
        c = download.Control()
        with self.assertRaises(download.Stopped):
            download.Fetch(f'{self.base}/f.bin', self.tmp / 'f', control=c, progress=lambda d, t, p: c.stop('paused') if d >= 300 * KiB else None, **self.net).run()
        part = (self.tmp / 'f.part').stat().st_size
        self.assertTrue(300 * KiB <= part < len(body))
        got = download.Fetch(f'{self.base}/f.bin', self.tmp / 'f', **self.net).run()
        self.assertEqual(got['sha256'], sha(body))
        self.assertEqual(self.asked('f.bin')[-1], f'bytes={part}-')

    def test_it_begins_again_if_the_file_changed(self):
        self.files.data['f.bin'] = b'A' * (600 * KiB)
        c = download.Control()
        with self.assertRaises(download.Stopped):
            download.Fetch(f'{self.base}/f.bin', self.tmp / 'f', control=c, progress=lambda d, t, p: c.stop('paused') if d >= 200 * KiB else None, **self.net).run()
        new = b'B' * (500 * KiB)
        self.files.data['f.bin'], self.files.etag['f.bin'] = new, '"e2"'
        got = download.Fetch(f'{self.base}/f.bin', self.tmp / 'f', **self.net).run()
        self.assertEqual(((self.tmp / 'f').read_bytes(), got['sha256']), (new, sha(new)))    # none of the old file's bytes in it

    def test_a_broken_line_and_a_server_that_names_its_file_in_no_way(self):
        body = os.urandom(500 * KiB)
        self.files.data['f.bin'] = body
        self.files.cut_after['f.bin'] = [100 * KiB]
        self.assertEqual(download.Fetch(f'{self.base}/f.bin', self.tmp / 'a', **self.net).run()['sha256'], sha(body))
        self.assertEqual(self.asked('f.bin'), [None, f'bytes={100 * KiB}-'])
        self.files.log.clear()
        self.files.no_names.add('f.bin')
        self.files.cut_after['f.bin'] = [100 * KiB]
        self.assertEqual(download.Fetch(f'{self.base}/f.bin', self.tmp / 'b', **self.net).run()['sha256'], sha(body))
        self.assertEqual(self.asked('f.bin'), [None, None])    # no ETag and no date: begun again rather than joined to something unknown

    def test_a_checksum_that_is_known_is_checked(self):
        body = os.urandom(100 * KiB)
        self.files.data['m.gguf'] = body
        self.assertEqual(download.Fetch(f'{self.base}/m.gguf', self.tmp / 'ok', sha256=sha(body).upper(), **self.net).run()['size'], len(body))
        with self.assertRaises(download.BadChecksum):
            download.Fetch(f'{self.base}/m.gguf', self.tmp / 'bad', sha256='0' * 64, **self.net).run()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith('bad')), [])

    def test_a_move_to_another_host_is_refused_unless_it_is_a_mirror_of_a_checked_file(self):
        body = os.urandom(400 * KiB)
        self.files2.data['w.zim'] = body
        self.files.redirect['w.zim'] = f'{self.base2}/w.zim'
        other = urllib_host(self.base2)
        only_first = frozenset({urllib_host(self.base)})
        with mock.patch.object(download, 'url_allowed', lambda u, allowed=None: urllib_host(u) in only_first and u.startswith('https://')):
            with self.assertRaises(download.Failed):                                    # no checksum: no mirror
                download.Fetch(f'{self.base}/w.zim', self.tmp / 'a', context=self.ctx).run()
            with self.assertRaises(ValueError):
                download.Fetch(f'{self.base}/w.zim', self.tmp / 'b', context=self.ctx, mirrors=True)
            with self.assertRaises(download.Failed):                                    # a checksum, but the move is to somewhere mirror_allowed refuses
                download.Fetch(f'{self.base}/w.zim', self.tmp / 'c', context=self.ctx, sha256=sha(body), mirrors=True).run()
            with mock.patch.object(download, 'mirror_allowed', lambda u: urllib_host(u) == other):
                got = download.Fetch(f'{self.base}/w.zim', self.tmp / 'd', context=self.ctx, sha256=sha(body), mirrors=True).run()
                self.assertEqual((got['sha256'], got['url']), (sha(body), f'{self.base2}/w.zim'))
                with self.assertRaises(download.Failed):                                # and never as the address asked for in the first place
                    download.Fetch(f'{self.base2}/w.zim', self.tmp / 'e', context=self.ctx, sha256=sha(body), mirrors=True).run()
                self.files2.data['w.zim'] = body[:-1] + bytes([body[-1] ^ 1])          # the mirror holds something else of the same size
                with self.assertRaises(download.BadChecksum):
                    download.Fetch(f'{self.base}/w.zim', self.tmp / 'f', context=self.ctx, sha256=sha(body), mirrors=True).run()
                self.assertFalse((self.tmp / 'f').exists())

    def test_through_mirrors_a_part_is_carried_on_from_whatever_server_answers_next(self):
        body = os.urandom(600 * KiB)
        self.files2.data['w.zim'] = body
        self.files2.etag['w.zim'] = '"mirror-one"'
        self.files.redirect['w.zim'] = f'{self.base2}/w.zim'
        other = urllib_host(self.base2)
        with mock.patch.object(download, 'mirror_allowed', lambda u: urllib_host(u) == other):
            c = download.Control()
            with self.assertRaises(download.Stopped):
                download.Fetch(f'{self.base}/w.zim', self.tmp / 'w', control=c, sha256=sha(body), mirrors=True,
                               progress=lambda d, t, p: c.stop('paused') if d >= 200 * KiB else None, **self.net).run()
            part = (self.tmp / 'w.part').stat().st_size
            self.files2.etag['w.zim'] = '"mirror-two"'      # the next mirror names the same file its own way
            got = download.Fetch(f'{self.base}/w.zim', self.tmp / 'w', sha256=sha(body), mirrors=True, **self.net).run()
        self.assertEqual(got['sha256'], sha(body))
        with self.files2.lock:
            self.assertEqual(self.files2.log[-1], ('w.zim', f'bytes={part}-'))

    def test_the_throttle_holds_the_rate(self):
        t = download.Throttle(200 * KiB)
        start = time.monotonic()
        for _ in range(8):
            t.take(50 * KiB)            # 400 KiB at 200 KiB/s: about two seconds, less the first second's allowance
        took = time.monotonic() - start
        self.assertTrue(0.7 < took < 3.0, took)
        t.set(None)
        start = time.monotonic()
        for _ in range(100):
            t.take(10 * 1024 * KiB)
        self.assertLess(time.monotonic() - start, 0.2)


def urllib_host(u):
    import urllib.parse
    return urllib.parse.urlsplit(u).netloc


def make_zip(members, many=0):
    """A zip as a published pack is: stored members under LastbookData/, a folder entry, one deflated member.
    members: [(name, bytes, method)]. many: that many extra tiny members, enough to need the ZIP64 end record."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', allowZip64=True) as z:
        z.writestr(zipfile.ZipInfo('LastbookData/'), b'')
        for name, data, method in members:
            z.writestr(zipfile.ZipInfo('LastbookData/' + name), data, compress_type=method)
        for i in range(many):
            z.writestr(zipfile.ZipInfo(f'LastbookData/many/{i:06d}.txt'), b'%d' % i)
    return buf.getvalue()


class PackInPlace(Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(packzip, 'BIG', 64 * KiB), mock.patch.object(packzip, 'RUN_MAX', 256 * KiB), mock.patch.object(packzip, 'GAP', 4 * KiB)):
            p.start()
            self.addCleanup(p.stop)
        self.small = [(f'library-data/pages/p{i:03d}.html', os.urandom(3000 + i * 37), zipfile.ZIP_STORED) for i in range(120)]
        self.big = ('library-data/videos/a [id].mp4', os.urandom(700 * KiB), zipfile.ZIP_STORED)
        self.squeezed = ('library-data/text/t.txt', b'water ' * 9000, zipfile.ZIP_DEFLATED)
        self.skipped = ('derived/geo/terrain.sqlite', os.urandom(300 * KiB), zipfile.ZIP_STORED)
        self.all = self.small[:60] + [self.skipped] + self.small[60:] + [self.big, self.squeezed]
        self.zip = make_zip(self.all)
        self.files.data['pack.zip'] = self.zip
        self.url = f'{self.base}/pack.zip'
        self.content = {'LastbookData/' + n: d for n, d, _ in self.all}
        self.manifest = {n: sha(d) for n, d in self.content.items()}

    def pack(self, **k):
        return packzip.open_pack(self.url, self.tmp / 'cache', **self.net, **k)

    def puller(self, pack, dest='lib', **k):
        p = packzip.Puller(pack, self.tmp / dest, strip='LastbookData/', **{**self.net, **k})
        self.addCleanup(p.close)
        return p

    def wanted(self, pack):
        return [m for m in pack.members if not m.name.startswith('LastbookData/derived/')]

    def test_the_directory_is_read_with_three_small_requests(self):
        pack = self.pack()
        self.assertEqual(len(self.asked('pack.zip')), 3)                      # one to learn its size, the tail, the directory itself
        self.assertEqual(sorted(m.name for m in pack.members), sorted(self.content))          # the folder entry is not a file
        for m in pack.members:
            self.assertEqual((m.size, m.crc), (len(self.content[m.name]), zlib.crc32(self.content[m.name])), m.name)
            self.assertEqual(self.zip[m.lho:m.lho + 4], b'PK\x03\x04')
        self.assertEqual({m.method for m in pack.members}, {0, 8})
        self.assertEqual(pack.size, len(self.zip))
        again = self.pack()
        self.assertEqual(again.members, pack.members)
        self.assertEqual(len(self.asked('pack.zip')), 4)                      # kept: one request to see the file is still the same one
        self.files.etag['pack.zip'] = '"e2"'
        self.pack()
        self.assertEqual(len(self.asked('pack.zip')), 8)                      # another file there now: read afresh

    def test_a_pack_with_more_files_than_the_old_zip_format_counts(self):
        self.files.data['pack.zip'] = make_zip(self.small[:3], many=66000)
        pack = self.pack()
        self.assertEqual(len(pack.members), 66003)
        self.assertEqual(pack.by_name['LastbookData/many/065999.txt'].size, 5)
        p = self.puller(pack)
        res = p.run([pack.by_name['LastbookData/many/065999.txt'], pack.by_name['LastbookData/many/000000.txt'], pack.by_name['LastbookData/' + self.small[1][0]]])
        self.assertEqual((res['files'], res['errors']), (3, []))
        self.assertEqual((self.tmp / 'lib/many/065999.txt').read_bytes(), b'65999')

    def test_only_what_is_wanted_comes_down_and_small_files_share_requests(self):
        pack = self.pack()
        self.files.log.clear()
        p = self.puller(pack, verify=self.manifest)
        res = p.run(self.wanted(pack))
        self.assertEqual((res['files'], res['skipped'], res['errors']), (122, 0, []))
        for name, data in self.content.items():
            path = self.tmp / 'lib' / name[len('LastbookData/'):]
            if name.startswith('LastbookData/derived/'):
                self.assertFalse(path.exists(), name)                          # the maps: never asked for
            else:
                self.assertEqual(path.read_bytes(), data, name)
        asked = self.asked('pack.zip')
        self.assertLess(len(asked), 12)                                        # 120 small files in a handful of requests, not 120
        got = sum(int(b) - int(a) + 1 for a, b in (r.split('=')[1].split('-') for r in asked))
        self.assertLess(got, len(self.zip) - len(self.skipped[1]) + 8 * KiB)   # and the skipped member's bytes were not among them
        self.assertEqual([p for p in (self.tmp / 'lib').rglob('*.partial')], [])
        self.assertEqual(p.sha256('LastbookData/' + self.big[0]), sha(self.big[1]))
        self.files.log.clear()
        res = p.run(self.wanted(pack))
        self.assertEqual((res['skipped'], self.asked('pack.zip')), (122, []))  # all here: nothing asked

    def test_a_broken_line_in_the_middle_of_a_run_and_of_a_large_file(self):
        pack = self.pack()
        self.files.cut_after['pack.zip'] = [40 * KiB, 30 * KiB, 100 * KiB, 9 * KiB]
        p = self.puller(pack, workers=1, verify=self.manifest)
        res = p.run(self.wanted(pack))
        self.assertEqual((res['files'], res['errors']), (122, []))
        for name, data in self.content.items():
            if not name.startswith('LastbookData/derived/'):
                self.assertEqual((self.tmp / 'lib' / name[len('LastbookData/'):]).read_bytes(), data, name)

    def test_a_large_file_is_carried_on_from_the_byte_it_reached(self):
        pack = self.pack()
        big = pack.by_name['LastbookData/' + self.big[0]]
        c = download.Control()
        p = self.puller(pack, control=c, workers=1)
        seen = [0]

        class PauseAt:      # the person presses Pause once 200 KiB have arrived: counted where every piece passes, not by a timer
            def take(self, n, control=None):
                seen[0] += n
                if seen[0] >= 200 * KiB:
                    c.stop('paused')
        with mock.patch.object(download, 'THROTTLE', PauseAt()), mock.patch.object(download, 'CHUNK', 16 * KiB):
            with self.assertRaises(download.Stopped):
                p.run([big])
        part = self.tmp / 'lib' / (self.big[0] + '.partial')
        have = part.stat().st_size
        self.assertTrue(200 * KiB <= have < len(self.big[1]))
        p.close()
        self.files.log.clear()
        p2 = self.puller(pack, workers=1, verify=self.manifest)
        res = p2.run([big])
        self.assertEqual((res['files'], res['errors']), (1, []))
        self.assertEqual((self.tmp / 'lib' / self.big[0]).read_bytes(), self.big[1])
        data_at = big.lho + 30 + len(big.name.encode())
        self.assertEqual(self.asked('pack.zip')[-1], f'bytes={data_at + have}-{data_at + len(self.big[1]) - 1}')

    def test_what_is_not_the_file_listed_is_refused_and_the_rest_kept(self):
        pack = self.pack()
        wrong = dict(self.manifest)
        wrong['LastbookData/' + self.small[5][0]] = '0' * 64                  # the manifest says otherwise
        broken = bytearray(self.zip)
        victim = pack.by_name['LastbookData/' + self.small[90][0]]
        broken[victim.lho + 30 + len(victim.name.encode()) + 10] ^= 0xff       # one byte of its data changed on the server
        self.files.data['pack.zip'] = bytes(broken)
        p = self.puller(pack, verify=wrong)
        res = p.run(self.wanted(pack))
        self.assertEqual(res['files'], 120)
        self.assertEqual(len(res['errors']), 2)
        self.assertFalse((self.tmp / 'lib' / self.small[5][0]).exists())
        self.assertFalse((self.tmp / 'lib' / self.small[90][0]).exists())
        self.assertTrue((self.tmp / 'lib' / self.small[6][0]).exists())
        self.assertIsNone(p.sha256('LastbookData/' + self.small[5][0]))

    def test_a_large_file_that_is_not_the_one_listed_leaves_nothing_behind(self):
        pack = self.pack()
        big = pack.by_name['LastbookData/' + self.big[0]]
        broken = bytearray(self.zip)
        broken[big.lho + 30 + len(big.name.encode()) + 5000] ^= 1
        self.files.data['pack.zip'] = bytes(broken)
        p = self.puller(pack, workers=1)
        res = p.run([big])
        self.assertEqual((res['files'], len(res['errors'])), (0, 1))
        self.assertEqual(list((self.tmp / 'lib').rglob('*.mp4*')), [])

    def test_a_folder_begun_from_one_pack_is_not_carried_on_from_another(self):
        pack = self.pack()
        self.puller(pack).run(self.wanted(pack)[:3])
        self.files.etag['pack.zip'] = '"e2"'
        newer = self.pack()
        with self.assertRaises(download.Failed) as e:
            packzip.Puller(newer, self.tmp / 'lib', strip='LastbookData/', **self.net)
        self.assertEqual(e.exception.code, 'changed')

    def test_names_that_would_land_outside_the_folder(self):
        pack = self.pack()
        p = self.puller(pack)
        for name in ('LastbookData/../../etc/passwd', 'LastbookData/a/../../b', 'LastbookData/c:\\x', 'LastbookData/a\\b', 'LastbookData/'):
            with self.assertRaises(packzip.Refused, msg=name):
                p.path(packzip.Member(name, 1, 1, 0, 0, 0))
        self.assertEqual(p.path(packzip.Member('LastbookData/library-data/a b [x].mp4', 1, 1, 0, 0, 0)), self.tmp / 'lib/library-data/a b [x].mp4')

    def test_a_disk_that_fills_stops_everything(self):
        pack = self.pack()
        p = self.puller(pack, workers=3)
        real = open

        def full(path, mode='r', *a, **k):
            if str(path).endswith('.partial'):
                raise OSError(28, 'No space left on device')
            return real(path, mode, *a, **k)
        with mock.patch('builtins.open', full):
            with self.assertRaises(download.Failed) as e:
                p.run(self.wanted(pack))
        self.assertEqual(e.exception.code, 'no_space')


if __name__ == '__main__':
    unittest.main()
