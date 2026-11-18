"""The tables a region's map.sqlite holds, its packed geometry, and which OpenStreetMap tags become which classes.

The tables and the classes are those of the map Lastbook already draws (tools/build_map.py, made from Geofabrik's
ready-made layers), so web/map.js styles a region with no change. Here they are derived from the raw tags instead,
because Geofabrik publishes those layers only for its smaller extracts and publishes .osm.pbf for all of them.

  feat(id, layer, cls, name, minz, geom)     geom: int32s: parts, then for each part its point count and x,y deltas in 1e-6 degrees
  feat_idx (R*Tree)                          id, minx, maxx, miny, maxy, minz, maxz: maxz lets a feature stop at a zoom (coarse coast)
  poi(id, cls, name, lon, lat)               help points
  cover(cell)                                0.05 degree cells this region maps in detail: an overview is not drawn there
  meta(key, value)
"""
import math
from array import array

SCHEMA = '''
CREATE TABLE IF NOT EXISTS feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, minz INTEGER, geom BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS feat_idx USING rtree(id, minx, maxx, miny, maxy, minz, maxz);
CREATE TABLE IF NOT EXISTS poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, lon REAL, lat REAL);
CREATE TABLE IF NOT EXISTS cover(cell INTEGER PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
'''
CELL = 0.05             # as server.SOCAL_CELL: the grid "mapped in detail here" is kept on
MAXZ = 30

# Zoom from which each kind of road is drawn (tools/build_map.py ROAD_Z).
ROAD_Z = {'motorway': 5, 'trunk': 6, 'motorway_link': 10, 'trunk_link': 10, 'primary': 8, 'primary_link': 12,
          'secondary': 10, 'secondary_link': 13, 'tertiary': 11, 'tertiary_link': 13, 'residential': 13,
          'unclassified': 13, 'living_street': 14, 'pedestrian': 14, 'busway': 14, 'service': 15,
          'track': 13, 'footway': 15, 'path': 14, 'cycleway': 14, 'steps': 16, 'bridleway': 14}
OVERVIEW_ROADS = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'motorway_link', 'trunk_link'}
PLACE_Z = {'county': 7, 'city': 6, 'town': 9, 'suburb': 12, 'village': 11, 'hamlet': 14, 'locality': 14, 'island': 11, 'farm': 15, 'region': 5}
PLACE_AS = {'neighbourhood': 'suburb', 'quarter': 'suburb', 'borough': 'suburb', 'municipality': 'town', 'islet': 'island', 'isolated_dwelling': 'farm'}
OVERVIEW_PLACES = {'city', 'town', 'village', 'island', 'region', 'county'}

# Land: (key, value) -> the class the map colours by.
LAND = {}
for _cls, _pairs in {
    'park': [('leisure', 'park'), ('leisure', 'common'), ('leisure', 'recreation_ground'), ('leisure', 'nature_reserve'), ('leisure', 'garden'),
             ('landuse', 'recreation_ground'), ('landuse', 'village_green'), ('landuse', 'cemetery'), ('amenity', 'grave_yard')],
    'green': [('landuse', 'forest'), ('natural', 'wood'), ('landuse', 'grass'), ('landuse', 'meadow'), ('natural', 'scrub'), ('natural', 'heath'), ('natural', 'grassland')],
    'farm': [('landuse', 'allotments'), ('landuse', 'farmland'), ('landuse', 'orchard'), ('landuse', 'vineyard'), ('landuse', 'farmyard')],
    'res': [('landuse', 'residential')],
    'com': [('landuse', 'commercial'), ('landuse', 'retail')],
    'ind': [('landuse', 'industrial'), ('landuse', 'military'), ('landuse', 'quarry'), ('landuse', 'landfill')],
}.items():
    for _p in _pairs:
        LAND[_p] = _cls
OVERVIEW_LAND = {'res', 'com', 'ind', 'green', 'park'}
WATER = {('natural', 'water'): 'water', ('landuse', 'reservoir'): 'reservoir', ('landuse', 'basin'): 'reservoir', ('waterway', 'riverbank'): 'river',
         ('waterway', 'dock'): 'dock', ('natural', 'glacier'): 'glacier'}
WATERWAY_Z = {'river': 10, 'canal': 12, 'stream': 13}
FERRY_Z = 7             # a ferry's route, drawn in the 'waterway' layer as class 'ferry'
# Borders, from the ways that carry them: admin_level 2 is a country's, 4 a state's or a nation's within one (England and Scotland).
BORDER = {'2': ('country', 4), '4': ('region', 7)}
RAIL_Z = {'rail': 10, 'light_rail': 13, 'subway': 13, 'tram': 14, 'narrow_gauge': 13}

# Help points: (key, value) -> class. The classes are those server.HELP names and web/map.js has layers for, with four added
# that matter outside the US and in an emergency: water_well, defibrillator, ambulance_station, assembly_point.
POI = {}
for _key, _values in {
    'amenity': {'hospital': 'hospital', 'clinic': 'clinic', 'doctors': 'doctors', 'pharmacy': 'pharmacy', 'dentist': 'dentist', 'veterinary': 'veterinary',
                'fire_station': 'fire_station', 'police': 'police', 'drinking_water': 'drinking_water', 'water_point': 'drinking_water', 'fountain': 'fountain',
                'toilets': 'toilet', 'shelter': 'shelter', 'community_centre': 'community_centre', 'townhall': 'town_hall', 'school': 'school', 'library': 'library',
                'post_office': 'post_office', 'courthouse': 'courthouse', 'embassy': 'embassy', 'nursing_home': 'nursing_home', 'kindergarten': 'kindergarten',
                'college': 'college', 'university': 'university', 'fuel': 'fuel'},
    'shop': {'supermarket': 'supermarket', 'convenience': 'convenience', 'doityourself': 'doityourself', 'hardware': 'hardware', 'garden_centre': 'garden_centre',
             'chemist': 'chemist', 'bicycle': 'bicycle_shop', 'car_repair': 'car_repair'},
    'man_made': {'water_tower': 'water_tower', 'water_works': 'water_works', 'wastewater_plant': 'wastewater_plant', 'water_well': 'water_well',
                 'communications_tower': 'comms_tower'},
    'tourism': {'camp_site': 'camp_site', 'caravan_site': 'camp_site', 'alpine_hut': 'shelter', 'wilderness_hut': 'shelter'},
    'natural': {'spring': 'spring'},
    'emergency': {'defibrillator': 'defibrillator', 'ambulance_station': 'ambulance_station', 'assembly_point': 'assembly_point', 'drinking_water': 'water_well'},
    'healthcare': {'hospital': 'hospital', 'clinic': 'clinic', 'doctor': 'doctors', 'pharmacy': 'pharmacy', 'dentist': 'dentist'},
}.items():
    for _v, _cls in _values.items():
        POI[(_key, _v)] = _cls
POI_KEYS = ('emergency', 'amenity', 'healthcare', 'shop', 'man_made', 'tourism', 'natural')      # in this order: an AED inside a shop is an AED
OVERVIEW_POI = {'hospital', 'clinic', 'doctors', 'dentist', 'pharmacy', 'chemist', 'fire_station', 'police', 'shelter', 'community_centre', 'town_hall', 'library',
                'drinking_water', 'spring', 'water_well', 'supermarket', 'convenience', 'hardware', 'doityourself', 'fuel', 'camp_site',
                'defibrillator', 'ambulance_station', 'assembly_point'}


GONE = ('disused', 'abandoned', 'demolished', 'removed', 'razed')


def poi_class(tags):
    for word in GONE:           # mapped as no longer there: a closed hospital is worse than no hospital
        if tags.get(word) == 'yes':
            return None
    for key in POI_KEYS:
        v = tags.get(key)
        if v is None:
            continue
        cls = POI.get((key, v))
        if cls is None:
            if key == 'man_made' and v == 'tower' and tags.get('tower:type') == 'communication':
                return 'comms_tower'
            continue
        if cls == 'shelter' and tags.get('shelter_type') == 'public_transport':
            return None     # a bus shelter is not somewhere to shelter
        return cls
    return None


def land_class(tags):
    for key in ('landuse', 'leisure', 'natural', 'amenity'):
        v = tags.get(key)
        if v is not None:
            cls = LAND.get((key, v))
            if cls:
                return cls
    return None


def water_class(tags):
    for key in ('natural', 'landuse', 'waterway'):
        v = tags.get(key)
        if v is not None:
            cls = WATER.get((key, v))
            if cls:
                return cls
    return None


def _plain(text):
    """Letters only, without their accents, in lower case: 'Zürich' and 'Zurich' are the same name."""
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFKD', text.lower()) if c.isalnum() and not unicodedata.combining(c))


def place_name(tags):
    """A place's name as its own country writes it, with the English one after it in brackets where that is another word:
    'München (Munich)', 'Αθήνα (Athens)'. Road signs there say the first; a person who reads English looks for the second.
    Names that differ only by accents ('Zürich', 'Zurich') are given once."""
    name, en = tags.get('name'), tags.get('name:en') or tags.get('int_name')
    if not name:
        return en or None
    if en and _plain(en) != _plain(name) and '(' not in name and len(en) <= 60:
        return f'{name} ({en})'
    return name


def split_name(label):
    """'München (Munich)' -> ('München', 'Munich'); 'Leeds' -> ('Leeds', None). The reverse of place_name, for the place finder."""
    if label.endswith(')') and ' (' in label:
        own, _, other = label[:-1].rpartition(' (')
        if own and other:
            return own, other
    return label, None


def peak_z(ele):
    """The zoom a summit's name appears at: the highest from furthest out."""
    if ele is None:
        return 13
    return 9 if ele >= 2500 else 10 if ele >= 1000 else 11 if ele >= 600 else 12


def metres(text):
    """An OpenStreetMap ele tag as a number of metres, or None: '1345', '1345 m', '1,345', '1345.2'; feet are converted."""
    if not text:
        return None
    t = text.strip().lower().replace(',', '')
    feet = t.endswith(('ft', "'", 'feet'))
    t = t.rstrip("abcdefghijklmnopqrstuvwxyz' ")
    try:
        v = float(t)
    except ValueError:
        return None
    v = v * 0.3048 if feet else v
    return v if -500 < v < 9000 else None


def cell(lon, lat):
    return int((lon + 180) / CELL) * 100000 + int((lat + 90) / CELL)


def pack(parts):
    """parts: [[x, y, x, y, ...], ...] in degrees -> the packed int32 blob (tools/build_map.py pack)."""
    a = array('i', [len(parts)])
    append = a.append
    for p in parts:
        append(len(p) >> 1)
        lx = ly = 0
        for i in range(0, len(p), 2):
            x, y = int(round(p[i] * 1e6)), int(round(p[i + 1] * 1e6))
            append(x - lx)
            append(y - ly)
            lx, ly = x, y
    return a.tobytes()


def unpack(blob):
    """The reverse, as server.unpack: [[(lon, lat), ...], ...]"""
    a = array('i')
    a.frombytes(blob)
    parts, i = [], 1
    for _ in range(a[0]):
        n = a[i]
        i += 1
        x = y = 0
        pts = []
        for _ in range(n):
            x += a[i]
            y += a[i + 1]
            i += 2
            pts.append((x / 1e6, y / 1e6))
        parts.append(pts)
    return parts


def simplify(flat, tol):
    """Drops points within tol degrees (in both x and y) of the last one kept; the ends stay."""
    if len(flat) <= 4 or tol <= 0:
        return flat
    out = [flat[0], flat[1]]
    px, py = flat[0], flat[1]
    for i in range(2, len(flat) - 2, 2):
        x, y = flat[i], flat[i + 1]
        if abs(x - px) > tol or abs(y - py) > tol:
            out.append(x)
            out.append(y)
            px, py = x, y
    out.append(flat[-2])
    out.append(flat[-1])
    return out


def bounds(parts):
    minx = miny = 1e9
    maxx = maxy = -1e9
    for p in parts:
        xs, ys = p[0::2], p[1::2]
        a, b, c, d = min(xs), max(xs), min(ys), max(ys)
        if a < minx:
            minx = a
        if b > maxx:
            maxx = b
        if c < miny:
            miny = c
        if d > maxy:
            maxy = d
    return minx, maxx, miny, maxy


def area_z(minx, maxx, miny, maxy, base):
    """The zoom a polygon appears at: when it is about a sixteenth of the screen across, and never before `base`."""
    size = max(maxx - minx, maxy - miny)
    if size <= 0:
        return 17
    return max(base, min(16, int(math.log2(360 / size)) - 4))
