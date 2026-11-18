#!/usr/bin/env python3
"""Making a region from OpenStreetMap data: an extract read into map.sqlite, and a whole region built and put in place.

  PYTHONDONTWRITEBYTECODE=1 <build python> -B app/tests/test_mapbuild.py [-v]

Needs osmium (pyosmium), which the app itself does not carry: with the app's own Python every test here is skipped.
The extracts are small .osm files written by regionkit.py; nothing is downloaded. Never packaged."""
import json, os, shutil, sqlite3, sys, tempfile, unittest, zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
try:
    import osmium  # noqa: F401
    import numpy as np
except ImportError:
    osmium = None
from engine import regions  # noqa: E402
from engine.mapbuild import build, catalog, schema, sources  # noqa: E402
import regionkit  # noqa: E402

# A town square: a road through it, a motorway beside it, a building, a lake with an island in it, places and help points.
NODES = [
    (1, -4.5000, 54.2000, None), (2, -4.4998, 54.2000, None), (3, -4.4998, 54.2002, None), (4, -4.5000, 54.2002, None),      # a building
    (10, -4.5100, 54.2000, None), (11, -4.5050, 54.2010, None), (12, -4.4950, 54.2030, None),                               # a road
    (20, -4.5020, 54.2005, {'place': 'town', 'name': 'Testby', 'population': '9000'}),
    (21, -4.5010, 54.2006, {'amenity': 'hospital', 'name': 'Testby General'}),
    (22, -4.5011, 54.2007, {'emergency': 'defibrillator', 'shop': 'convenience'}),
    (23, -4.5012, 54.2008, {'amenity': 'shelter', 'shelter_type': 'public_transport'}),
    (24, -4.5013, 54.2009, {'amenity': 'toilets'}),
    (25, -4.4000, 54.3000, {'place': 'hamlet', 'name': 'Nether Test'}),
    (26, -4.3000, 54.3500, {'place': 'city', 'name': 'Bigton', 'capital': 'yes'}),
    (30, -4.5300, 54.2100, None), (31, -4.5100, 54.2100, None), (32, -4.5100, 54.2200, None), (33, -4.5300, 54.2200, None),  # a lake's shore
    (34, -4.5220, 54.2140, None), (35, -4.5180, 54.2140, None), (36, -4.5180, 54.2160, None), (37, -4.5220, 54.2160, None),  # the island in it
    (40, -4.4900, 54.1900, None), (41, -4.4890, 54.1900, None), (42, -4.4890, 54.1910, None), (43, -4.4900, 54.1910, None),  # a clinic's building
    (50, -4.6000, 54.1000, None), (51, -4.3000, 54.4000, None),
    (60, -4.4500, 54.2600, {'natural': 'peak', 'name': 'Test Fell', 'ele': '621'}),
    (61, -4.4600, 54.2700, {'natural': 'peak', 'name': 'Μύτικας', 'name:en': 'Mytikas', 'ele': '2,917 m', 'wikidata': 'Q1'}),
    (62, -4.4700, 54.2800, {'natural': 'peak', 'ele': '300'}),                                                               # no name: nothing to find
    (63, -4.5014, 54.2010, {'amenity': 'pharmacy', 'name': 'Old Pharmacy', 'disused': 'yes'}),
    (64, -4.5015, 54.2011, {'place': 'village', 'name': 'München', 'name:en': 'Munich'}),
    (65, -4.5016, 54.2012, {'place': 'village', 'name': 'Zürich', 'name:en': 'Zurich'}),
    (70, -4.7000, 54.0500, None), (71, -4.9000, 54.0000, None),
]
WAYS = [
    (100, [10, 11, 12], {'highway': 'residential', 'name': 'High Street'}),
    (101, [50, 51], {'highway': 'motorway', 'ref': 'M1'}),
    (102, [1, 2, 3, 4, 1], {'building': 'yes'}),
    (103, [30, 31, 32, 33, 30], None),
    (104, [34, 35, 36, 37, 34], None),
    (105, [10, 999], {'highway': 'service'}),                                  # runs off the edge of the extract: its second node is not here
    (106, [40, 41, 42, 43, 40], {'building': 'yes', 'amenity': 'clinic', 'name': 'Harbour Clinic'}),
    (107, [11, 12], {'highway': 'footway'}),
    (108, [10, 11], {'railway': 'rail'}),
    (109, [10, 12], {'waterway': 'stream', 'name': 'Test Burn'}),
    (110, [50, 51], {'highway': 'proposed'}),                                  # not a road yet
    (111, [70, 71], {'route': 'ferry', 'name': 'Testby to Elsewhere'}),
    (112, [50, 51], {'boundary': 'administrative', 'admin_level': '2'}),
    (113, [10, 12], {'boundary': 'administrative', 'admin_level': '4'}),
    (114, [70, 71], {'boundary': 'administrative', 'admin_level': '2', 'maritime': 'yes'}),      # the edge of territorial waters: not drawn
    (115, [10, 11], {'boundary': 'administrative', 'admin_level': '8'}),                         # a parish
]
RELATIONS = [(200, [(103, 'outer'), (104, 'inner')], {'type': 'multipolygon', 'natural': 'water', 'name': 'Loch Test'})]


def rows(path, sql, *args):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


@unittest.skipUnless(osmium, 'needs osmium (pyosmium): run with the build environment')
class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-mapbuild-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.osm = regionkit.write_osm(self.tmp / 'town.osm', NODES, WAYS, RELATIONS)

    def read(self, level, **more):
        from engine.mapbuild import osm
        out = self.tmp / f'{level}{len(more)}.sqlite'
        return out, osm.build(self.osm, out, level=level, **more)


class Extract(Temp):
    def test_in_detail_everything_is_kept(self):
        out, made = self.read('detail')
        roads = dict(rows(out, "SELECT cls, name FROM feat WHERE layer='road'"))
        self.assertEqual(roads, {'residential': 'High Street', 'motorway': 'M1', 'footway': None})
        self.assertEqual(rows(out, "SELECT COUNT(*) FROM feat WHERE layer='bldg'"), [(2,)])
        self.assertEqual(rows(out, "SELECT cls, name FROM feat WHERE layer='rail'"), [('rail', None)])
        self.assertEqual(sorted(rows(out, "SELECT cls, name FROM feat WHERE layer='waterway'")), [('ferry', 'Testby to Elsewhere'), ('stream', 'Test Burn')])
        self.assertEqual(sorted(rows(out, "SELECT cls, minz FROM feat WHERE layer='border'")), [('country', 4), ('region', 7)])     # not the line at sea, not the parish
        lake = rows(out, "SELECT cls, name, geom FROM feat WHERE layer='water'")
        self.assertEqual([r[:2] for r in lake], [('water', 'Loch Test')])
        self.assertEqual([len(p) for p in schema.unpack(lake[0][2])], [5, 5])          # the shore, and the island as a hole in it
        places = {name: (cls, minz) for cls, name, minz in rows(out, "SELECT cls, name, minz FROM feat WHERE layer='place'")}
        self.assertEqual(places, {'Testby': ('town', 9), 'Nether Test': ('hamlet', 14), 'Bigton': ('city', 5),      # a capital shows from far out
                                  'Test Fell (621 m)': ('peak', 11), 'Μύτικας (Mytikas) (2,917 m)': ('peak', 9),      # summits, the highest from furthest out
                                  'München (Munich)': ('village', 11), 'Zürich': ('village', 11)})                   # its own name, and the English one where that is another word
        pois = sorted(rows(out, 'SELECT cls, name FROM poi'), key=str)
        self.assertEqual(pois, [('clinic', 'Harbour Clinic'), ('defibrillator', None), ('hospital', 'Testby General'), ('toilet', None)])
        clinic = rows(out, "SELECT lon, lat FROM poi WHERE cls='clinic'")[0]
        self.assertAlmostEqual(clinic[0], -4.4895, places=4)                           # the middle of its building
        self.assertAlmostEqual(clinic[1], 54.1905, places=4)
        self.assertEqual(made['unreadable'], 1)                                        # the way with a node missing
        self.assertEqual((made['roads'], made['buildings'], made['pois'], made['places']), (3, 2, 4, 7))     # the pharmacy mapped as closed is not a help point
        self.assertGreater(made['cells'], 0)
        self.assertEqual(rows(out, 'SELECT COUNT(*) FROM cover')[0][0], made['cells'])
        meta = dict(rows(out, 'SELECT key, value FROM meta'))
        self.assertEqual(meta['level'], 'detail')
        self.assertIn('OpenStreetMap', meta['source'])
        self.assertEqual(rows(out, 'SELECT COUNT(*) FROM feat')[0], rows(out, 'SELECT COUNT(*) FROM feat_idx')[0])
        self.assertEqual({r[0] for r in rows(out, 'SELECT DISTINCT maxz FROM feat_idx')}, {schema.MAXZ})

    def test_an_overview_keeps_what_a_continent_can_carry(self):
        out, made = self.read('overview')
        self.assertEqual(rows(out, "SELECT cls, name FROM feat WHERE layer='road'"), [('motorway', 'M1')])
        self.assertEqual(rows(out, "SELECT COUNT(*) FROM feat WHERE layer='bldg'"), [(0,)])
        self.assertEqual(rows(out, "SELECT cls FROM feat WHERE layer='waterway'"), [('ferry',)])                              # how to cross the water; not the stream
        self.assertEqual(rows(out, "SELECT cls FROM feat WHERE layer='border'"), [('country',)])
        self.assertEqual(sorted(r[0] for r in rows(out, "SELECT name FROM feat WHERE layer='place'")),
                         ['Bigton', 'München (Munich)', 'Testby', 'Zürich', 'Μύτικας (Mytikas) (2,917 m)'])                  # the summit the world knows; not every fell
        self.assertEqual(sorted(r[0] for r in rows(out, 'SELECT cls FROM poi')), ['clinic', 'defibrillator', 'hospital'])     # every help point with a layer; not the toilet
        self.assertEqual(rows(out, "SELECT name FROM feat WHERE layer='water'"), [('Loch Test',)])
        self.assertEqual(made['cells'], 0)                                             # an overview claims no ground as its own
        self.assertEqual(rows(out, 'SELECT COUNT(*) FROM cover'), [(0,)])

    def test_buildings_can_be_left_out(self):
        out, made = self.read('detail', buildings=False)
        self.assertEqual(rows(out, "SELECT COUNT(*) FROM feat WHERE layer='bldg'"), [(0,)])
        self.assertEqual(rows(out, "SELECT cls FROM poi WHERE name='Harbour Clinic'"), [('clinic',)])       # the clinic is still a help point

    def test_an_extract_is_never_read_over_a_map_already_there(self):
        from engine.mapbuild import osm
        out, _ = self.read('detail')
        with self.assertRaises(ValueError):
            osm.build(self.osm, out)

    def test_a_region_given_as_several_extracts_holds_each_thing_once(self):
        from engine.mapbuild import osm
        east = regionkit.write_osm(self.tmp / 'east.osm', [n for n in NODES if n[0] in (10, 11, 12, 20, 21)] + [(60, -4.2, 54.25, {'place': 'village', 'name': 'Eastby'})],
                                   [w for w in WAYS if w[0] == 100], [])
        out = self.tmp / 'two.sqlite'
        made = osm.build([self.osm, east], out, node_index=str(self.tmp / 'nodes'))
        self.assertEqual(rows(out, "SELECT COUNT(*) FROM feat WHERE layer='place' AND name='Testby'"), [(1,)])      # in both extracts: once
        self.assertEqual(rows(out, "SELECT COUNT(*) FROM poi WHERE cls='hospital'"), [(1,)])
        self.assertEqual(rows(out, "SELECT COUNT(*) FROM feat WHERE layer='place' AND name='Eastby'"), [(1,)])
        self.assertEqual(made['pois'], 4)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith('nodes')], [])                    # the node files are gone

    def test_many_lines_that_cannot_be_placed_stop_the_build(self):
        from engine.mapbuild import osm
        broken = regionkit.write_osm(self.tmp / 'broken.osm', NODES, WAYS + [(1000 + i, [10, 5000 + i], {'highway': 'residential'}) for i in range(300)], [])
        with self.assertRaisesRegex(RuntimeError, 'could not be placed'):
            osm.build(broken, self.tmp / 'broken.sqlite')


class Fake(sources.Sources):
    """Everything a build would fetch, from files already here; anything else is 'not published'."""

    def __init__(self, folder, files):
        super().__init__(folder)
        self.files, self.asked = files, []

    def get(self, url, name, missing_ok=False, max_age_days=None):
        self.asked.append(name)
        if name in self.files:
            return self.files[name]
        if missing_ok:
            return None
        raise AssertionError(f'asked for {name}')

    def source_note(self, name):
        return {'url': 'https://example.invalid/' + name, 'size': 1}


class Land:
    def pieces(self, minx, miny, maxx, maxy):
        yield -4.7, 54.0, -4.2, 54.5, np.array([[-4.7, 54.0], [-4.2, 54.0], [-4.2, 54.5], [-4.7, 54.5], [-4.7, 54.0]])

    def close(self):
        pass


INDEX = {'features': [{'properties': {'id': 'testland', 'parent': 'europe', 'name': 'Testland', 'iso3166-1:alpha2': ['TL'],
                                      'urls': {'pbf': 'https://download.geofabrik.de/europe/testland-latest.osm.pbf'}},
                       'geometry': {'type': 'Polygon', 'coordinates': [[[-4.8, 53.9], [-4.1, 53.9], [-4.1, 54.6], [-4.8, 54.6], [-4.8, 53.9]]]}}]}


class Whole(Temp):
    def builder(self, dem=True, postcodes=True):
        files = {'geofabrik/testland.osm.pbf': self.osm}
        if postcodes:
            z = self.tmp / 'TL.zip'
            with zipfile.ZipFile(z, 'w') as f:
                f.writestr('TL.txt', 'TL\tTB1\tTestby\tTestshire\tTS\t\t\t\t\t54.2005\t-4.5020\t4\n')
            files['geonames/TL.zip'] = z
        if dem:
            n = 120
            a = np.tile(np.linspace(0, 600, n, dtype=np.float32), (n, 1))
            url = sources.dem_url(54, -5)
            files['dem30/' + url.rsplit('/', 1)[1]] = regionkit.write_geotiff(self.tmp / 'cell.tif', a, -5.0, 55.0, 1 / n, 1 / n)
        b = build.Builder(self.tmp / 'LastbookData', self.tmp / 'src', workers=1)
        b.src = Fake(self.tmp / 'src', files)
        b._catalog = catalog.Catalog(INDEX)
        b._land = Land()
        return b

    def test_a_region_from_nothing_that_the_app_then_reads(self):
        b = self.builder()
        self.assertIsNone(b.built('testland'))
        info = b.build('testland')
        root = self.tmp / 'LastbookData' / 'derived' / 'geo' / 'regions'
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['testland'])                       # nothing half-made left beside it
        self.assertEqual(sorted(p.name for p in (root / 'testland').iterdir()), ['map.sqlite', 'places.sqlite', 'region.json', 'terrain.sqlite'])
        self.assertEqual(json.loads((root / 'testland' / 'region.json').read_text()), info)
        self.assertEqual((info['id'], info['name'], info['level'], info['iso'], info['box']), ('testland', 'Testland', 'detail', ['TL'], [-4.8, 53.9, -4.1, 54.6]))
        self.assertEqual(info['counts']['finder'], {'places': 7, 'points': 2, 'streets': 1, 'postcodes': 1})
        self.assertEqual(info['builder'], build.BUILDER)
        self.assertTrue(b.current('testland', 'detail'))
        self.assertFalse(b.current('testland', 'overview'))
        self.assertEqual(info['counts']['coast']['ocean'], 1)
        self.assertGreater(info['counts']['terrain']['tiles'], 0)
        self.assertEqual(len(info['credits']), 4)
        self.assertEqual(set(info['bytes']), {'map.sqlite', 'places.sqlite', 'terrain.sqlite'})
        self.assertEqual(b.built('testland')['built'], info['built'])
        atlas = regions.Atlas(root)
        r = atlas.regions()[0]
        self.assertEqual((r.id, r.level, r.has_places, r.has_terrain), ('testland', 'detail', True, True))
        self.assertTrue(atlas.contains(-4.5, 54.2))
        self.assertFalse(atlas.contains(-3.0, 54.2))
        layers = {row[0] for row in atlas.rows(15, -4.52, 54.19, -4.48, 54.21)}
        self.assertEqual(layers, {'road', 'bldg', 'place', 'ocean', 'island', 'rail', 'waterway', 'water', 'border'})
        self.assertTrue(atlas.covered(-4.5, 54.2))
        self.assertIsNotNone(atlas.terrain(9, 249, 163))
        found = sqlite3.connect(root / 'testland' / 'places.sqlite')
        self.assertEqual(found.execute("SELECT kind, detail FROM place WHERE name='TB1'").fetchone(), ('postcode', 'Testby, Testshire'))
        found.close()

    def test_no_elevation_no_postcodes_published_is_a_region_without_them(self):
        info = self.builder(dem=False, postcodes=False).build('testland', level='overview')
        folder = self.tmp / 'LastbookData' / 'derived' / 'geo' / 'regions' / 'testland'
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['map.sqlite', 'places.sqlite', 'region.json'])
        self.assertEqual(info['level'], 'overview')
        self.assertNotIn('terrain', info['counts'])
        self.assertEqual(len(info['credits']), 2)
        self.assertEqual(info['counts']['finder']['postcodes'], 0)

    def test_a_build_that_fails_leaves_the_map_that_was_there(self):
        b = self.builder(dem=False)
        first = b.build('testland')
        from engine.mapbuild import places
        with mock.patch.object(places, 'build', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                b.build('testland')
        self.assertEqual(b.built('testland')['built'], first['built'])
        root = self.tmp / 'LastbookData' / 'derived' / 'geo' / 'regions'
        self.assertEqual([r.id for r in regions.Atlas(root).regions()], ['testland'])                # what was left half-made is not seen
        again = b.build('testland', level='overview')                                                # and the next build starts clean
        self.assertEqual(again['level'], 'overview')
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['testland'])
        self.assertTrue(b.remove('testland'))
        self.assertFalse(b.remove('testland'))
        self.assertEqual(list(root.iterdir()), [])

    def test_a_wrong_name_or_level_is_refused_before_anything_is_fetched(self):
        b = self.builder()
        with self.assertRaises(KeyError):
            b.build('atlantis')
        with self.assertRaises(ValueError):
            b.build('testland', level='everything')
        self.assertEqual(b.src.asked, [])

    def test_the_memory_mapped_node_file_is_never_put_beside_the_library(self):
        """What was measured is in build.scratch's own words: pages written through a memory map to an exFAT drive were lost."""
        with mock.patch.dict(os.environ, {'LASTBOOK_SCRATCH': ''}):
            self.assertEqual(build.scratch(), Path(tempfile.gettempdir()))
        with mock.patch.dict(os.environ, {'LASTBOOK_SCRATCH': str(self.tmp / 'own')}):
            self.assertEqual(build.scratch(), self.tmp / 'own')
        seen = {}
        from engine.mapbuild import osm
        real = osm.build

        def spy(pbf, out, **kw):
            seen.update(kw)
            return real(pbf, out, **{**kw, 'node_index': None})
        b = self.builder(dem=False)
        with mock.patch.object(build, 'BIG_PBF', 0), mock.patch.object(osm, 'build', spy):
            b.build('testland')
        self.assertTrue(seen['node_index'].startswith(str(Path(tempfile.gettempdir()))))
        self.assertNotIn(str(self.tmp / 'LastbookData'), seen['node_index'])

    @unittest.skipIf(os.name == 'nt', 'asks whether a process is there the POSIX way')
    def test_what_a_killed_build_left_is_deleted_before_the_next(self):
        import subprocess
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        own = self.tmp / 'scratch'
        own.mkdir()
        names = {'dead': f'lastbook-nodes-{gone.pid}-poland.0', 'mine': f'lastbook-nodes-{os.getpid()}-france.0', 'another': 'lastbook-nodes-1-spain.0',
                 'odd': 'lastbook-nodes-notanumber-x.0', 'other': 'something-else.bin'}
        for n in names.values():
            (own / n).write_bytes(b'x' * 10)
        with mock.patch.dict(os.environ, {'LASTBOOK_SCRATCH': str(own)}):
            build._forget_stale_nodes()
        left = sorted(p.name for p in own.iterdir())
        self.assertNotIn(names['dead'], left)                       # its process is gone: several gigabytes nobody will come back for
        for kept in ('mine', 'another', 'odd', 'other'):           # one in use, one whose process is someone's, and what is not a node file at all
            self.assertIn(names[kept], left)
        with mock.patch.dict(os.environ, {'LASTBOOK_SCRATCH': str(self.tmp / 'not-there')}):
            build._forget_stale_nodes()                             # nothing to look in: nothing happens


if __name__ == '__main__':
    unittest.main()
