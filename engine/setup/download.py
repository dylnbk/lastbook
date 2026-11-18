"""The one way out: downloads for setup. This is the only module in Lastbook that opens a network connection.

  - HTTPS only, to the hosts in ALLOWED_HOSTS (or a subdomain of ALLOWED_SUFFIXES), port 443.
  - No proxy: http.client never reads the system's or the environment's proxy settings (urllib would), so a request goes
    straight to the host named, or nowhere.
  - A redirect is followed only to another allowed address, at most MAX_REDIRECTS times.
  - Resuming uses HTTP Range on the ETag saved beside the partial file. R2 ignores If-Range (it answers 206 whatever the ETag),
    so the check is made here: another ETag on the 206, a 200 instead, or another total size starts that file over.
    (Not If-Match: on a redirect chain the first host's ETag is not the file's, so it would refuse every resume.)
  - The SHA-256 is computed while the bytes arrive. After a resume the bytes already on disk are read once to rebuild it.
  - Nothing here runs unless the person started or resumed a download (or opened setup: one GET of the manifest).

Tests pass their own `allowed` and `context` to reach a local test server. The product never does.
"""
import errno
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ALLOWED_HOSTS = frozenset({
    'downloads.lastbook.org',
    'huggingface.co',
    'cas-bridge.xethub.hf.co',
    # Open data the library builder reads when the person asks for a map or an encyclopedia (engine/mapbuild, engine/wizard).
    # Each is the publisher's own download host. All of them verify through roots.pem as it is (Let's Encrypt and Amazon).
    'download.geofabrik.de',                        # OpenStreetMap extracts, one per country or region (ODbL)
    'osmdata.openstreetmap.de',                     # land polygons made from OpenStreetMap's coastline (ODbL)
    'copernicus-dem-30m.s3.amazonaws.com',          # Copernicus DEM GLO-30 elevation (AWS Open Data)
    'copernicus-dem-90m.s3.amazonaws.com',          # Copernicus DEM GLO-90
    'overturemaps-us-west-2.s3.amazonaws.com',      # Overture Maps addresses
    'download.geonames.org',                        # postcode points (CC BY 4.0)
    'download.kiwix.org',                           # encyclopedias as ZIM files
    'lb.download.kiwix.org',                        # ...which sends each request on to a mirror near the person: see mirror_allowed
    'mirror.download.kiwix.org',
})
ALLOWED_SUFFIXES = ('.hf.co',)      # Hugging Face's download hosts (cas-bridge.xethub.hf.co and its siblings)
MAX_REDIRECTS = 5
TIMEOUT = 30                        # seconds without a byte before the connection counts as broken
CHUNK = 1024 * 1024
SAVE_EVERY = 2.0                    # seconds between saves of the partial file's .json
NETWORK_TRIES = 6                   # broken connections in a row, with no progress between them, before giving up
BACKOFF = (2, 4, 8, 15, 30, 30)
USER_AGENT = 'Lastbook-setup/1'


class Stopped(Exception):
    """The person paused or cancelled. .why is 'paused' or 'cancelled'."""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


class Failed(Exception):
    """A plain sentence for the person, and a code from docs/SETUP-API.md."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class BadChecksum(Failed):
    def __init__(self, message):
        super().__init__('checksum', message)


def disk_full(e):
    return isinstance(e, OSError) and (e.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)) or getattr(e, 'winerror', None) in (112, 39))


def host_allowed(host, allowed=None):
    host = (host or '').lower().rstrip('.')
    if allowed is not None:
        return host in allowed
    return host in ALLOWED_HOSTS or any(host.endswith(s) and len(host) > len(s) for s in ALLOWED_SUFFIXES)


def url_allowed(url, allowed=None):
    """An https address on an allowed host, with no user name or password in it, on port 443 (a test's own hosts: any port)."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    if u.scheme != 'https' or not u.hostname or u.username or u.password:
        return False
    if allowed is None and port not in (None, 443):
        return False
    return host_allowed(u.hostname, allowed)


def mirror_allowed(url):
    """An address a publisher's own server may send a download on to, when Lastbook already holds the file's SHA-256 from
    an allowed host (Fetch(mirrors=True)): any public https host on port 443. Kiwix hands each request to one of a changing
    set of mirrors run by others, and its own is too slow to be used alone. What arrives is kept only if it is the file
    whose checksum was published, so a mirror can cost time and can never change what is installed. Not a bare address,
    not this computer, not a name that only a local network knows."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    host = (u.hostname or '').lower().rstrip('.')
    if u.scheme != 'https' or not host or u.username or u.password or port not in (None, 443):
        return False
    if '.' not in host or host == 'localhost' or host.endswith(('.local', '.localhost', '.internal', '.lan', '.home', '.arpa')):
        return False
    if host.replace('.', '').isdigit() or ':' in host:      # 192.168.1.1, [::1]
        return False
    return True


def roots_path():
    """engine/setup/roots.pem, beside this file (in the packaged app, where the packaging puts it: engine/setup/ under _MEIPASS)."""
    here = Path(__file__).with_name('roots.pem')
    if not here.is_file() and getattr(sys, 'frozen', False):
        here = Path(getattr(sys, '_MEIPASS', '')) / 'engine' / 'setup' / 'roots.pem'
    return here


def tls_context():
    """TLS 1.2 or later, hostname checked, and trust in the roots of roots.pem only: never the system's store (see
    packaging/setup_roots.py for why and which). With the file missing, nothing is downloaded."""
    roots = roots_path()
    if not roots.is_file():
        raise Failed('network', 'This copy of Lastbook is missing its list of trusted download servers, so it cannot download. '
                                'Download Lastbook again.')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # check_hostname on and CERT_REQUIRED; no default certificates loaded
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(roots))
    return ctx


class Control:
    """Pause and cancel, from any thread. The download notices within one chunk or one second of waiting."""

    def __init__(self):
        self.ev = threading.Event()
        self.why = None

    def stop(self, why):
        if self.why != 'cancelled':
            self.why = why
        self.ev.set()

    def check(self):
        if self.ev.is_set():
            raise Stopped(self.why or 'paused')

    def sleep(self, s):
        if self.ev.wait(s):
            raise Stopped(self.why or 'paused')


def _open(url, start, etag, allowed, context, timeout, span=None, mirrors=False):
    """(connection, response, final url) for a GET from byte `start`, after following allowed redirects.
    span: (first, last) asks for exactly those bytes instead, last included.
    mirrors: the address asked for must be an allowed one as ever, but it may send the request on to a mirror (mirror_allowed).
    Only for a caller that will check what arrives against a SHA-256 it already holds."""
    first = True
    for _ in range(MAX_REDIRECTS + 1):
        if not (url_allowed(url, allowed) or (mirrors and not first and mirror_allowed(url))):
            raise Failed('network', 'Lastbook refused to download from an address it does not know.')
        first = False
        u = urllib.parse.urlsplit(url)
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=context)
        headers = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'}
        if span is not None:
            headers['Range'] = f'bytes={span[0]}-{span[1]}'
        elif start:
            headers['Range'] = f'bytes={start}-'
            if etag:
                headers['If-Range'] = etag
        conn.request('GET', (u.path or '/') + ('?' + u.query if u.query else ''), headers=headers)
        r = conn.getresponse()
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader('Location')
            r.close()
            conn.close()
            if not loc:
                raise Failed('network', 'The download server answered with a move to nowhere.')
            url = urllib.parse.urljoin(url, loc)
            continue
        return conn, r, url
    raise Failed('network', 'The download server sent Lastbook round in circles.')


def fetch_small(url, limit=1 << 20, allowed=None, context=None, timeout=10):
    """One small GET (the manifest). Bytes, or None on any failure."""
    try:
        conn, r, _ = _open(url, 0, None, allowed, context or tls_context(), timeout)
        try:
            if r.status != 200:
                return None
            data = r.read(limit + 1)
            return data if len(data) <= limit else None
        finally:
            conn.close()
    except (OSError, http.client.HTTPException, Failed, ValueError):
        return None


def get_small(url, limit=4 << 20, allowed=None, context=None, timeout=15):
    """(status, body) of one small GET: a publisher's own list of its files. A status other than 200 comes back for the caller
    to put into words (404: no such thing; 401 and 403: not open to everyone). Raises Failed when the server cannot be
    reached, the address is not an allowed one, or the answer is larger than `limit`."""
    try:
        conn, r, _ = _open(url, 0, None, allowed, context or tls_context(), timeout)
        try:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise Failed('network', 'The download server sent far more than was asked for.')
            return r.status, data
        finally:
            conn.close()
    except ssl.SSLCertVerificationError:
        raise Failed('network', 'Lastbook could not confirm it is talking to the download server, so it stopped. '
                                'A network that intercepts secure connections (some offices, hotels) can cause this.')
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise Failed('network', f'The download server could not be reached ({type(e).__name__}). Check the internet connection, then try again.')


def _load_meta(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_meta(path, d):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _content_range(v):
    """(start, total) from 'bytes 100-199/1000', or None."""
    try:
        unit, _, rest = (v or '').partition(' ')
        span, _, total = rest.partition('/')
        return int(span.split('-')[0]), int(total)
    except ValueError:
        return None


class Download:
    """One file: <dest>.part grows, with <dest>.part.json beside it (url, size, sha256, bytes done, ETag).
    run() returns once the whole file is there and its SHA-256 matches, renamed to <dest>. Raises Stopped, Failed or BadChecksum.
    progress(done, total, phase) is called as bytes arrive; phase is 'downloading' or 'verifying'."""

    def __init__(self, url, dest, size, sha256, control, progress=lambda d, t, p: None, allowed=None, context=None, timeout=TIMEOUT):
        self.url, self.dest, self.size, self.sha256 = url, str(dest), int(size), sha256.lower()
        self.part, self.meta = self.dest + '.part', self.dest + '.part.json'
        self.control, self.progress = control, progress
        self.allowed, self.context, self.timeout = allowed, context, timeout

    def done_bytes(self):
        try:
            return min(os.path.getsize(self.part), self.size)
        except OSError:
            return 0

    def discard(self):
        for p in (self.part, self.meta, self.meta + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _rehash(self, n):
        """The SHA-256 of the first n bytes already on disk."""
        h = hashlib.sha256()
        done = 0
        with open(self.part, 'rb') as f:
            while done < n:
                self.control.check()
                b = f.read(min(8 * CHUNK, n - done))
                if not b:
                    break
                h.update(b)
                done += len(b)
                self.progress(done, n, 'verifying')
        return h, done

    def run(self):
        if os.path.isfile(self.dest) and os.path.getsize(self.dest) == self.size and not os.path.exists(self.part):
            return self.dest     # finished and checked before a restart
        os.makedirs(os.path.dirname(self.dest) or '.', exist_ok=True)
        meta = _load_meta(self.meta)
        if meta and (meta.get('sha256') != self.sha256 or meta.get('size') != self.size):
            self.discard()       # left by another version of this file
            meta = {}
        ctx = self.context or tls_context()
        start = self.done_bytes()
        if start and not meta.get('etag'):
            start = 0            # nothing to tell the server which file those bytes came from
        with open(self.part, 'ab') as f:
            f.truncate(start)
        h, start = self._rehash(start) if start else (hashlib.sha256(), 0)
        etag = meta.get('etag')
        tries = 0
        ignored_once = False
        while start < self.size:
            self.control.check()
            try:
                got = self._fetch(start, etag, h, ctx)
                start, etag = got
                tries = 0
            except Stopped:
                raise
            except Failed:
                raise
            except _Restart as r:
                if r.whole and not ignored_once:      # one 200 to a Range can be a passing hiccup at the edge: ask once more first
                    ignored_once = True
                    print(f'setup: the server sent the whole of {os.path.basename(self.dest)} to a resume; asking again', flush=True)
                    self.control.sleep(1)
                    continue
                print(f'setup: the file on the server changed or ignores Range; starting {os.path.basename(self.dest)} over', flush=True)
                with open(self.part, 'r+b') as f:
                    f.truncate(0)
                h, start, etag = hashlib.sha256(), 0, None
                continue
            except ssl.SSLCertVerificationError:
                raise Failed('network', 'Lastbook could not confirm it is talking to the download server, so it stopped. '
                                        'A network that intercepts secure connections (some offices, hotels) can cause this.')
            except (OSError, http.client.HTTPException, ValueError) as e:
                if disk_full(e):
                    raise Failed('no_space', 'The disk filled up while downloading. Free some space, then try again.')
                now = self.done_bytes()     # h already holds every byte written: it is updated just after each write
                tries = 0 if now > start else tries + 1
                start = now
                etag = _load_meta(self.meta).get('etag') or etag
                if tries >= NETWORK_TRIES:
                    raise Failed('network', f'The connection kept breaking ({type(e).__name__}). Check the internet connection, then try again.')
                print(f'setup: download broke at {start} of {self.size}: {type(e).__name__}: {e}; trying again', flush=True)
                self.control.sleep(BACKOFF[min(tries, len(BACKOFF) - 1)])
        if h.hexdigest() != self.sha256:
            self.discard()
            raise BadChecksum('The downloaded file did not match its published checksum.')
        os.replace(self.part, self.dest)
        try:
            os.unlink(self.meta)
        except OSError:
            pass
        return self.dest

    def _fetch(self, start, etag, h, ctx):
        """Reads until the end or a break. Returns (bytes done, etag). Raises _Restart when the server's file is not the one begun."""
        conn, r, final = _open(self.url, start, etag, self.allowed, ctx, self.timeout)
        try:
            if r.status == 416 or r.status >= 400:
                if 500 <= r.status < 600:
                    raise OSError(f'HTTP {r.status}')
                raise Failed('network', f'The download server refused the file (HTTP {r.status}).')
            new_etag = r.getheader('ETag')
            if r.status == 206:
                cr = _content_range(r.getheader('Content-Range'))
                if not cr or cr[0] != start or cr[1] != self.size or (etag and new_etag and new_etag != etag):
                    raise _Restart(new_etag)
            elif r.status == 200:
                if start:
                    raise _Restart(new_etag, whole=True)    # the server sent the whole file again: its copy changed, or it ignores Range
                length = r.getheader('Content-Length')
                if length is not None and length.isdigit() and int(length) != self.size:
                    raise BadChecksum(f'The file on the server is {int(length):,} bytes, not the {self.size:,} Lastbook expects.')
            else:
                raise OSError(f'HTTP {r.status}')
            etag = new_etag or etag
            meta = {'url': self.url, 'size': self.size, 'sha256': self.sha256, 'etag': etag, 'bytes_done': start}
            _save_meta(self.meta, meta)
            saved = time.time()
            with open(self.part, 'r+b') as f:
                f.seek(start)
                while start < self.size:
                    self.control.check()
                    b = r.read1(min(CHUNK, self.size - start))    # what has arrived, up to CHUNK: progress and pause stay quick on a slow line
                    if not b:
                        raise ConnectionError('the connection closed early')
                    f.write(b)
                    h.update(b)
                    start += len(b)
                    self.progress(start, self.size, 'downloading')
                    if time.time() - saved > SAVE_EVERY:
                        f.flush()
                        meta['bytes_done'] = start
                        _save_meta(self.meta, meta)
                        saved = time.time()
                f.flush()
                os.fsync(f.fileno())
            meta['bytes_done'] = start
            _save_meta(self.meta, meta)
            return start, etag
        finally:
            try:
                conn.close()
            except OSError:
                pass


class _Restart(Exception):
    def __init__(self, etag, whole=False):
        super().__init__('restart')
        self.etag, self.whole = etag, whole


# ------------------------------------------------------------------------------------------------------- open data --
# A pack from downloads.lastbook.org comes with its size and SHA-256 in the manifest (Download, above). What follows is
# for files with no such entry: a map extract, an elevation tile, a model someone names, a few bytes from the middle of
# a large zip. The same rules hold: HTTPS, allowed hosts only, no proxy, allowed redirects only, roots.pem only.

class Throttle:
    """The most bytes a second for everything that shares it, so a long download leaves the line usable. None: no limit."""

    def __init__(self, rate=None):
        self.lock = threading.Lock()
        self.set(rate)

    def set(self, rate):
        with self.lock:
            self.rate = float(rate) if rate else None
            self.allowance, self.at = 0.0, time.monotonic()

    def take(self, n, control=None):
        while True:
            with self.lock:
                if not self.rate:
                    return
                now = time.monotonic()
                self.allowance = min(self.rate, self.allowance + (now - self.at) * self.rate)   # at most a second's worth saved up
                self.at = now
                if self.allowance >= n or self.allowance >= self.rate:
                    self.allowance -= n      # may go below zero for one large piece: the next waits it out
                    return
                wait = min(1.0, (min(n, self.rate) - self.allowance) / self.rate)
            if control is not None:
                control.sleep(wait)
            else:
                time.sleep(wait)


THROTTLE = Throttle()      # shared by Fetch and read_range; the builder sets it from the person's choice


def _tries(fn, control=None, tries=NETWORK_TRIES):
    """fn() with the downloader's patience for a broken connection. Failed and Stopped pass straight through."""
    for attempt in range(tries):
        try:
            return fn()
        except (Stopped, Failed):
            raise
        except ssl.SSLCertVerificationError:
            raise Failed('network', 'Lastbook could not confirm it is talking to the download server, so it stopped. '
                                    'A network that intercepts secure connections (some offices, hotels) can cause this.')
        except (OSError, http.client.HTTPException, ValueError) as e:
            if disk_full(e):
                raise Failed('no_space', 'The disk filled up while downloading. Free some space, then try again.')
            if attempt == tries - 1:
                if str(e).startswith('HTTP 5'):     # the server answered, and said it could not: the connection is not what is wrong
                    raise Failed('network', f'The download server is too busy to answer ({e}). Try again in a few minutes.')
                raise Failed('network', f'The connection kept breaking ({type(e).__name__}). Check the internet connection, then try again.')
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            control.sleep(wait) if control is not None else time.sleep(wait)


def head(url, allowed=None, context=None, timeout=TIMEOUT):
    """What the server says about a file without sending it: {'size', 'etag', 'modified', 'url' (after redirects), 'ranges'}.
    Asked with a one-byte Range GET, which every host here answers and which also says whether Range is honoured."""
    def once():
        conn, r, final = _open(url, 0, None, allowed, context or tls_context(), timeout, span=(0, 0))
        try:
            if r.status not in (200, 206):
                if 500 <= r.status < 600:
                    raise OSError(f'HTTP {r.status}')
                raise Failed('network', f'The download server refused the file (HTTP {r.status}).', status=r.status)
            cr = _content_range(r.getheader('Content-Range')) if r.status == 206 else None
            length = r.getheader('Content-Length')
            size = cr[1] if cr else int(length) if length and length.isdigit() else None
            return {'size': size, 'etag': r.getheader('ETag'), 'modified': r.getheader('Last-Modified'), 'url': final, 'ranges': r.status == 206}
        finally:
            conn.close()
    return _tries(once)


def read_range(url, start, end, control=None, allowed=None, context=None, timeout=TIMEOUT, etag=None):
    """Bytes start..end inclusive of a file on an allowed host. For reading a zip's directory, one of its members, or the head
    of a model file, without the rest. etag: the file these bytes must come from; another file there raises Failed."""
    want = end - start + 1

    def once():
        conn, r, _ = _open(url, 0, None, allowed, context or tls_context(), timeout, span=(start, end))
        try:
            if r.status != 206:
                if 500 <= r.status < 600:
                    raise OSError(f'HTTP {r.status}')
                raise Failed('network', 'The download server would not send part of the file.' if r.status == 200
                             else f'The download server refused the file (HTTP {r.status}).', status=r.status)
            if etag and r.getheader('ETag') and r.getheader('ETag') != etag:
                raise Failed('changed', 'The file on the download server changed while Lastbook was reading it.')
            out = bytearray()
            while len(out) < want:
                if control is not None:
                    control.check()
                b = r.read1(min(CHUNK, want - len(out)))
                if not b:
                    raise ConnectionError('the connection closed early')
                THROTTLE.take(len(b), control)
                out += b
            return bytes(out)
        finally:
            conn.close()
    return _tries(once, control)


def stream_range(url, start, end, sink, control=None, allowed=None, context=None, timeout=TIMEOUT, etag=None):
    """Bytes start..end inclusive, handed to sink(bytes) as they arrive, picking up where a broken connection left off.
    Returns the number of bytes delivered (always end - start + 1, or it raises)."""
    ctx = context or tls_context()
    at, tries = start, 0
    while at <= end:
        if control is not None:
            control.check()
        before = at
        try:
            conn, r, _ = _open(url, 0, None, allowed, ctx, timeout, span=(at, end))
            try:
                if r.status != 206:
                    if 500 <= r.status < 600:
                        raise OSError(f'HTTP {r.status}')
                    raise Failed('network', f'The download server refused the file (HTTP {r.status}).', status=r.status)
                if etag and r.getheader('ETag') and r.getheader('ETag') != etag:
                    raise Failed('changed', 'The file on the download server changed while Lastbook was reading it.')
                while at <= end:
                    if control is not None:
                        control.check()
                    b = r.read1(min(CHUNK, end - at + 1))
                    if not b:
                        raise ConnectionError('the connection closed early')
                    THROTTLE.take(len(b), control)
                    try:
                        sink(b)
                    except OSError as e:     # the sink writes to the library's disk: not something asking the server again can mend
                        raise Failed('no_space' if disk_full(e) else 'write',
                                     'The disk filled up while downloading. Free some space, then try again.' if disk_full(e)
                                     else f'Lastbook could not write to the library’s disk ({type(e).__name__}).')
                    at += len(b)
            finally:
                conn.close()
        except (Stopped, Failed):
            raise
        except ssl.SSLCertVerificationError:
            raise Failed('network', 'Lastbook could not confirm it is talking to the download server, so it stopped. '
                                    'A network that intercepts secure connections (some offices, hotels) can cause this.')
        except (OSError, http.client.HTTPException, ValueError) as e:
            tries = 0 if at > before else tries + 1
            if tries >= NETWORK_TRIES:
                raise Failed('network', f'The connection kept breaking ({type(e).__name__}). Check the internet connection, then try again.')
            wait = BACKOFF[min(tries, len(BACKOFF) - 1)]
            control.sleep(wait) if control is not None else time.sleep(wait)
    return end - start + 1


class Fetch:
    """One whole file whose size and checksum are not known beforehand. <dest>.part grows with <dest>.part.json beside it
    (the address it really came from, its ETag, its size); a later run carries on from there if the server still has the
    same file, and starts over if it does not. sha256: checked at the end when the caller has one. run() returns
    {'path', 'size', 'sha256', 'url', 'etag', 'modified'}; a file already whole at dest is returned as it is.
    mirrors: the publisher's server may send the request on to a mirror of its choosing. Refused without a sha256."""

    def __init__(self, url, dest, control=None, progress=lambda d, t, p: None, sha256=None, allowed=None, context=None, timeout=TIMEOUT, mirrors=False):
        if mirrors and not sha256:
            raise ValueError('a mirror may only be used for a file whose SHA-256 is already known')
        self.mirrors = bool(mirrors)
        self.url, self.dest = url, str(dest)
        self.part, self.meta = self.dest + '.part', self.dest + '.part.json'
        self.done = self.dest + '.fetched.json'      # what a finished file was: so a second run knows it without asking
        self.control, self.progress = control or Control(), progress
        self.sha256 = sha256.lower() if sha256 else None
        self.allowed, self.context, self.timeout = allowed, context, timeout

    def run(self):
        if os.path.isfile(self.dest) and not os.path.exists(self.part):
            was = _load_meta(self.done)
            if was.get('size') == os.path.getsize(self.dest) and (not self.sha256 or was.get('sha256') == self.sha256):
                return dict(was, path=self.dest)
        os.makedirs(os.path.dirname(self.dest) or '.', exist_ok=True)
        ctx = self.context or tls_context()
        meta = _load_meta(self.meta)
        try:
            self.at = os.path.getsize(self.part)
        except OSError:
            self.at = 0
        self.size, self.etag, self.modified, self.final = meta.get('size'), meta.get('etag'), meta.get('modified'), meta.get('final') or self.url
        if self.at and not (self.etag or self.modified or (self.mirrors and self.size)):
            self.at = 0             # nothing to tell the server which file those bytes came from
        with open(self.part, 'ab') as f:
            f.truncate(self.at)
        self.h = hashlib.sha256()
        if self.at:
            with open(self.part, 'rb') as f:
                while chunk := f.read(8 * CHUNK):
                    self.control.check()
                    self.h.update(chunk)
        tries = restarts = 0
        while self.size is None or self.at < self.size:
            self.control.check()
            before = self.at
            try:
                self._pull(ctx)
                tries = 0
            except (Stopped, Failed):
                raise
            except _Restart:
                restarts += 1
                if restarts > 3:
                    raise Failed('network', 'The file on the download server keeps changing. Try again later.')
                self._begin_again()
            except ssl.SSLCertVerificationError:
                raise Failed('network', 'Lastbook could not confirm it is talking to the download server, so it stopped. '
                                        'A network that intercepts secure connections (some offices, hotels) can cause this.')
            except (OSError, http.client.HTTPException, ValueError) as e:
                if disk_full(e):
                    raise Failed('no_space', 'The disk filled up while downloading. Free some space, then try again.')
                tries = 0 if self.at > before else tries + 1
                if tries >= NETWORK_TRIES:
                    raise Failed('network', f'The connection kept breaking ({type(e).__name__}). Check the internet connection, then try again.')
                if not (self.etag or self.modified or (self.mirrors and self.size)):
                    self._begin_again()     # the server names its file in no way: what is here cannot be carried on from
                print(f'setup: download of {os.path.basename(self.dest)} broke at {self.at}: {type(e).__name__}: {e}; trying again', flush=True)
                self.control.sleep(BACKOFF[min(tries, len(BACKOFF) - 1)])
        digest = self.h.hexdigest()
        if self.sha256 and digest != self.sha256:
            for p in (self.part, self.meta):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise BadChecksum('The downloaded file did not match its published checksum.')
        os.replace(self.part, self.dest)
        try:
            os.unlink(self.meta)
        except OSError:
            pass
        out = {'size': self.size, 'sha256': digest, 'url': self.final, 'etag': self.etag, 'modified': self.modified}
        _save_meta(self.done, out)
        return dict(out, path=self.dest)

    def _begin_again(self):
        with open(self.part, 'r+b') as f:
            f.truncate(0)
        self.h = hashlib.sha256()
        self.at, self.size, self.etag, self.modified = 0, None, None, None

    def _pull(self, ctx):
        """One connection: reads from self.at until the end of the file or a break. Raises _Restart when what the server
        has is not the file this began with. self.at and self.h are true after every write, whatever happens next."""
        # Through a mirror, the next connection may be to another server holding the same file under another ETag: the file is
        # known by its size here and by its SHA-256 at the end, so no If-Range is sent and no ETag compared.
        conn, r, final = _open(self.url, self.at, None if self.mirrors else self.etag or self.modified, self.allowed, ctx, self.timeout, mirrors=self.mirrors)
        try:
            if r.status == 416:
                raise _Restart(None)        # asked to carry on past the end: the file there is not the one begun
            if r.status >= 400:
                if 500 <= r.status < 600:
                    raise OSError(f'HTTP {r.status}')
                raise Failed('network', f'The download server refused the file (HTTP {r.status}).', status=r.status)
            etag, modified = r.getheader('ETag'), r.getheader('Last-Modified')
            if r.status == 206:
                cr = _content_range(r.getheader('Content-Range'))
                changed = not self.mirrors and ((self.etag and etag and etag != self.etag) or (not self.etag and self.modified and modified and modified != self.modified))
                if not cr or cr[0] != self.at or changed or (self.size is not None and cr[1] != self.size):
                    raise _Restart(etag)
                size = cr[1]
            elif r.status == 200:
                if self.at:
                    raise _Restart(etag)    # the whole file again: its copy changed, or it ignores Range
                length = r.getheader('Content-Length')
                size = int(length) if length and length.isdigit() else None
            else:
                raise OSError(f'HTTP {r.status}')
            self.size, self.etag, self.modified, self.final = size, etag, modified, final

            def note():
                _save_meta(self.meta, {'url': self.url, 'final': final, 'etag': etag, 'modified': modified, 'size': self.size, 'bytes_done': self.at})
            note()
            saved = time.time()
            with open(self.part, 'r+b') as f:
                f.seek(self.at)
                while self.size is None or self.at < self.size:
                    self.control.check()
                    b = r.read1(CHUNK if self.size is None else min(CHUNK, self.size - self.at))
                    if not b:
                        if self.size is None:       # no length was given: the end of the connection is the end of the file
                            self.size = self.at
                            break
                        raise ConnectionError('the connection closed early')
                    THROTTLE.take(len(b), self.control)
                    f.write(b)
                    self.h.update(b)
                    self.at += len(b)
                    self.progress(self.at, self.size or self.at, 'downloading')
                    if time.time() - saved > SAVE_EVERY:
                        f.flush()
                        note()
                        saved = time.time()
                f.flush()
                os.fsync(f.fileno())
            note()
        finally:
            try:
                conn.close()
            except OSError:
                pass
