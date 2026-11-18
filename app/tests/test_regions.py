#!/usr/bin/env python3
"""Maps of anywhere, side by side: how the app reads derived/geo/regions/<id>/, and the parts of making one that need
no OpenStreetMap reader (outlines, coast, place finder, elevation, the catalog of regions).

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_regions.py [-v]

Regions here are written by hand (regionkit.py) into a temporary folder; the made-up library is derived/app-build/testdata.
Reading an .osm.pbf is in test_mapbuild.py, which needs osmium. Never packaged."""
import json, os, shutil, sqlite3, sys, tempfile, time, unittest, zipfile
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
import numpy as np  # noqa: E402
from engine import regions, address_search  # noqa: E402
from engine.mapbuild import catalog, coast, geom, schema, sources, terrain  # noqa: E402
from engine.mapbuild import places as place_finder  # noqa: E402
import regionkit  # noqa: E402
import server  # noqa: E402
from test_portable import ask  # noqa: E402

SQUARE = [[[-4.8, 54.0], [-4.2, 54.0], [-4.2, 54.5], [-4.8, 54.5], [-4.8, 54.0]]]     # an island's outline, as one polygon
FAR = [[[2.0, 48.0], [3.0, 48.0], [3.0, 49.0], [2.0, 49.0], [2.0, 48.0]]]
ROAD = [[-4.50, 54.20, -4.49, 54.21, -4.48, 54.215]]


def island(root, rid='isle', level='detail', **more):
    """A small region: a town, a village, two streets, a hospital, a defibrillator, a tap."""
    feats = [('place', 'town', 'Port Test', 9, 30, [[-4.50, 54.20]]),
             ('place', 'village', 'Little Test', 11, 30, [[-4.40, 54.30]]),
             ('road', 'residential', 'Harbour Road', 13, 30, ROAD),
             ('road', 'primary', 'A1', 8, 30, [[-4.6, 54.1, -4.3, 54.4]]),
             ('road', 'residential', 'Mill Lane', 13, 30, [[-4.41, 54.30, -4.40, 54.305]]),
             ('island', '', None, 5, 9, [[-4.8, 54.0, -4.2, 54.0, -4.2, 54.5, -4.8, 54.5, -4.8, 54.0]]),
             ('island', '', None, 10, 30, [[-4.8, 54.0, -4.5, 53.99, -4.2, 54.0, -4.2, 54.5, -4.8, 54.5, -4.8, 54.0]])]
    pois = [('hospital', 'Port Test Hospital', -4.495, 54.205), ('defibrillator', None, -4.499, 54.201), ('drinking_water', 'Quay tap', -4.501, 54.199)]
    more.setdefault('outline', [SQUARE])
    return regionkit.make_region(root, rid, level=level, name=more.pop('name', 'Isle of Test'), feats=more.pop('feats', feats), pois=more.pop('pois', pois), **more)


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-regions-'))
        self.root = self.tmp / 'regions'
        self.root.mkdir()
        self.atlas = regions.Atlas(self.root)
        p = mock.patch.object(regions, 'RESCAN', 0)      # look at the folder every time: these tests change it and ask at once
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def serve(self):
        """The engine reading these regions instead of the fixture library's (it has none), and with no US map beside them."""
        for p in (mock.patch.object(server, 'ATLAS', self.atlas), mock.patch.object(server, 'national_status', return_value={'state': 'not-built'})):
            p.start()
            self.addCleanup(p.stop)
        regions._stores.clear()


class Folder(Temp):
    def test_a_region_is_a_folder_and_nothing_else_is_one(self):
        self.assertFalse(self.atlas)
        island(self.root)
        (self.root / '.building-next').mkdir()
        shutil.copytree(self.root / 'isle', self.root / '.old-isle-1')
        (self.root / 'not-a-region').mkdir()
        (self.root / 'half').mkdir()
        (self.root / 'half' / 'region.json').write_text('{"id": "half"}')            # no map beside it
        (self.root / 'stray.txt').write_text('x')
        broken = island(self.root, 'broken')
        (broken / 'region.json').write_text('{not json')
        self.assertEqual([r.id for r in self.atlas.regions()], ['isle'])
        r = self.atlas.regions()[0]
        self.assertEqual((r.name, r.level), ('Isle of Test', 'detail'))
        self.assertTrue(r.has_places)
        self.assertFalse(r.has_terrain)

    def test_added_and_taken_away_with_no_restart(self):
        island(self.root)
        first = self.atlas.version()
        self.assertEqual(len(self.atlas.regions()), 1)
        island(self.root, 'mainland', level='overview', name='Mainland', outline=[FAR], feats=[('place', 'city', 'Farville', 6, 30, [[2.5, 48.5]])], pois=[])
        self.assertEqual([r.id for r in self.atlas.regions()], ['isle', 'mainland'])     # the detailed one first
        second = self.atlas.version()
        self.assertNotEqual(first, second)
        shutil.rmtree(self.root / 'isle')
        self.assertEqual([r.id for r in self.atlas.regions()], ['mainland'])
        self.assertNotEqual(self.atlas.version(), second)
        shutil.rmtree(self.root / 'mainland')
        self.assertFalse(self.atlas)
        self.assertEqual(self.atlas.version(), 'r0')

    def test_a_point_is_on_a_region_by_its_outline_not_its_box(self):
        l_shape = [[[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2], [0, 0]]]
        island(self.root, 'ell', outline=[l_shape], box=[0, 0, 2, 2], feats=[('place', 'town', 'Corner', 9, 30, [[0.5, 0.5]])], pois=[])
        self.assertTrue(self.atlas.contains(0.5, 1.5))
        self.assertFalse(self.atlas.contains(1.5, 1.5))      # inside the box, outside the outline
        self.assertFalse(self.atlas.contains(3, 3))
        self.assertEqual(self.atlas.level_at(1.5, 0.5), 'detail')
        self.assertIsNone(self.atlas.level_at(1.5, 1.5))

    def test_the_whole_of_what_is_here(self):
        self.assertIsNone(self.atlas.bounds())
        island(self.root)
        island(self.root, 'mainland', level='overview', name='Mainland', outline=[FAR], box=[2, 48, 3, 49], feats=[('place', 'city', 'Farville', 6, 30, [[2.5, 48.5]])],
               pois=[], data_as_of='2026-10-01T00:00:00Z')
        b = self.atlas.bounds()
        self.assertLess(b[0], -4.5)
        self.assertEqual((b[1], b[2]), (48, 3))
        self.assertGreater(b[3], 54.4)
        self.assertEqual(self.atlas.data_as_of(), ('2026-10-01', '2026-11-17'))
        self.assertEqual(self.atlas.credits(), ['Map data © OpenStreetMap contributors'])
        self.assertEqual({s['id']: s['level'] for s in self.atlas.summary()}, {'isle': 'detail', 'mainland': 'overview'})


class Tiles(Temp):
    def test_a_feature_is_drawn_between_its_first_and_last_zoom(self):
        island(self.root)
        def layers(z):
            return sorted((r[0], r[2]) for r in self.atlas.rows(z, -5, 53.9, -4, 54.6) if r[0] != 'road')
        self.assertEqual(layers(4), [])
        self.assertEqual(len([x for x in layers(7) if x[0] == 'island']), 1)     # the coarse coast only
        self.assertEqual(len([x for x in layers(12) if x[0] == 'island']), 1)    # the fine coast only
        self.assertIn(('place', 'Port Test'), layers(9))
        self.assertNotIn(('place', 'Little Test'), layers(9))
        self.assertIn(('place', 'Little Test'), layers(11))
        self.assertEqual([r[2] for r in self.atlas.rows(8, -5, 53.9, -4, 54.6) if r[0] == 'road'], ['A1'])
        self.assertEqual(self.atlas.rows(14, 10, 10, 11, 11), [])               # nowhere near

    def test_an_overview_is_not_drawn_over_a_detailed_region(self):
        island(self.root)
        island(self.root, 'around', level='overview', name='Around', outline=[[[[-6, 53], [-3, 53], [-3, 55], [-6, 55], [-6, 53]]]], box=[-6, 53, -3, 55],
               feats=[('road', 'primary', 'A1 (overview copy)', 8, 30, [[-4.6, 54.1, -4.3, 54.4]]), ('road', 'primary', 'B9', 8, 30, [[-5.5, 53.5, -5.4, 53.6]])],
               pois=[('hospital', 'Port Test Hospital', -4.495, 54.205), ('hospital', 'Far Hospital', -5.5, 53.5)])
        inside = (-4.499, 54.201, -4.496, 54.204)       # a street-level tile wholly in cells the island maps
        names = [r[2] for r in self.atlas.rows(15, *inside) if r[0] == 'road']
        self.assertIn('A1', names)
        self.assertNotIn('A1 (overview copy)', names)
        wide = [r[2] for r in self.atlas.rows(8, -6, 53, -3, 55) if r[0] == 'road']
        self.assertIn('A1 (overview copy)', wide)        # zoomed out, both are there, one over the other
        self.assertIn('B9', wide)
        self.assertTrue(self.atlas.covered(-4.495, 54.205))
        self.assertFalse(self.atlas.covered(-5.5, 53.5))

    def test_the_app_draws_regions_where_no_pack_map_is(self):
        self.serve()
        self.assertEqual(json.loads(server._map_tile(12, 1997, 1305, 'none')), {'error': 'map not built'})
        island(self.root)
        z, x, y = 12, int((-4.49 + 180) / 360 * 4096), 1305
        import math
        s = math.sin(math.radians(54.205))
        y = int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * 4096)
        d = json.loads(server.map_tile(z, x, y))
        self.assertEqual(d['z'], 12)
        self.assertIn('island', d['layers'])
        self.assertIn('Port Test', [f[1] for f in d['layers'].get('place', [])])
        self.assertIn('A1', [f[1] for f in d['layers']['road']])
        status, _, body = ask('GET', f'/api/map/tile/{z}/{x}/{y}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['z'], 12)

    def test_zoomed_out_tiles_are_kept_in_one_file_and_made_once(self):
        self.serve()
        island(self.root)
        first = server.map_tile(7, 62, 40)
        self.assertTrue((self.root / '.tiles.sqlite').is_file())
        self.assertEqual([p.name for p in self.root.iterdir() if p.suffix == '.json'], [])
        with mock.patch.object(server, '_map_tile', side_effect=AssertionError('made twice')):
            self.assertEqual(server.map_tile(7, 62, 40), first)
        self.assertEqual([r.id for r in self.atlas.regions()], ['isle'])          # the store beside the regions is not taken for one
        island(self.root, 'second', name='Second', outline=[FAR], feats=[('place', 'city', 'Farville', 6, 30, [[2.5, 48.5]])], pois=[])
        with mock.patch.object(server, '_map_tile', return_value=b'{"z":7,"layers":{}}') as made:
            server.map_tile(7, 62, 40)                                           # the map changed: what was kept is not served
        self.assertEqual(made.call_count, 1)

    def test_a_drive_that_cannot_be_written_goes_without_the_store(self):
        store = regions.TileStore(self.tmp / 'no-such-folder' / 'deeper' / '.tiles.sqlite')
        with mock.patch('engine.sqlitefs.Database', side_effect=sqlite3.OperationalError('unable to open database file')):
            self.assertIsNone(store.get('v', 1, 2, 3))
            store.put('v', 1, 2, 3, b'{}')
        self.assertTrue(store.off)


class Points(Temp):
    def both(self):
        island(self.root)
        island(self.root, 'around', level='overview', name='Around', outline=[[[[-6, 53], [-3, 53], [-3, 55], [-6, 55], [-6, 53]]]], box=[-6, 53, -3, 55],
               feats=[], pois=[('hospital', 'Port Test Hospital', -4.495, 54.205), ('hospital', 'Far Hospital', -5.5, 53.5), ('fuel', 'Island Fuel', -4.497, 54.203)])
        self.serve()

    def test_a_point_two_regions_hold_is_counted_once(self):
        self.both()
        got = server.pins(-6, 53, -3, 55, 16, ['hospital'])
        self.assertEqual(got['sites'], 2)
        names = sorted(p['name'] for p in got['points'])
        self.assertEqual(names, ['Far Hospital', 'Port Test Hospital'])
        listed = server.members(-6, 53, -3, 55, ['hospital'], 54.2, -4.5)
        self.assertEqual(sorted(i['name'] for i in listed['items']), ['Far Hospital', 'Port Test Hospital'])

    def test_what_only_the_overview_holds_is_lost_where_detail_covers(self):
        """The rule is by place, not by kind: a detailed region is taken to hold everything in the cells it maps."""
        self.both()
        self.assertEqual(server.pins(-6, 53, -3, 55, 16, ['fuel'])['sites'], 0)

    def test_nearest_help_comes_from_regions(self):
        self.both()
        got = server.near(54.2, -4.5, ['hospital', 'defibrillator', 'drinking_water'], 3)
        self.assertEqual(got[0]['cls'], 'defibrillator')
        self.assertEqual(got[0]['label'], 'Defibrillator (AED)')
        self.assertEqual([p['name'] for p in got if p['cls'] == 'hospital'], ['Port Test Hospital', 'Far Hospital'])
        self.assertLess(got[0]['km'], 0.2)

    def test_every_kind_of_point_a_region_can_hold_has_a_name_and_a_layer(self):
        kinds = set(schema.POI.values()) | {'comms_tower'}
        self.assertEqual(kinds - set(server.HELP), set())
        page = (ROOT / 'web' / 'map.js').read_text(encoding='utf-8')
        for cls in sorted(schema.OVERVIEW_POI):
            self.assertRegex(page, r"'[^']*\b%s\b[^']*', '(help|water|supply)'" % cls, cls)
        for new in ('water_well', 'defibrillator', 'ambulance_station', 'assembly_point'):
            self.assertIn(new, server.HELP)


class Finder(Temp):
    def test_towns_streets_and_points_of_a_region_are_found(self):
        island(self.root)
        self.serve()
        got = server.places('port test')
        self.assertEqual((got[0]['name'], got[0]['kind'], got[0]['detail']), ('Port Test', 'town', 'Isle of Test'))
        self.assertTrue(got[0]['onMap'])
        self.assertIn('Port Test Hospital', [p['name'] for p in got])
        got = server.places('harbour road')
        self.assertEqual((got[0]['name'], got[0]['kind'], got[0]['detail']), ('Harbour Road', 'street', 'near Port Test, Isle of Test'))
        self.assertEqual(server.places('little test')[0]['detail'], 'near Port Test, Isle of Test')
        self.assertEqual(server.places('zzzz nothing'), [])
        self.assertEqual(server.places('   '), [])
        status, _, body = ask('GET', '/api/places?q=mill+lane')
        self.assertEqual((status, json.loads(body)[0]['name']), (200, 'Mill Lane'))

    def test_an_address_gives_its_street_and_says_the_house_was_not_found(self):
        island(self.root)
        self.serve()
        for q in ('12 Harbour Road', '12a Harbour Road, Port Test', 'Harbour Road 12', '10-12 Harbour Road'):
            got = server.places(q)
            self.assertEqual(got[0]['name'], 'Harbour Road', q)
            self.assertIn('house numbers are not in this map', got[0]['detail'], q)
            self.assertEqual({p['kind'] for p in got}, {'street'}, q)
        self.assertNotIn('house numbers', server.places('Harbour Road')[0]['detail'])

    def test_postcodes_however_they_are_typed(self):
        z = self.tmp / 'GB.zip'
        with zipfile.ZipFile(z, 'w') as f:
            f.writestr('readme.txt', 'about')
            f.writestr('GB.txt', 'GB\tIM1 2AA\tPort Test\tIsle\tIOM\t\t\t\t\t54.2010\t-4.4990\t6\n'
                                 'GB\tIM9 9ZZ\tElsewhere\tFar\tX\t\t\t\t\t10.0\t10.0\t6\n'      # the same country, another region: left out
                                 'GB\tbroken line\n')
        island(self.root, postcodes=[z])
        self.serve()
        for q in ('IM1 2AA', 'im12aa', ' Im1  2aa '):
            got = server.places(q)
            self.assertEqual((got[0]['name'], got[0]['kind'], got[0]['source']), ('IM1 2AA', 'postcode', 'GeoNames'), q)
            self.assertEqual(got[0]['detail'], 'Port Test, Isle')
        self.assertEqual(server.places('IM9 9ZZ'), [])
        self.assertEqual(address_search.postcode_form('sw1a2aa'), 'SW1A 2AA')
        self.assertEqual(address_search.postcode_form('M1 1AE'), 'M1 1AE')
        for not_one in ('90210', 'London', 'SW1A', '12 High Street', ''):
            self.assertIsNone(address_search.postcode_form(not_one), not_one)

    def test_the_house_number_is_read_before_or_after_the_street(self):
        f = address_search.street_only
        self.assertEqual(f('10 Downing Street, London'), 'Downing Street London')
        self.assertEqual(f('Hauptstraße 12, Berlin'), 'Hauptstraße Berlin')
        self.assertEqual(f('221b Baker Street'), 'Baker Street')
        self.assertEqual(f('12/3 Rue de la Paix'), 'Rue de la Paix')
        self.assertIsNone(f('Downing Street'))
        self.assertIsNone(f('12'))
        self.assertIsNone(f('12 34'))
        self.assertIsNone(f(''))

    def test_a_name_then_where_finds_the_thing_called_that(self):
        feats = [('place', 'city', 'London', 5, 30, [[-0.1276, 51.5073]]), ('place', 'town', 'Leeds', 9, 30, [[-1.5491, 53.8008]]),
                 ('road', 'residential', 'Abbey Road', 13, 30, [[-0.1830, 51.5370, -0.1790, 51.5320]]),
                 ('road', 'residential', 'Abbey Road', 13, 30, [[-1.60, 53.83, -1.59, 53.82]]),
                 ('road', 'residential', 'Abbeyfield Road', 13, 30, [[-0.05, 51.49, -0.049, 51.489]]),
                 ('place', 'peak', 'Ben Nevis (1,345 m)', 10, 30, [[-5.0036, 56.7969]]),
                 ('road', 'residential', 'Ben Nevis Way', 13, 30, [[-2.1, 52.45, -2.09, 52.45]]),
                 ('place', 'city', 'München (Munich)', 6, 30, [[11.575, 48.137]])]
        regionkit.make_region(self.root, 'gb', name='Great Britain', feats=feats, pois=[])
        self.serve()
        got = server.places('Abbey Road, London')
        self.assertEqual((got[0]['name'], got[0]['detail']), ('Abbey Road', 'near London, Great Britain'))     # not Abbeyfield Road, which only starts the same
        self.assertEqual([p['name'] for p in got], ['Abbey Road', 'Abbeyfield Road'])                          # which is still offered, after it
        self.assertEqual(server.places('Abbey Road Leeds')[0]['detail'], 'near Leeds, Great Britain')
        self.assertEqual([p['detail'] for p in server.places('Abbey Road', near=(53.8, -1.55))][:2], ['near Leeds, Great Britain', 'near London, Great Britain'])   # with no town named: the nearer first
        got = server.places('Ben Nevis')
        self.assertEqual((got[0]['name'], got[0]['kind'], got[0]['detail']), ('Ben Nevis', 'summit', '1,345 m · Great Britain'))     # the mountain before the street named after it
        got = server.places('munich')
        self.assertEqual([(p['name'], p['detail']) for p in got], [('Munich', 'München · Great Britain')])     # one place, under the name that was asked for
        self.assertEqual(server.places('münchen')[0]['name'], 'München')

    def test_us_addresses_are_still_asked_of_the_address_index_first(self):
        island(self.root)
        self.serve()
        with mock.patch.object(address_search, 'find_addresses', return_value=[{'name': '1 Main St', 'kind': 'Street address'}]) as found:
            self.assertEqual(server.places('1 Main St')[0]['kind'], 'Street address')
        self.assertEqual(found.call_count, 1)


class Heights(Temp):
    def test_a_copernicus_geotiff_is_read_back_exactly(self):
        rng = np.random.default_rng(7)
        a = (rng.random((150, 100)) * 900 - 20).astype(np.float32)
        a[3, 4] = -32767
        for kw in ({}, {'tile': 0}, {'deflate': False, 'predictor': 1}, {'predictor': 1}, {'tile': 0, 'deflate': False, 'predictor': 1}):
            p = regionkit.write_geotiff(self.tmp / 'a.tif', a, -5.0, 55.0, 1 / 100, 1 / 150, nodata=-32767, **kw)
            got, west, north, dx, dy = terrain.read_geotiff(p)
            self.assertEqual(got.shape, (150, 100), kw)
            self.assertTrue(np.isnan(got[3, 4]), kw)
            keep = ~np.isnan(got)
            self.assertTrue(np.array_equal(got[keep], a[keep]), kw)
            self.assertEqual((west, north), (-5.0, 55.0))
            self.assertAlmostEqual(dx, 0.01)
        (self.tmp / 'not.tif').write_bytes(b'PNG' + bytes(100))
        with self.assertRaises(ValueError):
            terrain.read_geotiff(self.tmp / 'not.tif')

    def cells(self, folder, metres):
        """One 1 degree cell (54N 5W) rising from 0 in the west to 1000 m in the east, flat sea in its bottom half."""
        n = 240
        a = np.tile(np.linspace(0, 1000, n, dtype=np.float32), (n, 1))
        a[n // 2:] = 0
        url = sources.dem_url(54, -5, metres)
        p = folder / f'dem{metres}' / url.rsplit('/', 1)[1]
        p.parent.mkdir(parents=True, exist_ok=True)
        regionkit.write_geotiff(p, a, -5.0, 55.0, 1 / n, 1 / n)
        return p

    def src(self, metres):
        have = self.cells(self.tmp / 'src', metres)
        asked = []

        class Src:
            def get(self, url, name, missing_ok=False, max_age_days=None):
                asked.append(name)
                return have if name == f'dem{metres}/{have.name}' else None
        return Src(), asked

    def test_elevation_tiles_from_cells_with_sea_left_out(self):
        outline = [[[[-4.9, 54.55], [-4.1, 54.55], [-4.1, 54.95], [-4.9, 54.95], [-4.9, 54.55]]]]
        src, asked = self.src(30)
        out = self.tmp / 'terrain.sqlite'
        made = terrain.build(out, outline, src, level='detail', workers=1)
        self.assertEqual((made['maxz'], made['metres'], made['cells']), (12, 30, 1))
        self.assertTrue(all(a.startswith('dem30/Copernicus_DSM_COG_10_') for a in asked))
        db = sqlite3.connect(out)
        zs = dict(db.execute('SELECT z, COUNT(*) FROM tile GROUP BY z'))
        self.assertEqual(set(zs), set(range(4, 13)))
        z, x, y = 12, terrain.tile_of(-4.5, 54.75, 12)[0], terrain.tile_of(-4.5, 54.75, 12)[1]
        h = regionkit.heights(db.execute('SELECT png FROM tile WHERE z=? AND x=? AND y=?', (z, x, y)).fetchone()[0])
        w, s, e, n = terrain.tile_box(z, x, y)
        for col in (10, 128, 245):
            lon = w + (col + 0.5) / 256 * (e - w)
            self.assertAlmostEqual(h[128, col], (lon + 5) * 1000, delta=6)      # the slope, to within a source pixel
        sx, sy = terrain.tile_of(-4.5, 54.25, 12)
        self.assertIsNone(db.execute('SELECT 1 FROM tile WHERE z=12 AND x=? AND y=?', (sx, sy)).fetchone())      # all sea: no tile
        self.assertEqual(dict(db.execute('SELECT key, value FROM meta'))['wholez'], '8')
        db.close()

    def test_an_overview_stops_at_zoom_nine_from_the_coarser_model(self):
        outline = [[[[-4.9, 54.55], [-4.1, 54.55], [-4.1, 54.95], [-4.9, 54.95], [-4.9, 54.55]]]]
        src, asked = self.src(90)
        made = terrain.build(self.tmp / 't.sqlite', outline, src, level='overview', workers=1)
        self.assertEqual((made['maxz'], made['metres']), (9, 90))
        self.assertTrue(all(a.startswith('dem90/Copernicus_DSM_COG_30_') for a in asked))
        db = sqlite3.connect(self.tmp / 't.sqlite')
        self.assertEqual(db.execute('SELECT MAX(z), MIN(z) FROM tile').fetchone(), (9, 4))
        db.close()

    def test_nothing_but_sea_makes_no_tiles(self):
        class Empty:
            def get(self, *a, **k):
                return None
        made = terrain.build(self.tmp / 'sea.sqlite', [[[[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5], [0, 0]]]], Empty(), workers=1)
        self.assertEqual(made['tiles'], 0)

    def test_low_zoom_tiles_of_two_regions_are_laid_together(self):
        west = np.zeros((256, 256), np.float32)
        west[:, :128] = 300
        east = np.zeros((256, 256), np.float32)
        east[:, 128:] = 700
        east[0, 0] = 5                                   # ground both have: the first region's is kept
        a = island(self.root, 'a-west', name='A West')
        b = island(self.root, 'b-east', name='B East')
        regionkit.add_terrain(a, {(5, 15, 10): west, (9, 249, 163): west})
        regionkit.add_terrain(b, {(5, 15, 10): east, (9, 249, 163): east})
        self.atlas = regions.Atlas(self.root)
        self.assertTrue(self.atlas.has_terrain())
        self.assertTrue(self.atlas.terrain_detail())
        h = regionkit.heights(self.atlas.terrain(5, 15, 10))
        self.assertAlmostEqual(h[100, 10], 300, delta=0.1)
        self.assertAlmostEqual(h[100, 200], 700, delta=0.1)
        self.assertAlmostEqual(h[0, 0], 300, delta=0.1)
        whole = regionkit.heights(self.atlas.terrain(9, 249, 163))               # from zoom 8 a detailed region's tile is whole: served as it is
        self.assertAlmostEqual(whole[100, 200], 0, delta=0.1)
        self.assertIsNone(self.atlas.terrain(9, 1, 1))
        self.serve()
        status, head, body = ask('GET', '/api/map/terrain/5/15/10.png')
        self.assertEqual((status, head['content-type']), (200, 'image/png'))
        self.assertEqual(ask('GET', '/api/map/terrain/9/1/1.png')[0], 204)


class Info(Temp):
    def test_the_page_is_told_what_is_here(self):
        island(self.root)
        island(self.root, 'mainland', level='overview', name='Mainland', outline=[FAR], box=[2, 48, 3, 49],
               feats=[('place', 'city', 'Farville', 6, 30, [[2.5, 48.5]])], pois=[('fuel', 'Far Fuel', 2.5, 48.5)])
        self.serve()
        with mock.patch.object(server, 'pack_info', return_value=None):
            info = server.map_info()
        self.assertEqual([(r['id'], r['level']) for r in info['regions']], [('isle', 'detail'), ('mainland', 'overview')])
        self.assertEqual(info['units'], 'metric')
        self.assertEqual(info['data_as_of'], ['2026-11-17', '2026-11-17'] if isinstance(info['data_as_of'], list) else ('2026-11-17', '2026-11-17'))
        self.assertEqual(set(info['layers']), {'hospital', 'defibrillator', 'drinking_water', 'fuel'})
        self.assertLess(info['bounds'][0], -4.5)
        self.assertGreater(info['bounds'][2], 2.9)
        self.assertFalse(info['contours'])
        labels = [d['label'] for d in info['datasets']]
        self.assertTrue(labels[0].startswith('Isle of Test: every street'))
        self.assertIn('1 more region', labels[1])
        self.assertIn('Mainland', info['datasets'][1]['status'])
        self.assertFalse(any('US' in l or 'Census' in l for l in labels), labels)
        self.assertIn(self.atlas.version(), info['version'])

    def test_a_pack_with_regions_and_no_us_map_says_nothing_of_the_us(self):
        island(self.root)
        self.serve()
        with mock.patch.object(server, 'pack_info', return_value={'pack': 'all'}):
            info = server.map_info()
        self.assertFalse(any('US' in d['label'] for d in info['datasets']), info['datasets'])
        self.assertEqual(len(info['regions']), 1)

    def test_with_no_regions_nothing_is_added(self):
        self.serve()
        with mock.patch.object(server, 'pack_info', return_value=None):
            info = server.map_info()
        for key in ('regions', 'units', 'data_as_of', 'credits'):
            self.assertNotIn(key, info)
        self.assertNotIn('.r', info['version'])

    def test_which_map_a_point_is_on(self):
        island(self.root)
        island(self.root, 'mainland', level='overview', name='Mainland', outline=[FAR], box=[2, 48, 3, 49], feats=[('place', 'city', 'Farville', 6, 30, [[2.5, 48.5]])], pois=[])
        self.serve()
        self.assertEqual(server.map_where(54.2, -4.5), {'level': 'detail', 'name': 'Isle of Test', 'id': 'isle'})
        self.assertEqual(server.map_where(48.5, 2.5)['level'], 'overview')
        self.assertEqual(server.map_where(0, 0), {'level': None})
        with mock.patch.object(server, 'national_mapdb', return_value=object()):
            self.assertEqual(server.map_where(0, 0), {'level': 'other'})
        status, _, body = ask('GET', '/api/map/where?lat=54.2&lon=-4.5')
        self.assertEqual((status, json.loads(body)['id']), (200, 'isle'))


class Outline(unittest.TestCase):
    HOLED = [[[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]], [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]]]

    def test_inside_a_ring_and_not_in_its_hole(self):
        self.assertTrue(geom.inside(self.HOLED, 1, 1))
        self.assertFalse(geom.inside(self.HOLED, 5, 5))
        self.assertTrue(geom.inside(self.HOLED, 20.5, 20.5))
        self.assertFalse(geom.inside(self.HOLED, 15, 15))
        self.assertEqual(geom.box_of(self.HOLED), [0, 0, 21, 21])
        self.assertIsNone(geom.box_of([]))
        self.assertEqual(geom.multipolygon({'type': 'Polygon', 'coordinates': [[[0, 0]]]}), [[[[0, 0]]]])
        self.assertEqual(geom.multipolygon(None), [])
        self.assertEqual(geom.multipolygon({'type': 'Point', 'coordinates': [0, 0]}), [])

    def test_the_cells_an_outline_touches(self):
        cells = geom.cells(self.HOLED, 1.0)
        self.assertIn((180 + 1, 90 + 1), cells)
        self.assertIn((180 + 20, 90 + 20), cells)
        self.assertNotIn((180 + 15, 90 + 15), cells)
        self.assertIn((180 + 4, 90 + 4), cells)          # the hole's edge runs through it
        sliver = [[[[0.1, 0.1], [3.9, 0.12], [3.9, 0.13], [0.1, 0.11], [0.1, 0.1]]]]      # thinner than a cell, missing every cell's middle
        self.assertEqual({c for c, r in geom.cells(sliver, 1.0)}, {180, 181, 182, 183})
        self.assertEqual(sorted(geom.box_cells(0.5, 0.5, 1.5, 0.6, 1.0)), [(180, 90), (181, 90)])

    def test_an_outline_made_simpler_keeps_its_corners(self):
        ring = [[x / 100, 0.0001 * (x % 2)] for x in range(101)] + [[1, 1], [0, 1], [0, 0]]
        out = geom.simplify_ring(ring, 0.01)
        self.assertLess(len(out), 8)
        for corner in ([0, 0], [1, 1], [0, 1]):
            self.assertIn(corner, [[round(x, 2), round(y, 2)] for x, y in out])
        self.assertEqual(geom.simplify_ring(ring[:4], 0.5), ring[:4])


class Coast(Temp):
    class Land:
        """Two pieces of land: one of 400 points round the island, one far outside the outline's cells."""

        def pieces(self, minx, miny, maxx, maxy):
            t = np.linspace(0, 2 * np.pi, 400)
            ring = np.stack([-4.5 + 0.2 * np.cos(t), 54.25 + 0.1 * np.sin(t)], 1)
            ring[-1] = ring[0]
            yield -4.7, 54.15, -4.3, 54.35, ring
            yield 30.0, 30.0, 30.5, 30.5, np.array([[30.0, 30.0], [30.5, 30.0], [30.5, 30.5], [30.0, 30.0]])
            yield -4.6, 54.2, -4.6, 54.2, np.array([[-4.6, 54.2], [-4.6, 54.2], [-4.6, 54.2], [-4.6, 54.2], [-4.6, 54.2]])   # a rock: smaller than a pixel when zoomed out

    def bands(self, level):
        db = sqlite3.connect(self.tmp / f'{level}.sqlite', isolation_level=None)
        db.executescript(schema.SCHEMA)
        made = coast.add(db, self.Land(), [SQUARE], level)
        rows = db.execute('SELECT f.layer, i.minz, i.maxz, f.geom FROM feat f JOIN feat_idx i ON i.id=f.id ORDER BY f.id').fetchall()
        db.close()
        return made, rows

    def test_the_sea_is_wiped_and_the_land_laid_back_at_falling_detail(self):
        made, rows = self.bands('detail')
        ocean = [r for r in rows if r[0] == 'ocean']
        self.assertEqual([(r[1], r[2]) for r in ocean], [(5, schema.MAXZ)])
        self.assertEqual(schema.unpack(ocean[0][3])[0][0], (-4.8, 54.0))
        land = [r for r in rows if r[0] == 'island']
        big = [(int(r[1]), int(r[2]), len(schema.unpack(r[3])[0])) for r in land if len(schema.unpack(r[3])[0]) > 6]
        self.assertEqual([b[:2] for b in big], [(5, 6), (7, 9), (10, 12), (13, schema.MAXZ)])
        points = [b[2] for b in big]
        self.assertEqual(points, sorted(points))                       # more of the coast's points the further in
        self.assertEqual(points[-1], 400)
        self.assertLess(points[0], 200)
        first, last = schema.unpack(land[0][3])[0][0], schema.unpack(land[0][3])[0][-1]
        self.assertEqual(first, last)                                   # each ring still closes
        self.assertEqual(made['ocean'], 1)
        self.assertFalse(any(abs(schema.unpack(r[3])[0][0][0] - 30) < 1 for r in land))       # land outside the outline's cells is not this region's

    def test_an_overview_keeps_the_coast_to_about_twenty_metres(self):
        made, rows = self.bands('overview')
        land = [(int(r[1]), int(r[2])) for r in rows if r[0] == 'island' and len(schema.unpack(r[3])[0]) > 6]
        self.assertEqual(land, [(5, 6), (7, 9), (10, schema.MAXZ)])

    def test_no_outline_no_coast(self):
        db = sqlite3.connect(self.tmp / 'none.sqlite', isolation_level=None)
        db.executescript(schema.SCHEMA)
        self.assertEqual(coast.add(db, self.Land(), []), {'ocean': 0, 'island': 0})
        db.close()

    def test_the_published_land_file_is_made_ready_once(self):
        """A shapefile of two one-ring polygons inside a zip, as osmdata publishes: taken out and indexed, then asked for a box."""
        import struct

        def record(n, pts):
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            body = struct.pack('<i4d2i', 5, min(xs), min(ys), max(xs), max(ys), 1, len(pts)) + struct.pack('<i', 0) + b''.join(struct.pack('<2d', *p) for p in pts)
            return struct.pack('>2i', n, len(body) // 2) + body
        a = [(-4.6, 54.1), (-4.4, 54.1), (-4.4, 54.3), (-4.6, 54.1)]
        b = [(100.0, 10.0), (101.0, 10.0), (101.0, 11.0), (100.0, 10.0)]
        shp = bytes(100) + record(1, a) + record(2, b)
        z = self.tmp / 'land.zip'
        with zipfile.ZipFile(z, 'w') as f:
            f.writestr('land-polygons-split-4326/land_polygons.shp', shp)
            f.writestr('land-polygons-split-4326/README.txt', 'x')
        said = []
        land = coast.Land(z, self.tmp / 'land', say=said.append)
        got = list(land.pieces(-5, 54, -4, 55))
        self.assertEqual(len(got), 1)
        self.assertEqual([tuple(p) for p in got[0][4].tolist()], a)
        self.assertEqual(len(list(land.pieces(-180, -90, 180, 90))), 2)
        land.close()
        self.assertEqual(len(said), 2)
        again = coast.Land(z, self.tmp / 'land', say=said.append)       # the same zip: nothing is read again
        self.assertEqual(len(said), 2)
        again.close()


class Names(unittest.TestCase):
    INDEX = {'features': [
        {'properties': {'id': 'europe', 'name': 'Europe', 'urls': {'pbf': 'https://download.geofabrik.de/europe-latest.osm.pbf'}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[-10, 35], [40, 35], [40, 70], [-10, 70], [-10, 35]]]}},
        {'properties': {'id': 'germany', 'parent': 'europe', 'name': 'Germany', 'iso3166-1:alpha2': ['DE'], 'urls': {'pbf': 'https://download.geofabrik.de/europe/germany-latest.osm.pbf'}}, 'geometry': {'type': 'MultiPolygon', 'coordinates': [[[[6, 47], [15, 47], [15, 55], [6, 55], [6, 47]]]]}},
        {'properties': {'id': 'bayern', 'parent': 'germany', 'name': 'Bayern', 'urls': {'pbf': 'https://download.geofabrik.de/europe/germany/bayern-latest.osm.pbf'}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[9, 47], [14, 47], [14, 50], [9, 50], [9, 47]]]}},
        {'properties': {'id': 'berlin', 'parent': 'germany', 'name': 'Berlin', 'urls': {'pbf': 'https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf'}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[13, 52], [14, 52], [14, 53], [13, 53], [13, 52]]]}},
        {'properties': {'id': 'great-britain', 'parent': 'europe', 'name': 'Great Britain', 'urls': {'pbf': 'https://download.geofabrik.de/europe/great-britain-latest.osm.pbf'}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[-8, 50], [2, 50], [2, 61], [-8, 61], [-8, 50]]]}},
        {'properties': {'id': 'dach', 'parent': 'europe', 'name': 'Germany, Austria, Switzerland', 'urls': {'pbf': 'https://download.geofabrik.de/europe/dach-latest.osm.pbf'}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[5, 45], [17, 45], [17, 55], [5, 55], [5, 45]]]}},
        {'properties': {'id': 'us/california', 'parent': 'north-america', 'name': 'us/california', 'iso3166-2': ['US-CA'], 'urls': {'pbf': 'https://download.geofabrik.de/north-america/us/california-latest.osm.pbf'}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[-125, 32], [-114, 32], [-114, 42], [-125, 42], [-125, 32]]]}},
        {'properties': {'id': 'no-file', 'name': 'No file', 'urls': {}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
        {'properties': {'id': 'no-shape', 'name': 'No shape', 'urls': {'pbf': 'https://download.geofabrik.de/x.osm.pbf'}}, 'geometry': None},
        {'properties': {'id': '../escape', 'name': 'Bad', 'urls': {'pbf': 'https://download.geofabrik.de/x.osm.pbf'}}, 'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
    ]}

    def test_regions_are_geofabriks_and_a_folder_name_is_safe(self):
        c = catalog.Catalog(self.INDEX)
        self.assertEqual(set(c.entries), {'europe', 'germany', 'bayern', 'berlin', 'great-britain', 'dach', 'us/california'})
        e = c.get('us/california')
        self.assertEqual((e.folder, e.name), ('us_california', 'California'))
        self.assertIs(c.get('us_california'), e)
        self.assertEqual(e.box, [-125, 32, -114, 42])
        with self.assertRaises(KeyError):
            c.get('atlantis')
        for bad in ('../x', 'a/../b', '', 'A', 'x' * 200, '.hidden', 'a b'):
            with self.assertRaises(ValueError):
                catalog.folder_id(bad)

    def test_a_country_served_from_an_untrusted_mirror_is_made_of_its_parts(self):
        c = catalog.Catalog(self.INDEX)
        self.assertEqual([p.id for p in c.parts('germany')], ['bayern', 'berlin'])
        self.assertEqual([p.id for p in c.parts('bayern')], ['bayern'])
        self.assertEqual([p.id for p in c.parts('great-britain')], ['great-britain'])

    def test_every_country_of_a_continent_leaves_out_the_joined_ones(self):
        c = catalog.Catalog(self.INDEX)
        self.assertEqual([e.id for e in c.countries_of('europe')], ['germany', 'great-britain'])
        self.assertEqual([e.id for e in c.children('europe')], ['germany', 'dach', 'great-britain'])

    def test_country_codes_for_postcodes(self):
        c = catalog.Catalog(self.INDEX)
        self.assertEqual(c.iso_of('germany'), ['DE'])
        self.assertEqual(c.iso_of('bayern'), ['DE'])              # from the country above it
        self.assertEqual(c.iso_of('great-britain'), ['GB'])       # Geofabrik names none: this app's own table
        self.assertEqual(c.iso_of('europe'), [])

    def test_the_list_the_app_ships_has_no_outlines(self):
        s = catalog.slim(catalog.Catalog(self.INDEX))
        self.assertEqual(len(s['regions']), 7)
        row = next(r for r in s['regions'] if r['id'] == 'germany')
        self.assertEqual(row, {'id': 'germany', 'name': 'Germany', 'parent': 'europe', 'box': [6, 47, 15, 55], 'iso': ['DE'], 'composite': False, 'parts': True})
        self.assertTrue(next(r for r in s['regions'] if r['id'] == 'dach')['composite'])
        self.assertLess(len(json.dumps(s)), 2000)

    def test_the_shipped_list_is_a_catalog_of_the_whole_world(self):
        path = ROOT / 'content' / 'regions' / 'catalog.json'
        d = json.loads(path.read_text(encoding='utf-8'))
        ids = {r['id'] for r in d['regions']}
        self.assertGreater(len(ids), 500)
        for want in ('great-britain', 'ireland-and-northern-ireland', 'france', 'us/california', 'japan', 'australia', 'brazil', 'south-africa'):
            self.assertIn(want, ids)
        for r in d['regions']:
            catalog.folder_id(r['id'])
            self.assertEqual(len(r['box']), 4)
            self.assertNotIn('outline', r)
        self.assertNotRegex(path.read_text(encoding='utf-8'), r'https?://')        # names and boxes only: addresses are engine/mapbuild/sources.py's


class Sources(Temp):
    def test_elevation_cells_are_named_by_their_south_west_corner(self):
        self.assertTrue(sources.dem_url(54, -5).endswith('/Copernicus_DSM_COG_10_N54_00_W005_00_DEM/Copernicus_DSM_COG_10_N54_00_W005_00_DEM.tif'))
        self.assertTrue(sources.dem_url(-34, 18, 90).endswith('/Copernicus_DSM_COG_30_S34_00_E018_00_DEM.tif'))
        self.assertTrue(sources.dem_url(0, 0).endswith('N00_00_E000_00_DEM.tif'))

    def test_every_address_is_on_an_allowed_host(self):
        from engine.setup import download
        import urllib.parse
        for url in (sources.GEOFABRIK_INDEX, sources.LAND_SPLIT, sources.LAND_SIMPLE, sources.dem_url(54, -5), sources.dem_url(54, -5, 90), sources.POSTCODES.format(file='GB_full.csv.zip')):
            self.assertIn(urllib.parse.urlsplit(url).hostname, download.ALLOWED_HOSTS, url)

    def test_a_file_fetched_once_is_not_fetched_again_and_a_missing_one_is_remembered(self):
        from engine.setup import download
        src = sources.Sources(self.tmp / 'src')
        calls = []

        class Fetch:
            def __init__(self, url, dest, control=None, progress=None):
                self.url, self.dest = url, Path(dest)

            def run(self):
                calls.append(self.url)
                if 'absent' in self.url:
                    raise download.Failed('network', 'The download server refused the file (HTTP 404).', status=404)
                if 'broken' in self.url:
                    raise download.Failed('network', 'The connection kept breaking.')
                self.dest.write_bytes(b'12345')
                Path(str(self.dest) + '.fetched.json').write_text(json.dumps({'size': 5, 'sha256': 'ab', 'url': self.url + '?dated', 'modified': 'today'}))
        with mock.patch.object(download, 'Fetch', Fetch):
            p = src.get('https://x/there', 'a/there.bin')
            self.assertEqual(p.read_bytes(), b'12345')
            self.assertEqual(src.get('https://x/there', 'a/there.bin'), p)
            self.assertEqual(len(calls), 1)
            self.assertEqual(src.source_note('a/there.bin'), {'url': 'https://x/there?dated', 'size': 5, 'sha256': 'ab', 'modified': 'today'})
            self.assertIsNone(src.get('https://x/absent', 'a/absent.bin', missing_ok=True))
            self.assertIsNone(src.get('https://x/absent', 'a/absent.bin', missing_ok=True))
            self.assertEqual(len(calls), 2)                              # not asked for twice
            with self.assertRaises(download.Failed):
                src.get('https://x/absent', 'a/other.bin')               # only where the caller says a file may be missing
            with self.assertRaises(download.Failed):
                src.get('https://x/broken', 'a/broken.bin', missing_ok=True)      # a broken line is not "no such file"
            old = time.time() - 40 * 86400
            os.utime(p, (old, old))
            self.assertEqual(src.get('https://x/there', 'a/there.bin', max_age_days=30), p)
            self.assertEqual(len(calls), 5)                              # grown old: fetched afresh
            os.utime(p, (old, old))
            self.assertEqual(src.get('https://x/broken', 'a/there.bin', max_age_days=30), p)      # the refresh failed: the copy here still serves
        p.write_bytes(b'123')                                            # not the size its note says: not trusted
        self.assertIsNone(src.have('a/there.bin'))


class Finding(Temp):
    def test_the_nearest_town_is_named_only_when_it_is_near(self):
        t = place_finder.Towns([('Port Test', -4.5, 54.2), ('Farville', 2.5, 48.5)])
        self.assertEqual(t.near(-4.45, 54.25), 'Port Test')
        self.assertIsNone(t.near(-4.5, 55.5))                # 140 km north
        self.assertIsNone(t.near(-4.5, 54.2, but='Port Test'))

    def test_a_street_is_listed_once_per_neighbourhood(self):
        feats = [('place', 'town', 'Port Test', 9, 30, [[-4.50, 54.20]]),
                 ('road', 'residential', 'High Street', 13, 30, [[-4.500, 54.200, -4.499, 54.201]]),
                 ('road', 'residential', 'High Street', 13, 30, [[-4.499, 54.201, -4.498, 54.202]]),      # the same street, mapped as two ways
                 ('road', 'residential', 'High Street', 13, 30, [[-4.300, 54.400, -4.299, 54.401]]),      # another High Street, 20 km away
                 ('road', 'footway', 'Cliff Path', 15, 30, [[-4.5, 54.2, -4.49, 54.2]]),                  # a path is not a street
                 ('road', 'service', None, 15, 30, [[-4.5, 54.2, -4.49, 54.2]])]
        folder = regionkit.make_region(self.root, 'streets', name='Streets', feats=feats, pois=[('hospital', None, -4.5, 54.2)])
        db = sqlite3.connect(folder / 'places.sqlite')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM place WHERE kind='street'").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM place WHERE name='Cliff Path'").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM place WHERE rank=4").fetchone()[0], 0)       # a hospital with no name is not something to search for
        self.assertEqual(dict(db.execute('SELECT key, value FROM meta'))['streets'], '2')
        db.close()
        over = regionkit.make_region(self.root, 'over', level='overview', name='Over', feats=feats, pois=[])
        db = sqlite3.connect(over / 'places.sqlite')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM place WHERE kind='street'").fetchone()[0], 0)   # an overview has only main roads: no street finder
        db.close()
        with self.assertRaises(ValueError):
            place_finder.build(folder / 'map.sqlite', folder / 'places.sqlite', 'Streets')               # never over one already made


class Packed(unittest.TestCase):
    def test_geometry_goes_in_and_comes_out_to_a_millionth_of_a_degree(self):
        parts = [[-4.123456, 54.654321, -4.2, 54.7, 179.999999, -85.0], [0.0, 0.0, 0.000001, -0.000001]]
        back = schema.unpack(schema.pack(parts))
        self.assertEqual(back, [[(-4.123456, 54.654321), (-4.2, 54.7), (179.999999, -85.0)], [(0.0, 0.0), (0.000001, -0.000001)]])
        self.assertEqual(server.unpack(schema.pack(parts)), back)                # the app reads what the builder writes
        self.assertEqual(schema.bounds(parts), (-4.2, 179.999999, -85.0, 54.7))
        self.assertEqual(schema.cell(-4.5, 54.2), int((-4.5 + 180) / 0.05) * 100000 + int((54.2 + 90) / 0.05))
        self.assertEqual(schema.CELL, regions.CELL)
        self.assertEqual(schema.CELL, server.SOCAL_CELL)

    def test_tags_become_the_classes_the_map_already_draws(self):
        self.assertEqual(schema.poi_class({'amenity': 'hospital'}), 'hospital')
        self.assertEqual(schema.poi_class({'shop': 'convenience', 'emergency': 'defibrillator'}), 'defibrillator')       # an AED in a shop is an AED
        self.assertIsNone(schema.poi_class({'amenity': 'shelter', 'shelter_type': 'public_transport'}))
        self.assertEqual(schema.poi_class({'amenity': 'shelter'}), 'shelter')
        self.assertEqual(schema.poi_class({'man_made': 'tower', 'tower:type': 'communication'}), 'comms_tower')
        self.assertIsNone(schema.poi_class({'man_made': 'tower'}))
        self.assertEqual(schema.poi_class({'man_made': 'water_well'}), 'water_well')
        self.assertIsNone(schema.poi_class({'amenity': 'bench'}))
        self.assertEqual(schema.land_class({'landuse': 'forest'}), 'green')
        self.assertEqual(schema.land_class({'leisure': 'park'}), 'park')
        self.assertIsNone(schema.land_class({'landuse': 'construction'}))
        self.assertEqual(schema.water_class({'natural': 'water'}), 'water')
        self.assertEqual(schema.water_class({'landuse': 'reservoir'}), 'reservoir')
        self.assertIsNone(schema.water_class({'natural': 'wood'}))
        page = (ROOT / 'web' / 'map.js').read_text(encoding='utf-8')
        for cls in set(schema.ROAD_Z) - {'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}:
            self.assertIn(cls + ':', page.split('const ROAD =')[1].split('const roadStyle')[0], cls)
        for cls in set(schema.LAND.values()):
            self.assertRegex(page.split('const C =')[1].split('const ROAD')[0], r'\b%s:' % cls)

    def test_a_place_is_named_as_its_country_writes_it_and_as_english_does(self):
        f = schema.place_name
        self.assertEqual(f({'name': 'München', 'name:en': 'Munich'}), 'München (Munich)')
        self.assertEqual(f({'name': 'Αθήνα', 'name:en': 'Athens'}), 'Αθήνα (Athens)')
        self.assertEqual(f({'name': 'Zürich', 'name:en': 'Zurich'}), 'Zürich')               # the same name but for its accents
        self.assertEqual(f({'name': 'Leeds', 'name:en': 'Leeds'}), 'Leeds')
        self.assertEqual(f({'name': 'Leeds'}), 'Leeds')
        self.assertEqual(f({'name:en': 'Somewhere'}), 'Somewhere')
        self.assertEqual(f({'name': 'Wien', 'int_name': 'Vienna'}), 'Wien (Vienna)')
        self.assertEqual(f({'name': 'Newport (Gwent)', 'name:en': 'Newport'}), 'Newport (Gwent)')       # brackets of its own: left as it is
        self.assertIsNone(f({}))
        self.assertEqual(schema.split_name('München (Munich)'), ('München', 'Munich'))
        self.assertEqual(schema.split_name('Leeds'), ('Leeds', None))
        self.assertEqual(schema.split_name('(odd)'), ('(odd)', None))

    def test_heights_and_what_is_no_longer_there(self):
        for text, want in (('1345', 1345), ('1,345 m', 1345), ('1345.2', 1345.2), ('4413 ft', 4413 * 0.3048), ('621m', 621), ('', None), ('high', None), ('99999', None), (None, None)):
            got = schema.metres(text)
            self.assertEqual(got if got is None else round(got, 1), want if want is None else round(want, 1), text)
        self.assertEqual([schema.peak_z(e) for e in (3000, 1345, 621, 200, None)], [9, 10, 11, 12, 13])
        self.assertIsNone(schema.poi_class({'amenity': 'hospital', 'disused': 'yes'}))
        self.assertIsNone(schema.poi_class({'amenity': 'pharmacy', 'abandoned': 'yes'}))
        self.assertEqual(schema.poi_class({'amenity': 'hospital', 'disused': 'no'}), 'hospital')
        self.assertEqual(schema.BORDER['2'][0], 'country')

    def test_a_shape_appears_when_it_is_big_enough_to_see(self):
        self.assertEqual(schema.area_z(0, 10, 0, 10, 7), 7)          # a region-sized lake: from the first zoom allowed
        self.assertEqual(schema.area_z(0, 0.02, 0, 0.01, 7), 10)
        self.assertEqual(schema.area_z(0, 0.0001, 0, 0.0001, 7), 16)
        self.assertEqual(schema.area_z(0, 0, 0, 0, 7), 17)
        flat = [0.0, 0.0, 0.00001, 0.0, 0.00002, 0.0, 1.0, 0.0, 1.0, 1.0]
        self.assertEqual(schema.simplify(flat, 0.001), [0.0, 0.0, 1.0, 0.0, 1.0, 1.0])
        self.assertEqual(schema.simplify(flat, 0), flat)


if __name__ == '__main__':
    unittest.main()
