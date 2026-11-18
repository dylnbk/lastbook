#!/usr/bin/env python3
"""A library that travels: where it is found, what it keeps and where, and sharing it with phones.

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_portable.py [-v]

Uses the made-up library in derived/app-build/testdata (no model). Never packaged."""
import io, json, os, re, shutil, socket, sqlite3, subprocess, sys, tempfile, unittest, urllib.error, urllib.request
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import fixture  # noqa: E402
DATA = fixture.ensure()
os.environ['LASTBOOK_DATA'] = str(DATA)
os.environ['SURVIVAL_MODEL_BASE'] = 'http://127.0.0.1:9'
os.environ['SURVIVAL_DISABLE_INDEX_WATCH'] = '1'
sys.modules['onnxruntime'] = None   # this Mac runs no AI models
from engine import portable, userstore, qr, paths, sqlitefs  # noqa: E402
import server  # noqa: E402


class _Sock:
    """All of a socket the Handler touches, as app/bridge.py gives it one."""

    def __init__(self, raw):
        self.raw, self.out = raw, bytearray()

    def makefile(self, mode, buffering=-1):
        return io.BufferedReader(io.BytesIO(self.raw))

    def settimeout(self, t):
        pass

    def sendall(self, b):
        self.out += bytes(b)

    def getsockname(self):
        return ('127.0.0.1', 0)

    def shutdown(self, how):
        pass

    def close(self):
        pass


def ask(method, target, body=None, client='127.0.0.1', headers=()):
    """One request to the engine's Handler with no socket: (status, headers, body). client: the address it seems to come from."""
    lines = [f'{method} {target} HTTP/1.1', 'Host: localhost', 'Connection: close', *headers]
    data = b''
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        lines += ['Content-Type: application/json', f'Content-Length: {len(data)}']
    sock = _Sock(('\r\n'.join(lines) + '\r\n\r\n').encode() + data)
    server.Handler(sock, (client, 0), None)
    head, _, rest = bytes(sock.out).partition(b'\r\n\r\n')
    first, *more = head.decode('latin-1').split('\r\n')
    return int(first.split()[1]), {k.lower(): v for k, v in (line.split(': ', 1) for line in more if ': ' in line)}, rest


class Found(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-portable-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_only_a_marked_library_beside_the_program_counts(self):
        drive = self.tmp / 'drive'
        deep = drive / 'bin' / 'macos-arm64' / 'Lastbook.app' / 'Contents' / 'MacOS'
        deep.mkdir(parents=True)
        program = deep / 'Lastbook'
        program.write_text('x')
        lib = drive / 'LastbookData'
        (lib / 'library-data').mkdir(parents=True)
        (lib / 'library-data' / 'catalog.json').write_text('[]')
        self.assertIsNone(portable.beside(program))             # an ordinary library that happens to be near is not taken over
        info = portable.mark(lib)
        self.assertTrue(info['portable'])
        self.assertEqual(portable.beside(program), lib)
        self.assertEqual(portable.beside(drive / 'Start-Mac.command'), lib)   # a launcher at the drive's top
        self.assertTrue(portable.is_portable(lib) and portable.has_library(lib))
        self.assertTrue((lib / 'user').is_dir())
        self.assertEqual(portable.state_dir(lib), drive / '.lastbook')        # beside the library, so a switch of libraries leaves it

    def test_a_library_too_far_up_is_not_found(self):
        lib = self.tmp / 'LastbookData'
        portable.mark(lib)
        far = self.tmp / 'a' / 'b' / 'c' / 'd' / 'e' / 'f' / 'g'
        far.mkdir(parents=True)
        self.assertIsNone(portable.beside(far / 'program'))

    def test_the_mark_says_nothing_about_the_computer(self):
        lib = self.tmp / 'LastbookData'
        portable.mark(lib)
        text = (lib / 'portable.json').read_text()
        for word in (str(Path.home()), os.environ.get('USER') or 'no-user-name', socket.gethostname()):
            self.assertNotIn(word, text)

    def test_the_app_keeps_nothing_on_this_computer_for_one(self):
        import main
        lib = self.tmp / 'drive' / 'LastbookData'
        portable.mark(lib)
        was, env = main.TRAVELLING, os.environ.get('LASTBOOK_PORTABLE')
        try:
            main.go_portable(lib)
            settings, local = main.app_folders()
            self.assertEqual(settings, lib.parent / '.lastbook')
            self.assertEqual(local, lib.parent / '.lastbook')
            self.assertEqual(list(main.places()), [lib])
            self.assertEqual(main.default_choice(), (lib.parent, None))
            main.remember(lib)
            self.assertFalse((settings / 'settings.json').exists())      # found by where it sits, not by a path kept somewhere
            for folder in main.linux_dirs():
                self.assertTrue(str(folder).startswith(str(lib.parent)), folder)
            self.assertEqual(os.environ['LASTBOOK_PORTABLE_STATE'], str(lib.parent / '.lastbook'))    # for the shells: see Shell.rule_stores
        finally:
            main.TRAVELLING = was
            os.environ.pop('LASTBOOK_PORTABLE_STATE', None)
            os.environ.pop('LASTBOOK_PORTABLE', None) if env is None else os.environ.__setitem__('LASTBOOK_PORTABLE', env)

    def start(self, argv, program=None):
        """main.main() as far as the window: (what it returned, the folders it would use, where setup would put a library, the home folder it was given)."""
        import main
        home = self.tmp / 'home'
        home.mkdir(exist_ok=True)
        seen = {}

        def setup():
            seen.update(folders=main.app_folders(), parent=main.default_parent(), places=list(main.places()), env=os.environ.get('LASTBOOK_PORTABLE'))
            return 7
        patches = [mock.patch.dict(os.environ, {'HOME': str(home), 'USERPROFILE': str(home), 'PATH': os.environ.get('PATH', '')}, clear=True), mock.patch.object(main.sys, 'platform', 'darwin'),
                   mock.patch.object(sys, 'argv', ['Lastbook', *argv]), mock.patch.object(main, 'run_setup', setup), mock.patch.object(main, 'TRAVELLING', None)]
        if program:
            patches.append(mock.patch.object(portable, 'program', lambda: program))
        for p in patches:
            p.start()
        try:
            return main.main(), seen, home
        finally:
            for p in reversed(patches):
                p.stop()

    def test_a_marked_drive_with_no_library_yet_opens_setup_and_stays_off_this_computer(self):
        drive = self.tmp / 'drive'
        lib = drive / 'LastbookData'
        portable.mark(lib)
        got, seen, home = self.start(['--data', str(lib)])
        self.assertEqual(got, 7)
        self.assertEqual(seen['folders'], (drive.resolve() / '.lastbook',) * 2)
        self.assertEqual(seen['parent'], drive.resolve())                 # the library goes where its launcher is
        self.assertEqual(seen['places'], [lib.resolve()])
        self.assertEqual(seen['env'], '1')
        self.assertEqual(list(home.rglob('*')), [])                       # nothing under the home folder: no Application Support, no settings

    def test_it_is_found_beside_the_program_with_nothing_named(self):
        drive = self.tmp / 'drive'
        lib = drive / 'LastbookData'
        portable.mark(lib)
        program = drive / 'bin' / 'macos-arm64' / 'Lastbook.app' / 'Contents' / 'MacOS' / 'Lastbook'
        program.parent.mkdir(parents=True)
        program.write_text('x')
        got, seen, home = self.start([], program=program)
        self.assertEqual((got, seen['places']), (7, [lib.resolve()]))
        self.assertEqual(list(home.rglob('*')), [])

    def test_portable_marks_the_folder_named(self):
        lib = self.tmp / 'stick' / 'LastbookData'
        got, seen, _ = self.start(['--data', str(lib), '--portable'])
        self.assertEqual(got, 7)
        self.assertTrue(portable.is_portable(lib))
        self.assertEqual(seen['parent'], lib.parent.resolve())

    def test_an_ordinary_library_is_left_as_it_was(self):
        lib = self.tmp / 'plain' / 'LastbookData'
        (lib / 'library-data').mkdir(parents=True)
        got, seen, home = self.start([], program=self.tmp / 'plain' / 'Lastbook')
        self.assertEqual(got, 7)
        self.assertIsNone(seen['env'])                                    # not marked: not taken over, and this computer's folders as before
        self.assertEqual(seen['folders'][0], home / 'Library' / 'Application Support' / 'Lastbook')

    def test_files_a_mac_leaves_beside_others_are_not_the_library(self):
        names = ['wikipedia.zim', '._wikipedia.zim', '.DS_Store', 'hurt.json', '._hurt.json']
        self.assertEqual([p.name for p in paths.visible(Path(n) for n in names)], ['wikipedia.zim', 'hurt.json'])
        collections = server.LIB / 'collections'
        (collections / '._made-up.zim').write_bytes(b'\x00\x05\x16\x07' + b'\0' * 60)
        self.addCleanup((collections / '._made-up.zim').unlink)
        self.assertEqual(server.zim_names(), [])


class Kept(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-userstore-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = userstore.Store(self.tmp / 'user')
        self.addCleanup(self.store.close)

    def test_put_read_delete_clear(self):
        s = self.store
        self.assertEqual(s.all(), {'kv': {}, 'chats': {}})
        s.apply([{'op': 'put', 'ns': 'kv', 'id': 'sg.saved', 'value': '[1]'}, {'op': 'put', 'ns': 'chats', 'id': 'c1', 'value': '{"id":"c1"}'},
                 {'op': 'put', 'ns': 'chats', 'id': 'c2', 'value': '{"id":"c2"}'}])
        s.apply([{'op': 'put', 'ns': 'kv', 'id': 'sg.saved', 'value': '[1,2]'}, {'op': 'delete', 'ns': 'chats', 'id': 'c1'}])
        self.assertEqual(s.all(), {'kv': {'sg.saved': '[1,2]'}, 'chats': {'c2': '{"id":"c2"}'}})
        s.apply([{'op': 'clear', 'ns': 'chats'}])
        self.assertEqual(s.all()['chats'], {})

    def test_it_is_one_file_with_a_rollback_journal(self):
        self.store.apply([{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': 'b'}])
        self.assertEqual(self.store.journal_mode(), 'delete')
        self.store.close()
        self.assertEqual(sorted(p.name for p in (self.tmp / 'user').iterdir()), ['userdata.sqlite'])
        again = userstore.Store(self.tmp / 'user')
        self.addCleanup(again.close)
        self.assertEqual(again.all()['kv'], {'a': 'b'})         # still there for the next computer that opens it

    def test_what_is_refused_changes_nothing(self):
        s = self.store
        s.apply([{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': '1'}])
        bad = [None, {'ops': 1}, [{'op': 'put', 'ns': 'elsewhere', 'id': 'a', 'value': '1'}], [{'op': 'drop', 'ns': 'kv', 'id': 'a'}],
               [{'op': 'put', 'ns': 'kv', 'id': '', 'value': '1'}], [{'op': 'put', 'ns': 'kv', 'id': 'x' * 201, 'value': '1'}],
               [{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': 5}], [{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': 'x' * (userstore.MAX_VALUE + 1)}],
               [{'op': 'delete', 'ns': 'kv', 'id': 'a'}] * (userstore.MAX_OPS + 1),
               [{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': '2'}, {'op': 'put', 'ns': 'nowhere', 'id': 'b', 'value': '3'}]]   # all of them, or none
        for ops in bad:
            with self.assertRaises(userstore.Refused, msg=str(ops)[:80]):
                s.apply(ops)
        self.assertEqual(s.all()['kv'], {'a': '1'})

    def test_the_oldest_go_when_there_are_too_many(self):
        keep = userstore.KEEP['chats']
        for i in range(0, keep + 30, 50):
            self.store.apply([{'op': 'put', 'ns': 'chats', 'id': f'c{n:04d}', 'value': '{}'} for n in range(i, min(i + 50, keep + 30))])
        chats = self.store.all()['chats']
        self.assertEqual(sorted(chats), [f'c{n:04d}' for n in range(30, keep + 30)])    # exactly the 30 put first are gone

    def test_a_file_that_will_not_open_is_moved_aside_not_fatal(self):
        folder = self.tmp / 'broken'
        folder.mkdir()
        (folder / 'userdata.sqlite').write_bytes(b'this was a database until the drive was pulled out' * 40)
        s = userstore.Store(folder)
        self.addCleanup(s.close)
        self.assertEqual(s.all(), {'kv': {}, 'chats': {}})
        s.apply([{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': 'b'}])
        self.assertEqual(s.all()['kv'], {'a': 'b'})
        self.assertEqual(len([p for p in folder.iterdir() if p.name.startswith('userdata.unreadable-')]), 1)   # kept, in case it can be read by hand


class Reopened(unittest.TestCase):
    """engine/sqlitefs.py. What it guards against needs a real exFAT drive and ten seconds to show (mac_portable_check.py does
    that); here, that a database is never used on the connection that made it, and that the error is mended by opening again."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-sqlitefs-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def make(db):
        db.execute('CREATE TABLE IF NOT EXISTS t(a)')

    def test_the_connection_handed_back_is_not_the_one_that_made_the_file(self):
        seen = []
        real = sqlite3.connect

        def watched(*a, **k):
            c = real(*a, **k)
            seen.append(c)
            return c
        with mock.patch.object(sqlitefs.sqlite3, 'connect', watched):
            db = sqlitefs.connect(self.tmp / 'new.sqlite', self.make)
        self.addCleanup(db.close)
        self.assertEqual(len(seen), 2)                          # one to make it, closed; one to keep
        self.assertIs(db, seen[1])
        with self.assertRaises(sqlite3.ProgrammingError):       # the first really is closed
            seen[0].execute('SELECT 1')
        self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        db.execute('INSERT INTO t VALUES (1)')
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['new.sqlite'])
        with mock.patch.object(sqlitefs.sqlite3, 'connect', watched):   # there already: opened once
            again = sqlitefs.connect(self.tmp / 'new.sqlite', self.make)
        self.addCleanup(again.close)
        self.assertEqual(len(seen), 3)
        self.assertEqual(again.execute('SELECT count(*) FROM t').fetchone()[0], 1)

    def test_a_file_that_is_not_there_needs_a_way_to_make_it(self):
        with self.assertRaises(ValueError):
            sqlitefs.connect(self.tmp / 'none.sqlite')
        self.assertFalse((self.tmp / 'none.sqlite').exists())

    @staticmethod
    def moved_error():
        e = sqlite3.OperationalError('attempt to write a readonly database')
        e.sqlite_errorcode, e.sqlite_errorname = 1032, 'SQLITE_READONLY_DBMOVED'
        return e

    def test_which_errors_mean_the_file_seems_to_have_moved(self):
        self.assertTrue(sqlitefs.moved(self.moved_error()))
        full = sqlite3.OperationalError('database or disk is full')
        full.sqlite_errorcode = 13
        self.assertFalse(sqlitefs.moved(full))
        self.assertFalse(sqlitefs.moved(sqlite3.DatabaseError('file is not a database')))
        self.assertTrue(sqlitefs.moved(sqlite3.OperationalError('attempt to write a readonly database')))   # an older Python gives no code

    def test_a_write_that_meets_it_is_done_once_more_on_a_new_connection(self):
        base = sqlitefs.Database(self.tmp / 'kept.sqlite', self.make)
        self.addCleanup(base.close)
        used = []

        def write(db):
            used.append(db)
            db.execute('BEGIN')
            db.execute('INSERT INTO t VALUES (?)', (len(used),))
            if len(used) == 1:
                raise self.moved_error()                        # as the drive would, part way through
            db.execute('COMMIT')
            return 'saved'
        self.assertEqual(base.run(write), 'saved')
        self.assertEqual(len(used), 2)
        self.assertIsNot(used[0], used[1])
        self.assertEqual(base.run(lambda db: db.execute('SELECT a FROM t').fetchall()), [(2,)])   # the first try left nothing behind

    def test_it_is_tried_once_more_and_no_more(self):
        base = sqlitefs.Database(self.tmp / 'kept.sqlite', self.make)
        self.addCleanup(base.close)
        tries = []

        def write(db):
            tries.append(1)
            raise self.moved_error()
        with self.assertRaises(sqlite3.OperationalError):
            base.run(write)
        self.assertEqual(len(tries), 2)

    def test_other_errors_are_not_tried_again(self):
        base = sqlitefs.Database(self.tmp / 'kept.sqlite', self.make)
        self.addCleanup(base.close)
        tries = []

        def write(db):
            tries.append(1)
            db.execute('INSERT INTO nowhere VALUES (1)')
        with self.assertRaises(sqlite3.OperationalError):
            base.run(write)
        self.assertEqual(len(tries), 1)

    def test_the_store_keeps_saving_when_the_drive_changes_the_files_number(self):
        store = userstore.Store(self.tmp / 'user')
        self.addCleanup(store.close)
        store.apply([{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': '1'}])
        first = store.base.db
        real_run, hit = store.base.run, []

        def flaky(fn):
            def once(db):
                if not hit:
                    hit.append(1)
                    raise self.moved_error()
                return fn(db)
            return real_run(once)
        with mock.patch.object(store.base, 'run', flaky):
            store.apply([{'op': 'put', 'ns': 'kv', 'id': 'b', 'value': '2'}])
        self.assertEqual(store.all()['kv'], {'a': '1', 'b': '2'})
        self.assertIsNot(store.base.db, first)                  # it is on a new connection now


class Code(unittest.TestCase):
    LINK = 'http://192.168.1.23:8890/?key=AbCdEfGhIjKlMnOpQrStUvWx'

    def test_size_and_the_three_finders(self):
        rows = qr.matrix(self.LINK)
        n = len(rows)
        self.assertEqual(n, 17 + 4 * 4)     # 55 bytes at level M: version 4
        self.assertTrue(all(len(r) == n for r in rows))
        finder = ['1111111', '1000001', '1011101', '1011101', '1011101', '1000001', '1111111']
        for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):
            got = [''.join('1' if rows[oy + y][ox + x] else '0' for x in range(7)) for y in range(7)]
            self.assertEqual(got, finder, (ox, oy))
        self.assertTrue(rows[n - 8][8])     # the module that is always dark

    def test_the_format_bits_are_a_valid_codeword_and_both_copies_agree(self):
        for text in ('a', self.LINK, 'x' * 200):
            rows = qr.matrix(text)
            n = len(rows)
            at = lambda x, y: int(rows[y][x])
            first = [at(8, i) for i in range(6)] + [at(8, 7), at(8, 8), at(7, 8)] + [at(14 - i, 8) for i in range(9, 15)]
            second = [at(n - 1 - i, 8) for i in range(8)] + [at(8, n - 15 + i) for i in range(8, 15)]
            self.assertEqual(first, second)
            bits = sum(b << i for i, b in enumerate(first)) ^ 0x5412
            rem = bits
            for i in range(14, 9, -1):      # divide by the generator: a codeword leaves nothing
                if rem >> i & 1:
                    rem ^= 0x537 << (i - 10)
            self.assertEqual(rem, 0, text[:20])

    def test_versions_grow_with_the_text_and_too_much_is_refused(self):
        sizes = [len(qr.matrix('x' * k)) for k in (1, 14, 15, 26, 27, 213, 214, 271)]
        self.assertEqual(sizes, [21, 21, 25, 25, 29, 57, 53, 57])    # 214 bytes no longer fit level M at all, so it drops to L, where version 9 holds them
        with self.assertRaises(qr.TooLong):
            qr.matrix('x' * 272)

    def test_the_same_text_draws_the_same_code(self):
        # Checked with an independent reader (zxing-cpp) when this was written, as were 325 others across every version.
        import hashlib
        rows = qr.matrix(self.LINK)
        digest = hashlib.sha256(''.join('1' if v else '0' for r in rows for v in r).encode()).hexdigest()
        self.assertEqual(digest, GOLDEN)

    def test_svg_is_one_picture_with_a_quiet_zone_and_no_address_but_the_namespace(self):
        s = qr.svg(self.LINK)
        self.assertTrue(s.startswith('<svg ') and s.endswith('</svg>'))
        self.assertIn('viewBox="0 0 41 41"', s)                # 33 modules and 4 of quiet zone each side
        self.assertEqual(re.findall(r'https?://[^"]+', s), ['http://www.w3.org/2000/svg'])
        self.assertNotIn('<script', s)
        # Its colours are its own: the page's rule for icons (a stroke two units wide, no fill) once drew it as a pale smear no camera could read.
        self.assertIn('<path style="fill:#000;stroke:none"', s)
        self.assertIn('<rect width="41" height="41" style="fill:#fff;stroke:none"', s)


GOLDEN = '5ab6f4bdb736805d39c9145f4fc743381d5efd6cb6d3e7f025d8375b363fcb74'   # of Code.LINK's modules, read back correctly by zxing-cpp 3.1.1


class Requests(unittest.TestCase):
    PHONE = '192.168.1.50'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-requests-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        was = server.USERSTORE
        server.USERSTORE = userstore.Store(self.tmp / 'user')
        self.addCleanup(lambda: (server.USERSTORE.close(), setattr(server, 'USERSTORE', was)))

    def test_this_computer_reads_and_saves(self):
        status, _, body = ask('GET', '/api/user-store')
        self.assertEqual((status, json.loads(body)), (200, {'available': True, 'portable': server.PORTABLE, 'data': {'kv': {}, 'chats': {}}}))
        status, _, body = ask('POST', '/api/user-store', {'ops': [{'op': 'put', 'ns': 'kv', 'id': 'sg.saved', 'value': '[]'}]})
        self.assertEqual((status, json.loads(body)), (200, {'ok': True, 'applied': 1}))
        self.assertEqual(json.loads(ask('GET', '/api/user-store')[2])['data']['kv'], {'sg.saved': '[]'})
        self.assertTrue((self.tmp / 'user' / 'userdata.sqlite').is_file())

    def test_bad_requests_are_told_so(self):
        self.assertEqual(ask('POST', '/api/user-store', b'not json')[0], 400)
        self.assertEqual(ask('POST', '/api/user-store', {'ops': [{'op': 'put', 'ns': 'x', 'id': 'a', 'value': '1'}]})[0], 400)
        self.assertEqual(ask('POST', '/api/user-store', {'ops': [{'op': 'put', 'ns': 'kv', 'id': 'a', 'value': 'x' * 300_000}]})[0], 400)   # over the request's own limit
        self.assertEqual(ask('POST', '/api/user-store', {'ops': []}, headers=['Origin: http://evil.example'])[0], 403)                # another page's script

    def test_a_phone_keeps_its_own_and_changes_nothing_here(self):
        was = server.LAN_KEY
        server.LAN_KEY = 'k' * 24
        self.addCleanup(setattr, server, 'LAN_KEY', was)
        cookie = [f'Cookie: sg_key={server.LAN_KEY}']
        self.assertEqual(ask('GET', '/api/user-store', client=self.PHONE)[0], 403)                      # no key: nothing at all
        status, _, body = ask('GET', '/api/user-store', client=self.PHONE, headers=cookie)
        self.assertEqual((status, json.loads(body)), (200, {'available': False}))
        self.assertEqual(ask('POST', '/api/user-store', {'ops': []}, client=self.PHONE, headers=cookie)[0], 403)
        self.assertEqual(ask('POST', '/api/share', {'action': 'stop'}, client=self.PHONE, headers=cookie)[0], 403)
        self.assertEqual(ask('POST', '/api/assistant/local', {'action': 'off'}, client=self.PHONE, headers=cookie)[0], 403)
        status, _, body = ask('GET', '/api/share', client=self.PHONE, headers=cookie)
        self.assertEqual(json.loads(body), {'on': True, 'guest': True})                                 # never the link or the key
        status, head, _ = ask('GET', f'/?key={server.LAN_KEY}', client=self.PHONE)                      # the link from the code: a cookie, then the page
        self.assertEqual(status, 302)
        self.assertIn(f'sg_key={server.LAN_KEY}', head['set-cookie'])
        self.assertEqual(ask('GET', '/', client=self.PHONE, headers=cookie)[0], 200)


NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'needs node on PATH')
class Page(unittest.TestCase):
    """web/userstore.js against a made-up engine and browser (userstore_test.mjs)."""

    @classmethod
    def setUpClass(cls):
        r = subprocess.run([NODE, str(HERE / 'userstore_test.mjs'), str(ROOT / 'web' / 'userstore.js')], capture_output=True, timeout=120)
        assert r.returncode == 0, r.stderr.decode('utf-8', 'replace')[-2000:]
        cls.got = json.loads(r.stdout.decode('utf-8'))

    def test_the_rest_of_the_page_loads_after_the_store_and_in_order(self):
        g = self.got['library']
        self.assertEqual(g['early'], 0)
        self.assertEqual(g['loaded'], [['/ui/first.js?v=1', False], ['/ui/second.js?v=2', False]])
        self.assertTrue(g['sideOff'])       # applied once known: plat.js ran before the store could answer
        for name in ('guest', 'noengine'):  # with no store the page still opens
            self.assertEqual(self.got[name]['loaded'], 2, name)

    def test_an_ordinary_library_takes_over_what_the_browser_kept_once(self):
        g = self.got['library']
        self.assertEqual((g['where'], g['at']), ('library', 'in this library'))
        self.assertEqual(g['saved'], '[1]')                              # the library's own copy wins
        self.assertEqual(g['vprog'], '{"v":1}')                          # only the browser had it: taken over
        self.assertEqual(g['sent'], ['put kv sg.moved', 'put kv sg.vprog'])
        self.assertIsNone(g['cache'])                                    # a copy of the guides is not something the person keeps
        self.assertIsNone(g['other'])                                    # nor is another program's
        self.assertIsNone(g['missing'])
        self.assertEqual(g['chatOrder'], ['b', 'a'])                     # newest first
        self.assertEqual(g['hint'], '0')

    def test_a_library_that_travels_takes_nothing_from_this_browser_and_leaves_nothing_in_it(self):
        g = self.got['portable']
        self.assertEqual((g['where'], g['at']), ('drive', 'on this drive'))
        self.assertIsNone(g['vprog'])                                    # that was another library's, open here before
        self.assertEqual(g['chats'], ['mine'])
        self.assertEqual(g['browserAfter'], ['sg.chats', 'sg.vprog'])    # exactly as it was found
        self.assertEqual(g['sent'], ['put chats mine', 'put kv sg.saved', 'put kv sg.side-off'])

    def test_a_phone_or_an_engine_without_the_store_keeps_the_browsers_own(self):
        for name in ('guest', 'noengine'):
            g = self.got[name]
            self.assertEqual((g['where'], g['read'], g['browserAfter'], g['posts']), ('browser', '{}', ['sg.vprog'], 0), name)

    def test_what_cannot_be_saved_never_holds_up_what_can(self):
        g = self.got['stuck']
        self.assertEqual(g['first'], [['delete kv sg.topicsCache']])     # one an earlier version wrote comes off the drive
        self.assertEqual(g['none'], 1)                                   # the guides' copy and a value too long for one request: no request at all
        self.assertEqual((g['cacheInMemory'], g['hugeInMemory']), (400000, 300000))   # still there for this sitting
        self.assertEqual(g['posts'], [[400, 'sg.refused'],               # refused as it is: dropped
                                      [200, 'sg.after'],                 # so this went alone, with nothing in front of it
                                      [500, 'sg.later'],                 # the drive could not take it: kept
                                      [0, 'sg.later,sg.more'],           # the request itself failed: both kept
                                      [200, 'sg.later,sg.more']])        # and both go

    def test_conversations_are_saved_one_at_a_time_and_only_when_changed(self):
        g = self.got['chats']
        self.assertEqual(g['posts'], [['put chats b', 'put chats a'], ['put chats a', 'delete chats b'], ['put chats long'], ['delete chats a', 'delete chats long']])
        self.assertGreater(g['longWhole'], 700000)
        self.assertLess(g['longSaved'], 230000)                          # what it looked at and did not use went first; the answers stayed
        self.assertLess(g['longRequest'], 256 * 1024)                    # the most the app's bridge takes in one POST (app/bridge.py MAX_POST)

    def test_many_saves_go_in_few_requests_each_under_the_limit(self):
        g = self.got['batches']
        self.assertEqual((g['total'], g['sizes']), (7, [2, 2, 2, 1]))
        self.assertLess(g['biggest'], 256 * 1024)

    def test_the_page_names_no_storage_but_through_it(self):
        for name in ('app.js', 'chat.js', 'map.js', 'setup.js', 'update.js', 'words.js', 'terrain3d.js'):
            self.assertNotIn('localStorage', (ROOT / 'web' / name).read_text(encoding='utf-8'), name)
        import bridge
        self.assertGreater(bridge.MAX_POST, 230000)                      # userstore.js LIMIT must stay under it


class Sharing(unittest.TestCase):
    def test_off_until_asked_then_a_port_a_link_and_a_code_then_off_again(self):
        self.assertEqual(server.share_status(), {'on': False})
        self.assertIsNone(server.LAN_KEY)
        status, _, body = ask('POST', '/api/share', {'action': 'start'})
        self.addCleanup(server.share_stop)
        s = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(s['on'] and s['stoppable'])
        self.assertIn(s['port'], server.SHARE_PORTS)
        if s['ip']:     # on a network
            self.assertEqual(s['url'], f'http://{s["ip"]}:{s["port"]}/?key={server.LAN_KEY}')
            self.assertTrue(s['qr'].startswith('<svg '))
        with urllib.request.urlopen(f'http://127.0.0.1:{s["port"]}/api/health', timeout=10) as r:    # the same engine answers on it
            self.assertEqual(r.status, 200)
        if s['ip']:     # from this computer's own network address, as a phone's request arrives: nothing without the key
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(f'http://{s["ip"]}:{s["port"]}/api/topics', timeout=10)
            self.assertEqual(refused.exception.code, 403)
        self.assertEqual(json.loads(ask('POST', '/api/share', {'action': 'start'})[2])['port'], s['port'])   # asked twice: still the one port
        self.assertEqual(json.loads(ask('POST', '/api/share', {'action': 'stop'})[2]), {'on': False})
        self.assertIsNone(server.LAN_KEY)
        with self.assertRaises(OSError):
            urllib.request.urlopen(f'http://127.0.0.1:{s["port"]}/api/health', timeout=3)
        self.assertEqual(ask('POST', '/api/share', {'action': 'dance'})[0], 400)

    def test_the_key_is_kept_so_the_link_is_the_same_next_time(self):
        self.assertEqual(server.lan_key(), server.lan_key())
        self.assertGreaterEqual(len(server.lan_key()), 20)


if __name__ == '__main__':
    unittest.main()
