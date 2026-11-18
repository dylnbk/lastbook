#!/usr/bin/env python3
"""A library of chosen parts: what a pack offers, what a choice comes to, and building it from a pack on a disk or on a server.

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_build.py [-v]

The pack is made up (buildkit.py). Its videos are real, a few seconds long, when this computer has an ffmpeg; the tests
that re-encode them are skipped when it has none. The server is test_wizard.py's, on this computer. Never packaged."""
import json, os, shutil, sqlite3, sys, tempfile, time, unittest, zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
from engine.setup import download  # noqa: E402
from engine.wizard import build, contents, library, packzip, video  # noqa: E402
from engine import portable  # noqa: E402
import buildkit  # noqa: E402
import test_wizard  # noqa: E402

LIB = 'library-data/'
VIDEOS = None           # real ones, made once


def setUpModule():
    global VIDEOS, TOP
    TOP = Path(tempfile.mkdtemp(prefix='lb-build-videos-'))
    VIDEOS = buildkit.make_videos(TOP)


def tearDownModule():
    shutil.rmtree(TOP, ignore_errors=True)


EVERYTHING = {'documents': 'all', 'encyclopedias': 'all', 'videos': {'default': 'original'}}


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-build-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(contents, 'BIG_CHANNEL', 500_000)      # a "large channel" at this pack's scale
        p.start()
        self.addCleanup(p.stop)

    def pack(self, videos=None):
        recs, files = buildkit.records_and_files(videos)
        manifest = {p: (len(b), buildkit.sha(b)) for p, b in files.items()}
        return contents.Pack(recs, manifest), recs, files


class Offered(Temp):
    def test_topics_from_what_a_record_says_of_itself(self):
        t = lambda **r: contents.topic_of({'path': 'documents/x/y.pdf', **r})
        self.assertEqual(t(category='medical'), 'medical')
        self.assertEqual(t(category='first-aid'), 'medical')
        self.assertEqual(t(category='water-sanitation'), 'water')
        self.assertEqual(t(category='food-preservation'), 'food')
        self.assertEqual(t(category='firearm-safety-maintenance'), 'firearms')
        self.assertEqual(t(category='small-engine-repair'), 'power')
        self.assertEqual(t(category='science-education'), 'science')            # a textbook, though "education" is in its name
        self.assertEqual(t(category='accessible-education'), 'community')
        self.assertEqual(t(category='aviation'), 'aviation')
        self.assertEqual(t(category='zzz'), 'other')
        self.assertEqual(contents.topic_of({'path': 'courses/8-04-quantum/pages/x.html', 'category': 'medical'}), 'science')    # a course's pages go with the course
        self.assertEqual(contents.topic_title('water'), 'Water and sanitation')
        self.assertEqual(contents.topic_title('nope'), 'Everything else')

    def test_the_menu_counts_what_each_choice_brings(self):
        pack, recs, files = self.pack()
        m = pack.menu()
        docs = {d['id']: d for d in m['documents']}
        self.assertEqual(set(docs), {'medical', 'water', 'food', 'science', 'aviation'})
        size = lambda *paths: sum(len(files[LIB + p]) for p in paths)
        self.assertEqual(docs['medical']['bytes'], size('documents/medical/first-aid.pdf', 'text/first-aid-manual.txt'))
        # a saved book comes with its pictures and its category's shared pictures; a course with its style sheets
        self.assertEqual(docs['food']['bytes'], size('pages/food-preservation/canning/index.html', 'text/canning-book.txt', 'pages/food-preservation/canning/images/jar.png', 'assets/food-preservation/abc.png'))
        self.assertEqual(docs['science']['bytes'], size('courses/18-06-linear-algebra/pages/notes.html', 'text/algebra-notes.txt', 'courses/18-06-linear-algebra/static/style.css'))
        self.assertTrue(docs['medical']['essential'] and docs['water']['essential'] and not docs['aviation']['essential'])
        self.assertEqual([e['id'] for e in m['encyclopedias']], ['collections/small_wiki.zim', 'collections/big_wiki.zim'])       # smallest first
        vids = {v['id']: v for v in m['videos']}
        self.assertEqual(set(vids), {'channel:Health Films', 'channel:Corporals Corner', 'topic:science'})
        cc = vids['channel:Corporals Corner']
        self.assertEqual((cc['count'], cc['to_shrink'], cc['seconds']), (1, 1, 4))
        self.assertLess(cc['small_bytes'], cc['bytes'])
        self.assertLess(cc['words_bytes'], 5000)
        self.assertEqual(vids['topic:science']['small_bytes'], vids['topic:science']['bytes'])       # already small: left as it is
        self.assertEqual(m['always_bytes'], len(files[LIB + 'reader.css']))

    def test_a_choice_comes_to_records_files_and_videos_to_make_smaller(self):
        pack, recs, files = self.pack()
        p = pack.plan({'documents': ['medical', 'food'], 'encyclopedias': ['collections/small_wiki.zim'],
                       'videos': {'default': 'small', 'channel:Corporals Corner': 'words', 'topic:science': 'none'}})
        ids = [r['id'] for r in p['records']]
        self.assertEqual(sorted(ids), sorted(['first-aid-manual', 'canning-book', 'small-wiki', 'video-bandage', 'metadata-video-bandage', 'transcript-bandage',
                                              'video-campfire', 'metadata-video-campfire', 'transcript-campfire']))
        copied = {path for path, _, _ in p['copy']}
        self.assertIn(LIB + 'pages/food-preservation/canning/images/jar.png', copied)
        self.assertIn(LIB + 'assets/food-preservation/abc.png', copied)
        self.assertNotIn(LIB + 'assets/medical/def.png', copied)                    # pictures of saved pages: no medical page was saved
        self.assertIn(LIB + 'videos/first-aid/bandage.en.vtt', copied)
        self.assertNotIn(LIB + 'videos/first-aid/bandage.mp4', copied)              # it is to be made smaller, not copied
        self.assertNotIn(LIB + 'videos/creator-corporals-corner/campfire.mp4', copied)
        self.assertIn(LIB + 'transcripts/campfire.en.txt', copied)                  # words only: everything but the picture
        self.assertIn(LIB + 'metadata/videos/video-campfire.thumbnail.jpg', copied)
        self.assertNotIn(LIB + 'collections/big_wiki.zim', copied)
        self.assertNotIn('derived/geo/map.sqlite', copied)
        self.assertEqual([(r['id'], profile) for r, profile in p['shrink']], [('video-bandage', 'howto')])
        self.assertTrue(next(r for r in p['records'] if r['id'] == 'video-campfire')['words_only'])
        self.assertEqual(p['counts'], {'documents': 2, 'encyclopedias': 1, 'videos': 2, 'videos_small': 1, 'videos_words': 1})
        self.assertEqual(p['bytes'], sum(s for _, s, _ in p['copy']) + video.estimate(p['shrink'][0][0], 'howto'))
        for path, size, digest in p['copy']:
            self.assertEqual((size, digest), (len(files[path]), buildkit.sha(files[path])))

    def test_every_way_of_keeping_videos(self):
        pack, recs, files = self.pack()
        all_of = lambda mode: pack.plan({'documents': [], 'encyclopedias': [], 'videos': {'default': mode}})
        self.assertEqual(all_of('none')['records'], [])
        original = all_of('original')
        self.assertEqual(len(original['shrink']), 0)
        self.assertIn(LIB + 'videos/creator-corporals-corner/campfire.mp4', {p for p, _, _ in original['copy']})
        small = all_of('small')
        self.assertEqual(sorted((r['id'], pr) for r, pr in small['shrink']), [('video-bandage', 'howto'), ('video-campfire', 'talk')])       # a talking channel is kept smaller still
        self.assertIn(LIB + 'videos/mathematics/lecture.mp4', {p for p, _, _ in small['copy']})         # under 400 kbps: brought as it is
        words = all_of('words')
        self.assertTrue(all(r.get('words_only') for r in words['records'] if r['kind'] == 'video'))
        self.assertFalse(any(p.endswith('.mp4') for p, _, _ in words['copy']))
        self.assertEqual(pack.plan({})['records'], [])
        with self.assertRaises(ValueError):
            pack.plan({'videos': {'default': 'tiny'}})
        everything = pack.plan(EVERYTHING)
        self.assertEqual(len(everything['records']), len(recs))
        self.assertEqual({p for p, _, _ in everything['copy']}, {p for p in files if p.startswith(LIB)})      # all of the library, and nothing of the US map

    def test_how_small_a_video_will_be(self):
        r = {'bytes': 100_000_000, 'duration': 600, 'publisher': 'Someone'}
        self.assertEqual(video.profile_for(r), 'howto')                              # 1,333 kbps
        self.assertEqual(video.profile_for(dict(r, publisher='Corporals Corner')), 'talk')
        self.assertEqual(video.profile_for(dict(r, bytes=20_000_000)), 'keep')       # 267 kbps: small already
        self.assertEqual(video.profile_for({'bytes': 5}), 'keep')                    # no length known: not guessed at
        self.assertEqual(video.estimate(r), (250 + 48) * 1000 // 8 * 600)
        self.assertEqual(video.estimate(dict(r, publisher='Corporals Corner')), (157 + 40) * 1000 // 8 * 600)
        self.assertEqual(video.estimate(dict(r, bytes=20_000_000)), 20_000_000)
        self.assertLessEqual(video.estimate({'bytes': 1000, 'duration': 600, 'publisher': 'x'}, 'howto'), 1000)       # never more than it is now

    def test_a_manifest_is_read_whatever_else_is_in_it(self):
        text = 'path\tbytes\tsha256\thow\tchanges\nlibrary-data/a.pdf\t12\tabc\tclone\t\nbroken line\nlibrary-data/b\tx\ty\n\nlibrary-data/c\t5\t\tclone\t\n'
        self.assertEqual(contents.read_manifest(text), {'library-data/a.pdf': (12, 'abc'), 'library-data/c': (5, None)})


class Written(Temp):
    def test_the_search_index_keeps_only_what_is_kept(self):
        recs, files = buildkit.records_and_files()
        src = self.tmp / 'search.sqlite'
        buildkit.search_db(src, recs, files)
        before = src.read_bytes()
        lib = self.tmp / 'lib'
        pages = library.write_search(src, lib, {'water-guide', 'transcript-bandage'})
        db = sqlite3.connect(lib / 'search.sqlite')
        self.assertEqual({r[0] for r in db.execute('SELECT doc_id FROM pages')}, {'water-guide', 'transcript-bandage'})
        self.assertEqual(pages, db.execute('SELECT COUNT(*) FROM pages').fetchone()[0])
        self.assertEqual(db.execute("SELECT doc_id FROM pages WHERE pages MATCH 'boil'").fetchall(), [('water-guide',)] * 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM pages WHERE pages MATCH 'thermals'").fetchone()[0], 0)
        db.close()
        self.assertLess((lib / 'search.sqlite').stat().st_size, len(before))              # and the room they took is given back
        self.assertEqual(src.read_bytes(), before)                                        # the pack's own is not touched
        self.assertEqual(sorted(p.name for p in lib.iterdir()), ['search.sqlite'])        # nothing left beside it
        everything = library.write_search(src, self.tmp / 'lib2', {r['id'] for r in recs})
        self.assertEqual(everything, sqlite3.connect(src).execute('SELECT COUNT(*) FROM pages').fetchone()[0])

    def test_counts_are_of_what_is_there(self):
        recs, _ = buildkit.records_and_files()
        kept = [dict(r, words_only=True) if r['id'] == 'video-campfire' else r for r in recs if r['id'] != 'glider-handbook']
        s = library.write_summary(self.tmp, kept)
        self.assertEqual((s['pdfs'], s['pdf_pages'], s['web_pages'], s['videos'], s['videos_words_only'], s['collections']), (2, 5, 2, 2, 1, 2))
        self.assertEqual(json.loads((self.tmp / 'summary.json').read_text()), s)
        self.assertNotIn('videos_words_only', library.write_summary(self.tmp, recs))

    def test_files_are_written_whole_or_not_at_all(self):
        src = self.tmp / 'a.bin'
        src.write_bytes(os.urandom(3 * library.CHUNK // 2))
        self.assertEqual(library.copy_file(src, self.tmp / 'deep' / 'b.bin'), buildkit.sha(src.read_bytes()))
        self.assertEqual(library.sha256_of(self.tmp / 'deep' / 'b.bin'), buildkit.sha(src.read_bytes()))
        c = download.Control()
        c.stop('paused')
        with self.assertRaises(download.Stopped):
            library.copy_file(src, self.tmp / 'deep' / 'c.bin', c)
        self.assertEqual(sorted(p.name for p in (self.tmp / 'deep').iterdir()), ['b.bin'])
        library.write_json(self.tmp / 'x.json', {'a': 'é'})
        self.assertEqual((self.tmp / 'x.json').read_text(encoding='utf-8'), '{"a":"é"}')
        library.write_manifest(self.tmp, [('library-data/b', 2, 'bb', 'copy', ''), ('library-data/a', 1, None, 'encode', 'HEVC 480p')])
        self.assertEqual((self.tmp / 'pack-manifest.tsv').read_text(), 'path\tbytes\tsha256\thow\tchanges\nlibrary-data/a\t1\t\tencode\tHEVC 480p\nlibrary-data/b\t2\tbb\tcopy\t\n')
        p = library.write_pack(self.tmp, {'name': 'all', 'snapshot': 's1', 'known_gaps': ['x']}, {'documents': 'all'}, {'documents': 3}, ['great-britain'])
        self.assertEqual((p['name'], p['made_from'], p['regions'], p['known_gaps']), ('custom', {'name': 'all', 'snapshot': 's1'}, ['great-britain'], ['x']))


class Built(Temp):
    """From a pack on a disk here."""

    def source(self, videos=None, without=()):
        self.recs, self.files = buildkit.write_pack(self.tmp / 'pack', videos, without)
        return build.LocalSource(self.tmp / 'pack')

    def run_build(self, choice, source=None, **kw):
        b = build.Build(self.tmp / 'drive' / 'LastbookData', source or self.source(), choice, **kw)
        return b, b.run()

    def test_everything_as_it_is(self):
        b, status = self.run_build(dict(EVERYTHING, portable=True))
        data = self.tmp / 'drive' / 'LastbookData'
        self.assertEqual((status['phase'], status['problems']), ('done', []))
        for path, body in self.files.items():
            if path.startswith(LIB) and path.rsplit('/', 1)[-1] not in ('catalog.json', 'search.sqlite', 'summary.json'):
                self.assertEqual((data / path).read_bytes(), body, path)
        self.assertFalse((data / 'derived' / 'geo').exists())                              # the US map is not part of it
        self.assertEqual(json.loads((data / 'library-data' / 'catalog.json').read_text()), self.recs)
        self.assertTrue(portable.is_portable(data))
        self.assertTrue(portable.has_library(data))
        pack = json.loads((data / 'pack.json').read_text())
        self.assertEqual((pack['name'], pack['made_from']['name'], pack['counts']['videos_pending']), ('custom', 'all', 0))
        listed = contents.read_manifest((data / 'pack-manifest.tsv').read_text())
        for path, (size, digest) in listed.items():
            self.assertEqual((size, digest), ((data / path).stat().st_size, buildkit.sha((data / path).read_bytes())), path)
        self.assertEqual(set(listed), {p for p in self.files if p.startswith(LIB)})
        self.assertFalse((self.tmp / 'drive' / '.lastbook' / 'build').exists())            # its scratch is gone
        self.assertEqual(sorted(p.name for p in data.iterdir()), ['derived', 'library-data', 'pack-manifest.tsv', 'pack.json', 'portable.json', 'user'])

    def test_only_what_was_chosen(self):
        b, status = self.run_build({'documents': ['water'], 'encyclopedias': [], 'videos': {'default': 'words', 'topic:science': 'none'}})
        lib = self.tmp / 'drive' / 'LastbookData' / 'library-data'
        cat = {r['id']: r for r in json.loads((lib / 'catalog.json').read_text())}
        self.assertEqual(set(cat), {'water-guide', 'video-bandage', 'metadata-video-bandage', 'transcript-bandage', 'video-campfire', 'metadata-video-campfire', 'transcript-campfire'})
        self.assertTrue(cat['video-bandage']['words_only'])
        self.assertNotIn('pending', cat['video-bandage'])                                   # left out by choice, not waiting for anything
        self.assertFalse((lib / 'videos' / 'first-aid' / 'bandage.mp4').exists())
        self.assertTrue((lib / 'videos' / 'first-aid' / 'bandage.en.vtt').is_file())
        self.assertFalse((lib / 'documents' / 'medical').exists())
        self.assertFalse((lib / 'collections').exists())
        db = sqlite3.connect(lib / 'search.sqlite')
        self.assertEqual({r[0] for r in db.execute('SELECT doc_id FROM pages')}, {'water-guide', 'video-bandage', 'video-campfire', 'transcript-bandage', 'transcript-campfire'})
        db.close()
        self.assertEqual(json.loads((lib / 'summary.json').read_text())['videos_words_only'], 2)
        self.assertFalse(portable.is_portable(self.tmp / 'drive' / 'LastbookData'))         # not asked to travel

    def test_run_again_it_does_nothing_twice(self):
        choice = dict(EVERYTHING)
        b, _ = self.run_build(choice)
        lib = self.tmp / 'drive' / 'LastbookData' / 'library-data'
        times = {p: p.stat().st_mtime_ns for p in lib.rglob('*') if p.is_file() and p.suffix in ('.pdf', '.mp4', '.zim', '.png')}
        with mock.patch.object(library, 'copy_file', side_effect=lambda src, dest, *a, **k: self.assertIn(Path(dest).name, ('search.sqlite.new',)) or shutil.copyfile(src, dest)):
            b2, status = self.run_build(choice, source=build.LocalSource(self.tmp / 'pack'))
        self.assertEqual(status['phase'], 'done')
        self.assertEqual(times, {p: p.stat().st_mtime_ns for p in times})

    def test_a_pack_folder_still_being_filled(self):
        """What is not there yet is said, left out of the catalog, and brought on a later run."""
        late = [LIB + 'documents/aviation/glider.pdf', LIB + 'collections/big_wiki.zim']
        source = self.source(without=late)
        b, status = self.run_build(dict(EVERYTHING), source=source)
        lib = self.tmp / 'drive' / 'LastbookData' / 'library-data'
        self.assertEqual(status['phase'], 'done')
        self.assertEqual(sorted(status['problems']), sorted(f'{p} could not be had from the pack.' for p in late))
        ids = {r['id'] for r in json.loads((lib / 'catalog.json').read_text())}
        self.assertNotIn('glider-handbook', ids)                                            # the app is not told of a document it cannot open
        self.assertNotIn('big-wiki', ids)
        self.assertIn('water-guide', ids)
        for p in late:
            (self.tmp / 'pack' / p).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / 'pack' / p).write_bytes(self.files[p])
        b, status = self.run_build(dict(EVERYTHING), source=source)
        self.assertEqual(status['problems'], [])
        self.assertEqual({r['id'] for r in json.loads((lib / 'catalog.json').read_text())}, {r['id'] for r in self.recs})
        self.assertEqual((lib / 'documents' / 'aviation' / 'glider.pdf').read_bytes(), self.files[late[0]])

    def test_no_room_no_build(self):
        with mock.patch.object(library, 'free_bytes', return_value=10 * 2**20):
            with self.assertRaisesRegex(build.Refused, 'GB free'):
                self.run_build(dict(EVERYTHING))
        self.assertFalse((self.tmp / 'drive' / 'LastbookData' / 'library-data').exists())
        with self.assertRaisesRegex(build.Refused, 'does not hold a pack'):
            build.Build(self.tmp / 'x', build.LocalSource(self.tmp / 'nothing-here'), {}).run()

    def test_it_can_be_stopped_and_carried_on(self):
        source = self.source()
        c = download.Control()
        real = library.copy_file
        n = [0]

        def slow(src, dest, control=None, told=None):
            n[0] += 1
            if n[0] == 6:
                c.stop('paused')
            return real(src, dest, control, told)
        with mock.patch.object(library, 'copy_file', slow):
            with self.assertRaises(download.Stopped):
                build.Build(self.tmp / 'drive' / 'LastbookData', source, dict(EVERYTHING), control=c).run()
        lib = self.tmp / 'drive' / 'LastbookData' / 'library-data'
        self.assertFalse((lib / 'catalog.json').exists())                                   # not a library yet: the app will not open half of one
        self.assertEqual([p for p in lib.rglob('*.part')], [])
        b, status = self.run_build(dict(EVERYTHING), source=source)
        self.assertEqual((status['phase'], status['problems']), ('done', []))
        self.assertEqual(len(json.loads((lib / 'catalog.json').read_text())), len(self.recs))

    def test_maps_are_built_first_and_one_that_fails_does_not_cost_the_library(self):
        from engine.mapbuild import build as mapbuild
        asked = []

        class Builder:
            def __init__(self, data, sources_folder=None, say=None, control=None, workers=2):
                asked.append(('data', str(data), sources_folder))

            def current(self, rid, level):
                return rid == 'isle-of-man'

            def build(self, rid, level='detail', terrain=True):
                asked.append((rid, level, terrain))
                if rid == 'atlantis':
                    raise KeyError(rid)

            def close(self):
                asked.append('closed')
        maps = [{'id': 'isle-of-man'}, {'id': 'france', 'level': 'overview', 'terrain': False}, {'id': 'atlantis'}]
        with mock.patch.object(mapbuild, 'Builder', Builder):
            b, status = self.run_build(dict(EVERYTHING, maps=maps), sources_folder='/somewhere/sources')
        self.assertEqual(asked, [('data', str(self.tmp / 'drive' / 'LastbookData'), '/somewhere/sources'), ('france', 'overview', False), ('atlantis', 'detail', True), 'closed'])
        self.assertEqual(status['phase'], 'done')
        self.assertEqual(len(status['problems']), 1)
        self.assertIn('atlantis', status['problems'][0])
        self.assertEqual(json.loads((self.tmp / 'drive' / 'LastbookData' / 'pack.json').read_text())['regions'], ['isle-of-man', 'france', 'atlantis'])


@unittest.skipUnless(video.tools() and 'libx264' in video.tools()['encoders'] and 'libx265' in video.tools()['encoders'], 'needs an ffmpeg with libx264 and libx265')
class Smaller(Temp):
    """Videos really re-encoded: a few seconds each."""

    def setUp(self):
        super().setUp()
        self.recs, self.files = buildkit.write_pack(self.tmp / 'pack', VIDEOS)
        self.data = self.tmp / 'drive' / 'LastbookData'
        self.lib = self.data / 'library-data'
        self.choice = {'documents': ['medical'], 'encyclopedias': [], 'videos': {'default': 'small'}, 'threads': 2}

    def catalog(self):
        return {r['id']: r for r in json.loads((self.lib / 'catalog.json').read_text())}

    def test_one_video_made_smaller(self):
        src = self.tmp / 'pack' / LIB / 'videos/first-aid/bandage.mp4'
        said = []
        got = video.encode(src, self.tmp / 'out' / 'bandage.mp4', 'howto', threads=2, progress=said.append)
        out = self.tmp / 'out' / 'bandage.mp4'
        self.assertFalse(got['kept'])
        self.assertEqual(got['bytes'], out.stat().st_size)
        self.assertLess(got['bytes'], 0.5 * src.stat().st_size)
        p = video.probe(out)
        self.assertEqual((p['video'], p['audio'], p['height']), ('hevc', 'aac', 480))
        self.assertAlmostEqual(p['duration'], 4, delta=0.3)
        self.assertEqual(sorted(x.name for x in (self.tmp / 'out').iterdir()), ['bandage.mp4'])       # no part, and no '._' beside it
        self.assertTrue(said and said[-1] <= 1.0)
        self.assertEqual(out.read_bytes()[4:8], b'ftyp')
        self.assertIn(b'hvc1', out.read_bytes()[:4096])                                               # the name Apple's players look for
        talk = video.encode(self.tmp / 'pack' / LIB / 'videos/creator-corporals-corner/campfire.mp4', self.tmp / 'out' / 'campfire.mp4', 'talk', threads=2)
        self.assertEqual(video.probe(self.tmp / 'out' / 'campfire.mp4')['height'], 360)
        self.assertFalse(talk['kept'])

    def test_a_result_that_is_not_smaller_is_thrown_away(self):
        small = self.tmp / 'pack' / LIB / 'videos/mathematics/lecture.mp4'           # 160 by 120 at 60 kbps: nothing to gain
        got = video.encode(small, self.tmp / 'out' / 'lecture.mp4', 'howto', threads=2)
        self.assertTrue(got['kept'])
        self.assertEqual(list((self.tmp / 'out').iterdir()), [])

    def test_what_is_not_a_video_and_being_stopped(self):
        (self.tmp / 'not.mp4').write_bytes(b'not a video at all')
        with self.assertRaisesRegex(video.Failed, 'could not be read'):
            video.encode(self.tmp / 'not.mp4', self.tmp / 'out' / 'x.mp4', 'howto')
        c = download.Control()
        c.stop('paused')
        with self.assertRaises(download.Stopped):
            video.encode(self.tmp / 'pack' / LIB / 'videos/first-aid/bandage.mp4', self.tmp / 'out' / 'y.mp4', 'howto', control=c)
        self.assertEqual([p.name for p in (self.tmp / 'out').iterdir()] if (self.tmp / 'out').exists() else [], [])
        with self.assertRaisesRegex(video.Failed, 'hardware'):
            video.command(dict(video.tools(), encoders={'libx265'}), 'a', 'b', 'howto', encoder='hardware')

    def test_a_library_with_its_videos_made_smaller(self):
        b = build.Build(self.data, build.LocalSource(self.tmp / 'pack'), self.choice)
        status = b.run()
        self.assertEqual((status['phase'], status['problems'], status['videos_done'], status['videos_total']), ('done', [], 2, 2))
        cat = self.catalog()
        was = {r['id']: r for r in self.recs}
        for rid, profile, height in (('video-bandage', 'howto', 480), ('video-campfire', 'talk', 360)):
            r, f = cat[rid], self.lib / cat[rid]['path']
            self.assertEqual((r['bytes'], r['sha256'], r['made_smaller']), (f.stat().st_size, buildkit.sha(f.read_bytes()), profile))
            self.assertLess(r['bytes'], 0.6 * was[rid]['bytes'])
            self.assertNotIn('words_only', r)
            self.assertEqual(video.probe(f)['video'], 'hevc')
            self.assertEqual(r['path'], was[rid]['path'])                                   # where it was: everything that points at it still does
        self.assertEqual((self.lib / 'videos/mathematics/lecture.mp4').read_bytes(), VIDEOS['small'])       # small already: byte for byte the original
        self.assertEqual(cat['video-lecture'], was['video-lecture'])
        listed = dict((l.split('\t')[0], l.split('\t')) for l in (self.data / 'pack-manifest.tsv').read_text().splitlines()[1:])
        row = listed[LIB + 'videos/first-aid/bandage.mp4']
        self.assertEqual((int(row[1]), row[2], row[3]), (cat['video-bandage']['bytes'], cat['video-bandage']['sha256'], 'encode'))
        self.assertIn(was['video-bandage']['sha256'], row[4])                               # and which original it was made from
        self.assertEqual(self.tmp.joinpath('pack', LIB, 'videos/first-aid/bandage.mp4').read_bytes(), VIDEOS['big'])       # the pack's own copy is untouched
        self.assertEqual(json.loads((self.lib / 'summary.json').read_text())['videos'], 3)
        with mock.patch.object(video, 'encode', side_effect=AssertionError('encoded twice')):
            again = build.Build(self.data, build.LocalSource(self.tmp / 'pack'), self.choice).run()
        self.assertEqual(again['videos_done'], 2)

    def test_the_library_opens_before_its_videos_are_done(self):
        """Stopped in the middle of the videos: the catalog is there, the one not done is shown by its words, and a later run finishes."""
        c = download.Control()
        real = video.encode
        n = [0]

        def one_then_stop(*a, **k):
            n[0] += 1
            if n[0] == 2:
                c.stop('paused')
                c.check()
            return real(*a, **k)
        with mock.patch.object(video, 'encode', one_then_stop):
            with self.assertRaises(download.Stopped):
                build.Build(self.data, build.LocalSource(self.tmp / 'pack'), self.choice, control=c).run()
        cat = self.catalog()
        waiting = [r for r in cat.values() if r.get('pending')]
        self.assertEqual(len(waiting), 2)                                                   # written before the first video: both still wait in it
        self.assertTrue(all(r['words_only'] for r in waiting))
        self.assertTrue((self.lib / 'search.sqlite').is_file())
        status = build.Build(self.data, build.LocalSource(self.tmp / 'pack'), self.choice).run()
        self.assertEqual((status['phase'], status['videos_done']), ('done', 2))
        self.assertFalse(any(r.get('pending') or r.get('words_only') for r in self.catalog().values()))
        self.assertEqual(n[0], 2)                                                           # the one finished before the stop was not done again

    def test_a_video_that_cannot_be_made_smaller_is_kept_as_it_is(self):
        with mock.patch.object(video, 'encode', side_effect=video.Failed('ffmpeg could not make the video smaller (boom).')):
            status = build.Build(self.data, build.LocalSource(self.tmp / 'pack'), self.choice).run()
        self.assertEqual(status['phase'], 'done')
        self.assertEqual(len(status['problems']), 2)
        self.assertIn('The original is kept.', status['problems'][0])
        cat = self.catalog()
        self.assertEqual((self.lib / 'videos/first-aid/bandage.mp4').read_bytes(), VIDEOS['big'])
        self.assertNotIn('words_only', cat['video-bandage'])
        self.assertNotIn('made_smaller', cat['video-bandage'])

    def test_with_no_ffmpeg_it_says_so_before_starting_on_videos(self):
        with mock.patch.object(video, 'tools', return_value=None):
            with self.assertRaisesRegex(build.Refused, 'No ffmpeg'):
                build.Build(self.data, build.LocalSource(self.tmp / 'pack'), self.choice).run()
        self.assertTrue((self.lib / 'catalog.json').is_file())                              # the rest of the library is there and opens


class FromServer(test_wizard.Base):
    """From a published pack, read in place: only what was chosen is downloaded."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(packzip, 'BIG', 64 * 1024), mock.patch.object(packzip, 'RUN_MAX', 256 * 1024), mock.patch.object(packzip, 'GAP', 4 * 1024),
                  mock.patch.object(contents, 'BIG_CHANNEL', 500_000)):
            p.start()
            self.addCleanup(p.stop)
        self.recs, files = buildkit.write_pack(self.tmp / 'made')
        self.content = files
        self.files.data['pack.zip'] = test_wizard.make_zip([(p, b, zipfile.ZIP_STORED) for p, b in files.items()])
        self.source = build.RemoteSource(f'{self.base}/pack.zip', workers=3, **self.net)
        self.data = self.tmp / 'lib' / 'LastbookData'

    def bytes_asked_for(self):
        total = 0
        for r in self.asked('pack.zip'):
            rng = r.get('Range') if isinstance(r, dict) else None
            if rng and rng.startswith('bytes='):
                a, _, b = rng[6:].partition('-')
                total += int(b) - int(a) + 1 if b else 0
        return total

    def test_a_small_choice_downloads_little(self):
        status = build.Build(self.data, self.source, {'documents': ['water'], 'encyclopedias': ['collections/small_wiki.zim'], 'videos': {'default': 'words'}}).run()
        self.assertEqual((status['phase'], status['problems']), ('done', []))
        lib = self.data / 'library-data'
        self.assertEqual((lib / 'documents/water-sanitation/water.pdf').read_bytes(), self.content[LIB + 'documents/water-sanitation/water.pdf'])
        self.assertEqual((lib / 'collections/small_wiki.zim').read_bytes(), self.content[LIB + 'collections/small_wiki.zim'])
        self.assertFalse((lib / 'collections/big_wiki.zim').exists())
        self.assertFalse(any(lib.rglob('*.mp4')))
        ids = {r['id'] for r in json.loads((lib / 'catalog.json').read_text())}
        self.assertEqual(ids, {'water-guide', 'small-wiki', 'video-bandage', 'metadata-video-bandage', 'transcript-bandage', 'video-campfire', 'metadata-video-campfire',
                               'transcript-campfire', 'video-lecture', 'metadata-video-lecture'})
        db = sqlite3.connect(lib / 'search.sqlite')
        self.assertEqual(db.execute("SELECT COUNT(DISTINCT doc_id) FROM pages").fetchone()[0], 6)
        db.close()
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ['derived', 'library-data', 'pack-manifest.tsv', 'pack.json'])      # no scratch, no puller's state left
        whole = len(self.files.data['pack.zip'])
        self.assertLess(self.bytes_asked_for(), 0.45 * whole)                               # the videos and the large encyclopedia never crossed the line

    def test_everything_arrives_and_is_what_the_manifest_says(self):
        status = build.Build(self.data, self.source, dict(EVERYTHING)).run()
        self.assertEqual((status['phase'], status['problems']), ('done', []))
        for path, body in self.content.items():
            if path.startswith(LIB) and path.rsplit('/', 1)[-1] not in ('catalog.json', 'search.sqlite', 'summary.json'):
                self.assertEqual((self.data / path).read_bytes(), body, path)
        self.assertFalse((self.data / 'derived' / 'geo').exists())

    def test_a_file_changed_on_the_way_is_not_kept(self):
        bad = dict(self.content)
        bad[LIB + 'documents/water-sanitation/water.pdf'] = b'%PDF-1.4 ' + b'X' * 4000          # the same size, other bytes; the manifest still says the true ones
        self.files.data['pack.zip'] = test_wizard.make_zip([(p, b, zipfile.ZIP_STORED) for p, b in bad.items()])
        status = build.Build(self.data, self.source, {'documents': ['water', 'medical'], 'encyclopedias': [], 'videos': {}}).run()
        self.assertEqual(status['phase'], 'done')
        self.assertEqual(status['problems'], ['library-data/documents/water-sanitation/water.pdf could not be had from the pack.'])
        ids = {r['id'] for r in json.loads((self.data / 'library-data' / 'catalog.json').read_text())}
        self.assertEqual(ids, {'first-aid-manual'})
        self.assertFalse((self.data / 'library-data/documents/water-sanitation/water.pdf').exists())


if __name__ == '__main__':
    unittest.main()
