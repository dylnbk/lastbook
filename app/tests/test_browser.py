#!/usr/bin/env python3
"""Lastbook in a browser tab (app/shell_browser.py): what a loopback port is guarded with, and the app started that way
with a library and with none.

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_browser.py [-v]

The library is made up (buildkit.py). No browser is opened and nothing reaches the network. Never packaged."""
import http.server, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import shell_browser  # noqa: E402
import buildkit  # noqa: E402


class Guard(unittest.TestCase):
    def test_only_this_computer_by_name(self):
        for h in ('localhost', 'localhost:8890', '127.0.0.1', '127.0.0.1:51234', '[::1]:8890', '192.168.1.20:8890', '', None):
            self.assertTrue(shell_browser.host_ok(h), h)
        for h in ('evil.example', 'evil.example:8890', 'localhost.evil.example', '127.0.0.1.evil.example', 'lastbook.invalid', 'a b', '[::1'):
            self.assertFalse(shell_browser.host_ok(h), h)

    def test_a_post_comes_from_the_page_itself(self):
        ok = shell_browser.origin_ok
        self.assertTrue(ok(None, '127.0.0.1:8890'))                     # not a browser: no Origin at all
        self.assertTrue(ok('http://127.0.0.1:8890', '127.0.0.1:8890'))
        for origin in ('http://evil.example', 'null', 'http://127.0.0.1:9999', 'https://127.0.0.1.evil.example', 'http://[bad'):
            self.assertFalse(ok(origin, '127.0.0.1:8890'), origin)

    def test_a_handler_made_for_the_window_gets_both_checks(self):
        seen = []

        class Plain(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self):
                seen.append(self.command)
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(b'ok')
            do_GET = do_HEAD = do_POST = answer
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), shell_browser.guarded(Plain))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]

        def ask(method, headers):
            req = urllib.request.Request(f'http://127.0.0.1:{port}/', method=method, data=b'{}' if method == 'POST' else None, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        here = f'127.0.0.1:{port}'
        self.assertEqual(ask('GET', {}), 200)
        self.assertEqual(ask('HEAD', {}), 200)
        self.assertEqual(ask('POST', {'Origin': f'http://{here}'}), 200)
        self.assertEqual(ask('POST', {}), 200)
        self.assertEqual(seen, ['GET', 'HEAD', 'POST', 'POST'])
        self.assertEqual(ask('GET', {'Host': 'evil.example'}), 403)             # a page elsewhere, pointed here by its own name
        self.assertEqual(ask('POST', {'Host': 'evil.example', 'Origin': 'http://evil.example'}), 403)
        self.assertEqual(ask('POST', {'Origin': 'http://evil.example'}), 403)   # a page elsewhere, posting to this address
        self.assertEqual(ask('POST', {'Origin': 'null'}), 403)
        self.assertEqual(len(seen), 4)                                          # none of those reached the handler

    def test_the_port_asked_for_or_another_when_it_is_taken(self):
        class H(http.server.BaseHTTPRequestHandler):
            pass
        taken = socket.socket()
        taken.bind(('127.0.0.1', 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        with mock.patch.object(shell_browser, 'TRIES', 2):
            srv = shell_browser._server(H, taken.getsockname()[1])
        self.addCleanup(srv.server_close)
        self.assertNotEqual(srv.server_address[1], taken.getsockname()[1])
        self.assertEqual(srv.server_address[0], '127.0.0.1')                    # never every interface
        free = shell_browser._server(H, 0)
        self.addCleanup(free.server_close)
        again = free.server_address[1]
        free.server_close()
        got = shell_browser._server(H, again)
        self.addCleanup(got.server_close)
        self.assertEqual(got.server_address[1], again)

    def test_it_runs_until_it_is_stopped_and_opens_the_browser_once(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(204)
                self.end_headers()
        got, opened = {}, []

        def ready(srv, url):
            got.update(srv=srv, url=url)
        env = {k: v for k, v in os.environ.items() if k not in (shell_browser.PORT_ENV, shell_browser.OPENED_ENV)}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(shell_browser.webbrowser, 'open', side_effect=opened.append), \
                mock.patch.object(shell_browser, 'print', create=True):
            t = threading.Thread(target=lambda: got.update(code=shell_browser.run(H, 0, True, False, ready=ready)), daemon=True)
            t.start()
            for _ in range(100):
                if 'url' in got:
                    break
                time.sleep(0.05)
            self.assertRegex(got['url'], r'^http://127\.0\.0\.1:\d+/$')
            with urllib.request.urlopen(got['url'], timeout=10) as r:
                self.assertEqual(r.status, 204)
            self.assertEqual(os.environ[shell_browser.PORT_ENV], got['url'].rsplit(':', 1)[1].rstrip('/'))    # where a start-again will listen
            got['srv'].shutdown()
            t.join(10)
            self.assertEqual(got['code'], 0)
            self.assertEqual(opened, [got['url']])
            port = int(os.environ[shell_browser.PORT_ENV])
            t = threading.Thread(target=lambda: shell_browser.run(H, 0, True, False, ready=ready), daemon=True)     # started again after setup
            t.start()
            for _ in range(100):
                if got['srv'].server_address[1] == port and got['srv'].socket.fileno() != -1:
                    break
                time.sleep(0.05)
            self.assertEqual(got['srv'].server_address[1], port)                # the address the open tab has
            got['srv'].shutdown()
            t.join(10)
            self.assertEqual(len(opened), 1)                                    # and no second tab


class Started(unittest.TestCase):
    """app/main.py --browser, as a process of its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-browser-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.procs = []
        self.addCleanup(self.stop)

    def stop(self):
        for p in self.procs:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            p.stdout.close()

    def start(self, *args, env=None):
        home = self.tmp / 'home'
        home.mkdir(exist_ok=True)
        e = {k: v for k, v in os.environ.items() if not k.startswith('LASTBOOK_')}
        e.update({'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home / 'roaming'), 'LOCALAPPDATA': str(home / 'local'),
                  'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'})
        e.update(env or {})
        p = subprocess.Popen([sys.executable, '-B', str(ROOT / 'app' / 'main.py'), '--browser', '--no-open', '--no-box', *args], cwd=str(self.tmp), env=e,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        said = []
        end = time.time() + 90
        while time.time() < end:
            line = p.stdout.readline()
            if not line:
                break
            said.append(line)
            m = re.search(r'in the browser: (http://127\.0\.0\.1:\d+)/', line)
            if m:
                return m.group(1)
        self.fail('it did not say where it is:\n' + ''.join(said))

    def ask(self, url, body=None, headers=None):
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=dict({'Content-Type': 'application/json'} if body is not None else {}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, json.loads(data) if 'json' in (r.headers.get('Content-Type') or '') else data
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_a_library_opens_in_the_browser(self):
        buildkit.write_pack(self.tmp / 'drive' / 'LastbookData')
        base = self.start('--data', str(self.tmp / 'drive' / 'LastbookData'), '--portable')
        code, health = self.ask(base + '/api/health')
        self.assertEqual(code, 200, health)
        code, page = self.ask(base + '/')
        self.assertEqual(code, 200)
        self.assertIn(b'/ui/app.js', page)
        code, found = self.ask(base + '/api/search?q=boil')
        self.assertEqual((code, found[0]['doc_id']), (200, 'water-guide'))
        code, st = self.ask(base + '/api/wizard/state')                         # setup and the builder answer: it is this computer
        self.assertEqual((code, st['current']['portable']), (200, True))
        code, t = self.ask(base + '/api/wizard/target', {'path': str(self.tmp / 'drive')})
        self.assertEqual((code, t['library'], t['is_current']), (200, True, True))
        self.assertEqual(self.ask(base + '/api/wizard/pick', {})[1], {'cancelled': True})       # no folder chooser in a tab
        self.assertEqual(self.ask(base + '/api/health', headers={'Host': 'evil.example'})[0], 403)
        # A library that travels keeps what the program writes on its drive, in the browser as in the window: nothing in the home folder
        self.assertTrue((self.tmp / 'drive' / 'LastbookData' / 'derived' / 'logs' / 'lastbook.log').is_file())
        self.assertEqual(sorted(p.name for p in (self.tmp / 'home').iterdir()), [])

    def test_with_no_library_setup_opens_in_the_browser(self):
        base = self.start()
        code, page = self.ask(base + '/')
        self.assertEqual(code, 200)
        self.assertIn(b'/ui/setup.js', page)
        code, st = self.ask(base + '/api/setup/state')
        self.assertEqual((code, st['mode'], st['current']), (200, 'first-run', None))
        code, st = self.ask(base + '/api/wizard/state')
        self.assertEqual((code, st['current'], st['build']), (200, None, None))
        self.assertEqual(self.ask(base + '/ui/wizard.html')[0], 200)
        self.assertEqual(self.ask(base + '/api/health')[0], 404)                # nothing of a library: there is none
        self.assertEqual(self.ask(base + '/api/setup/state', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.ask(base + '/api/setup/pause', {}, headers={'Origin': 'http://evil.example'})[0], 403)

    def test_a_program_that_only_opens_a_library(self):
        """As the Android app starts the engine: its pages somewhere other than beside its Python, and no setup or builder to ask."""
        buildkit.write_pack(self.tmp / 'LastbookData')
        pages = self.tmp / 'unpacked'
        shutil.copytree(ROOT / 'web', pages / 'web')
        shutil.copytree(ROOT / 'content', pages / 'content')
        (pages / 'web' / 'app.js').write_text('/* the copy that was unpacked */\n' + (ROOT / 'web' / 'app.js').read_text(encoding='utf-8'), encoding='utf-8')
        base = self.start('--data', str(self.tmp / 'LastbookData'), env={'LASTBOOK_APP': str(pages), 'LASTBOOK_NO_SETUP': '1'})
        code, js = self.ask(base + '/ui/app.js')
        self.assertEqual(code, 200)
        self.assertTrue(js.startswith(b'/* the copy that was unpacked */'))               # the pages come from where it was told
        self.assertEqual(self.ask(base + '/api/search?q=boil')[1][0]['doc_id'], 'water-guide')
        self.assertGreater(len(self.ask(base + '/api/topics')[1]), 3)                   # and the guides
        for path in ('/api/wizard/state', '/api/setup/state'):
            self.assertEqual(self.ask(base + path)[0], 404, path)
        for path in ('/api/wizard/start', '/api/setup/start', '/api/wizard/target'):
            self.assertEqual(self.ask(base + path, {})[0], 404, path)

    def test_the_port_asked_for(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        base = self.start('--port', str(port))
        self.assertEqual(base, f'http://127.0.0.1:{port}')


if __name__ == '__main__':
    unittest.main(verbosity=1)
