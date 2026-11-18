"""A region's place finder: places.sqlite, in the form derived/geo/places.sqlite has always had, so one search reads both.

  place(id, name, kind, detail, lat, lon, rank, source)  +  place_fts(name, detail)

From the region's own map.sqlite: cities, towns, villages and smaller places; named help points; and, in a detailed
region, every named street once per neighbourhood (so "Glencrutchery Road" is found even though no open data gives
house numbers here). From GeoNames: postcodes, where it publishes them for the country.

rank orders equal matches: 1 city/town, 2 village/suburb/postcode, 3 hamlet and smaller, 4 help point, 5 street.
"""
import csv
import io
import math
import re
import struct
import zipfile

from engine import sqlitefs
from . import geom
from . import schema as S

SCHEMA = '''
CREATE TABLE IF NOT EXISTS place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61');
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
'''
INSERT = 'INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)'
PLACE_RANK = {'city': 1, 'town': 1, 'county': 2, 'region': 2, 'village': 2, 'suburb': 2, 'island': 2, 'peak': 2, 'hamlet': 3, 'locality': 3, 'farm': 3}
PLACE_KIND = {'city': 'city', 'town': 'town', 'village': 'village', 'suburb': 'suburb or neighbourhood', 'hamlet': 'hamlet', 'locality': 'locality',
              'island': 'island', 'farm': 'farm', 'county': 'county', 'region': 'region', 'peak': 'summit'}
HEIGHT = re.compile(r' \((-?[\d,]+) m\)$')
STREETS = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'unclassified', 'living_street', 'pedestrian'}
# A road with no name is drawn with its number (M1, A 4, D 906, E 45;A1): a number runs the length of a country and is not a street to find.
ROAD_NUMBER = re.compile(r'[A-Za-z]{0,4}[ -]?\d+[A-Za-z]?(\s*[;,/(].*)?')
NEAR_KM = 40            # "near <town>" is said only when a town is this close
STREET_CELL = 0.02      # a street is listed once per cell of this size (about 2 km): the same name in two towns is two streets
POI_LABEL = {'doctors': 'doctor', 'fire_station': 'fire station', 'drinking_water': 'drinking water tap', 'community_centre': 'community centre',
             'town_hall': 'town hall', 'doityourself': 'hardware / DIY store', 'camp_site': 'campsite', 'water_well': 'well or water pump',
             'defibrillator': 'defibrillator (AED)', 'ambulance_station': 'ambulance station', 'assembly_point': 'emergency assembly point',
             'post_office': 'post office', 'nursing_home': 'care home', 'garden_centre': 'garden centre', 'car_repair': 'car repair',
             'bicycle_shop': 'bicycle shop', 'comms_tower': 'communications tower', 'water_tower': 'water tower', 'water_works': 'water works',
             'wastewater_plant': 'wastewater plant', 'veterinary': 'vet', 'chemist': 'pharmacy'}


class Towns:
    """The nearest city or town to a point, for "near Ramsey"."""

    def __init__(self, towns, cell=0.5):
        self.cell = cell
        self.grid = {}
        for name, lon, lat in towns:
            self.grid.setdefault((int((lon + 180) / cell), int((lat + 90) / cell)), []).append((name, lon, lat))

    def near(self, lon, lat, but=None):
        c, r = int((lon + 180) / self.cell), int((lat + 90) / self.cell)
        k = math.cos(math.radians(lat))
        best, best_d = None, (NEAR_KM / 111.0) ** 2
        for dc in (-1, 0, 1):
            for dr in (-1, 0, 1):
                for name, x, y in self.grid.get((c + dc, r + dr), ()):
                    d = ((x - lon) * k) ** 2 + (y - lat) ** 2
                    if d < best_d and name != but:
                        best, best_d = name, d
        return best


def build(map_db, out, region_name, level='detail', postcodes=(), outline=None, control=None):
    """map_db: the region's finished map.sqlite (a path). postcodes: GeoNames zips for the region's countries.
    outline: the region's multipolygon; postcodes outside it (another part of the same country) are left out."""
    src = sqlitefs.connect(map_db)
    db = sqlitefs.connect(out, lambda d: d.executescript(SCHEMA), synchronous='OFF')
    db.execute('PRAGMA journal_mode=OFF')
    if db.execute('SELECT 1 FROM place LIMIT 1').fetchone():
        raise ValueError(f'{out} already holds places')
    n = {'places': 0, 'points': 0, 'streets': 0, 'postcodes': 0}
    named = []
    for cls, name, g in src.execute("SELECT cls, name, geom FROM feat WHERE layer='place' AND name IS NOT NULL"):
        _, _, x, y = struct.unpack_from('<iiii', g)
        height = None
        if cls == 'peak':           # 'Ben Nevis (1,345 m)': the height comes off the name and goes into the detail
            m = HEIGHT.search(name)
            if m:
                name, height = name[:m.start()], m.group(1)
        own, other = S.split_name(name)     # 'München (Munich)': found by either
        named.append((cls, own, other, height, x / 1e6, y / 1e6))
    # "near <town>" names the town as a reader of English would look for it, where the map has that name
    towns = Towns([(other or own, lon, lat) for cls, own, other, _, lon, lat in named if cls in ('city', 'town')])

    def where(lon, lat, but=None):
        t = towns.near(lon, lat, but)
        return f'near {t}, {region_name}' if t else region_name
    db.execute('BEGIN')
    for cls, own, other, height, lon, lat in named:
        base = region_name if cls in ('city', 'town') else where(lon, lat, other or own)
        if height:
            base = f'{height} m · {base}'
        kind, rank = PLACE_KIND.get(cls, cls), PLACE_RANK.get(cls, 3)
        db.execute(INSERT, (own, kind, f'{other} · {base}' if other else base, lat, lon, rank, 'OpenStreetMap'))
        if other:
            db.execute(INSERT, (other, kind, f'{own} · {base}', lat, lon, rank, 'OpenStreetMap'))
        n['places'] += 1
    for cls, name, lon, lat in src.execute("SELECT cls, name, lon, lat FROM poi WHERE name IS NOT NULL AND name<>''"):
        db.execute(INSERT, (name, POI_LABEL.get(cls, cls.replace('_', ' ')), where(lon, lat), lat, lon, 4, 'OpenStreetMap'))
        n['points'] += 1
    db.execute('COMMIT')
    if level == 'detail':
        seen = {}
        cur = src.execute("SELECT f.cls, f.name, f.geom FROM feat f WHERE f.layer='road' AND f.name IS NOT NULL")
        for cls, name, g in cur:
            if cls not in STREETS or ROAD_NUMBER.fullmatch(name):
                continue
            count = struct.unpack_from('<ii', g)[1]
            # the middle point of the way: deltas summed up to it
            vals = struct.unpack_from('<%di' % (2 * (count // 2 + 1)), g, 8)
            lon, lat = sum(vals[0::2]) / 1e6, sum(vals[1::2]) / 1e6
            key = (name, int((lon + 180) / STREET_CELL), int((lat + 90) / STREET_CELL))
            if key not in seen:
                seen[key] = (lon, lat)
        if control is not None:
            control.check()
        db.execute('BEGIN')
        for (name, _, _), (lon, lat) in seen.items():
            db.execute(INSERT, (name, 'street', where(lon, lat), lat, lon, 5, 'OpenStreetMap'))
            n['streets'] += 1
        db.execute('COMMIT')
    wanted = geom.cells(outline, 0.1) if outline else None
    for path in postcodes:
        if control is not None:
            control.check()
        db.execute('BEGIN')
        with zipfile.ZipFile(path) as z:
            member = next((m for m in z.namelist() if m.lower().endswith(('.txt', '.csv')) and 'readme' not in m.lower()), None)
            if member is None:
                db.execute('COMMIT')
                continue
            with z.open(member) as f:
                for r in csv.reader(io.TextIOWrapper(f, encoding='utf-8', errors='replace'), delimiter='\t', quoting=csv.QUOTE_NONE):
                    if len(r) < 11:
                        continue
                    try:
                        lat, lon = float(r[9]), float(r[10])
                    except ValueError:
                        continue
                    if wanted is not None and (int((lon + 180) / 0.1), int((lat + 90) / 0.1)) not in wanted:
                        continue
                    detail = ', '.join(dict.fromkeys(x for x in (r[2], r[5], r[3]) if x))
                    db.execute(INSERT, (r[1], 'postcode', detail or region_name, lat, lon, 2, 'GeoNames'))
                    n['postcodes'] += 1
        db.execute('COMMIT')
    db.execute("INSERT INTO place_fts(place_fts) VALUES ('rebuild')")
    db.execute("INSERT INTO place_fts(place_fts) VALUES ('optimize')")
    db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [(k, str(v)) for k, v in n.items()])
    db.close()
    src.close()
    return n
