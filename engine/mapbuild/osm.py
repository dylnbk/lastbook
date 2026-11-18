"""An OpenStreetMap extract (.osm.pbf) -> a region's map.sqlite.

  build(pbf, out, level='detail')   -> {'features', 'pois', 'places', 'data_as_of', 'box', ...}

One pass over the file with pyosmium. Its own code reads the file, keeps where every node is, puts multipolygons
together and drops what carries none of the tags asked for; only what will be kept reaches Python. Geometry comes out
as WKB, read here with struct, so no point is touched one at a time until it is packed.

level 'detail': every road and path, railways, waterways, water, land use, buildings, places, all help points.
level 'overview': what a continent can carry (schema.OVERVIEW_*), and still every help point that has a layer.
"""
import os
import struct
import time

from engine import sqlitefs
from . import schema as S

DETAIL_KEYS = ('highway', 'railway', 'waterway', 'natural', 'landuse', 'leisure', 'amenity', 'shop', 'man_made', 'tourism', 'emergency',
               'healthcare', 'place', 'building', 'boundary', 'route')
AREA_KEYS = ('natural', 'landuse', 'leisure', 'amenity', 'waterway', 'boundary', 'building', 'shop', 'tourism', 'man_made', 'healthcare')
BATCH = 40000
PROTECTED = {'national_park', 'protected_area'}
# How close together (degrees) two points of a shape may be before one is dropped. A detailed region keeps a road as it
# was drawn. An overview is looked at from far out and has a continent to carry: 0.0004 degrees is about 40 m, a pixel
# at zoom 11. Measured on Bulgaria, woods alone were 39 MB of a 74 MB overview before this.
TOLERANCE = {'detail': {'road': 0, 'rail': 0, 'waterway': 0, 'water': 0.00001, 'land': 0.00001, 'park': 0.00002},
             'overview': {'road': 0.00004, 'rail': 0.00006, 'waterway': 0.0002, 'water': 0.0003, 'land': 0.0004, 'park': 0.0005, 'woods': 0.001}}


def _wkb_parts(b, o=0):
    """(parts, next offset): each part a flat tuple x, y, x, y... A polygon gives its rings as parts (tools/build_map.py wkb_parts)."""
    e = '<' if b[o] == 1 else '>'
    t = struct.unpack_from(e + 'I', b, o + 1)[0] % 1000
    o += 5
    if t == 1:
        return [struct.unpack_from(e + 'dd', b, o)], o + 16
    if t == 2:
        n = struct.unpack_from(e + 'I', b, o)[0]
        return [struct.unpack_from(e + '%dd' % (2 * n), b, o + 4)], o + 4 + 16 * n
    if t == 3:
        rings = struct.unpack_from(e + 'I', b, o)[0]
        o += 4
        parts = []
        for _ in range(rings):
            n = struct.unpack_from(e + 'I', b, o)[0]
            parts.append(struct.unpack_from(e + '%dd' % (2 * n), b, o + 4))
            o += 4 + 16 * n
        return parts, o
    if t in (4, 5, 6, 7):
        n = struct.unpack_from(e + 'I', b, o)[0]
        o += 4
        parts = []
        for _ in range(n):
            p, o = _wkb_parts(b, o)
            parts.extend(p)
        return parts, o
    raise ValueError('geometry type %d' % t)


def _overview_filter(osmium):
    """Every (key, value) an overview keeps, as one filter: an object with any of them passes."""
    pairs = [('highway', v) for v in S.OVERVIEW_ROADS]
    pairs += [('railway', 'rail'), ('waterway', 'river'), ('waterway', 'canal'), ('natural', 'wetland'), ('natural', 'beach'),
              ('natural', 'peak'), ('natural', 'volcano'), ('route', 'ferry'), ('boundary', 'administrative')]
    pairs += [('place', v) for v in S.OVERVIEW_PLACES] + [('place', k) for k, v in S.PLACE_AS.items() if v in S.OVERVIEW_PLACES]
    pairs += [k for k, cls in S.LAND.items() if cls in S.OVERVIEW_LAND] + list(S.WATER)
    pairs += [k for k, cls in S.POI.items() if cls in S.OVERVIEW_POI]
    pairs += [('boundary', v) for v in PROTECTED]
    return osmium.filter.TagFilter(*sorted(set(pairs)))


def forget_nodes(path):
    """Removes a file where nodes were kept. True when nothing of it is left to take room.
    The library that reads the extract keeps the file open until the program ends (seen with lsof on a Mac: the mapping goes
    with the reader, the descriptor stays). A Mac and Linux remove an open file all the same. Windows will not (the first run
    of the tests there found both files still in place), so there it is emptied, which gives the room back at once, and the
    empty file is removed before a later build (build._forget_stale_nodes)."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass
    try:
        os.truncate(path, 0)
        return True
    except OSError:
        return False


def build(pbf, out, level='detail', buildings=True, node_index=None, progress=None, control=None, name=''):
    """pbf (one, or a list read one after another) -> out (a new map.sqlite).
    node_index: a file for where nodes are, instead of memory (a country needs gigabytes). It is memory-mapped: keep it on
    the computer's own disk, never on an exFAT drive (see build.scratch for what was measured).
    progress(what, count). control.check() raises to stop. Returns what went in."""
    import osmium      # here, so the rest of the engine starts without it
    detail = level == 'detail'
    t0 = time.time()
    db = sqlitefs.connect(out, lambda d: d.executescript(S.SCHEMA), synchronous='OFF')
    db.execute('PRAGMA journal_mode=OFF')       # a build that stops is begun again, never mended: no journal to pay for
    db.execute('PRAGMA cache_size=-262144')     # 256 MB
    db.execute('PRAGMA temp_store=FILE')
    if db.execute('SELECT 1 FROM feat LIMIT 1').fetchone():
        raise ValueError(f'{out} already holds a map')
    wkb = osmium.geom.WKBFactory()
    feat, idx, pois, cells = [], [], [], set()
    n = {'id': 0, 'poi': 0, 'place': 0, 'road': 0, 'bldg': 0, 'area': 0, 'bad': 0, 'seen': 0}
    box = [1e9, 1e9, -1e9, -1e9]

    def flush(force=False):
        if force or len(feat) >= BATCH:
            db.execute('BEGIN')
            db.executemany('INSERT INTO feat VALUES (?,?,?,?,?,?)', feat)
            db.executemany('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', idx)
            db.executemany('INSERT INTO poi VALUES (?,?,?,?,?)', pois)
            db.execute('COMMIT')
            feat.clear()
            idx.clear()
            pois.clear()
            if control is not None:
                control.check()
            if progress:
                progress('features', n['id'])

    def add(layer, cls, label, minz, parts):
        minx, maxx, miny, maxy = S.bounds(parts)
        n['id'] += 1
        feat.append((n['id'], layer, cls, label, minz, S.pack(parts)))
        idx.append((n['id'], minx, maxx, miny, maxy, minz, S.MAXZ))
        if minx < box[0]:
            box[0] = minx
        if miny < box[1]:
            box[1] = miny
        if maxx > box[2]:
            box[2] = maxx
        if maxy > box[3]:
            box[3] = maxy
        if detail and maxx - minx < S.CELL and maxy - miny < S.CELL:
            cells.add(S.cell(parts[0][0], parts[0][1]))
        if len(feat) >= BATCH:
            flush()

    def poi(cls, label, lon, lat):
        n['poi'] += 1
        pois.append((n['poi'], cls, label, lon, lat))

    def label_of(tags):
        return tags.get('name') or tags.get('name:en') or None

    tol = TOLERANCE[level]

    def line(way, within):
        """A way's points; in an overview, without those too close together to tell apart at the zooms it is drawn for."""
        part = _wkb_parts(bytes.fromhex(wkb.create_linestring(way)))[0][0]
        return [S.simplify(part, within) if within else part]

    def area(layer, cls, label, z, parts, within):
        """Adds a polygon, its rings made simpler; a ring left with no area is dropped. Returns 1 if anything was added."""
        rings = [r for r in (S.simplify(p, within) for p in parts) if len(r) >= 8]
        if not rings:
            return 0
        add(layer, cls, label, z, rings)
        return 1

    files = [str(p) for p in (pbf if isinstance(pbf, (list, tuple)) else [pbf])]
    stamps = []

    def objects():
        """Every wanted object of every file. A region given as several extracts (a country as its states) is read one after another."""
        for i, one in enumerate(files):
            where = 'flex_mem'
            if node_index:
                where = f'sparse_file_array,{node_index}.{i}'       # a file per extract: each is its own set of nodes
            fp = osmium.FileProcessor(one).with_locations(where)
            fp = fp.with_areas(osmium.filter.KeyFilter(*AREA_KEYS))
            fp = fp.with_filter(osmium.filter.KeyFilter(*DETAIL_KEYS) if detail else _overview_filter(osmium))
            try:
                stamps.append(fp.header.get('osmosis_replication_timestamp') or fp.header.get('timestamp') or '')
            except Exception:
                pass
            if progress and len(files) > 1:
                progress('file', i + 1)
            yield from fp
            del fp
            if node_index:
                forget_nodes(f'{node_index}.{i}')

    for o in objects():
        n['seen'] += 1
        tags = o.tags
        try:
            if o.is_node():
                place = tags.get('place')
                if place is None and tags.get('natural') in ('peak', 'volcano'):
                    # A named summit is a place to find and to steer by. An overview keeps the ones the world knows (those with an
                    # entry in Wikidata) and the very high; a detailed region keeps every one that has a name.
                    label, ele = S.place_name(tags), S.metres(tags.get('ele'))
                    if label and (detail or 'wikidata' in tags or (ele or 0) >= 2500):
                        add('place', 'peak', f'{label} ({ele:,.0f} m)' if ele is not None else label, S.peak_z(ele), [(o.lon, o.lat)])
                        n['place'] += 1
                    continue
                if place is not None:
                    cls = S.PLACE_AS.get(place, place)
                    label = S.place_name(tags)
                    if label and cls in S.PLACE_Z and (detail or cls in S.OVERVIEW_PLACES):
                        z = S.PLACE_Z[cls]
                        if cls in ('city', 'town'):
                            try:
                                big = int(float((tags.get('population') or '0').replace(',', '').split()[0])) > 300000
                            except (ValueError, IndexError):
                                big = False
                            if big or tags.get('capital') == 'yes':
                                z = 5
                        add('place', cls, label, z, [(o.lon, o.lat)])
                        n['place'] += 1
                cls = S.poi_class(tags)
                if cls is not None and (detail or cls in S.OVERVIEW_POI):
                    poi(cls, label_of(tags), o.lon, o.lat)
            elif o.is_way():
                hw = tags.get('highway')
                if hw is not None:
                    if hw == 'road':
                        hw = 'unclassified'
                    z = S.ROAD_Z.get(hw)
                    if z is None or tags.get('area') == 'yes' or not (detail or hw in S.OVERVIEW_ROADS):
                        continue
                    add('road', hw, tags.get('name') or tags.get('ref') or None, z, line(o, tol['road']))
                    n['road'] += 1
                    continue
                rw = tags.get('railway')
                if rw is not None:
                    z = S.RAIL_Z.get(rw)
                    if z is not None and tags.get('service') not in ('yard', 'siding', 'spur', 'crossover') and (detail or rw == 'rail'):
                        add('rail', rw, tags.get('name') or None, z, line(o, tol['rail']))
                    continue
                ww = tags.get('waterway')
                if ww is not None:
                    z = S.WATERWAY_Z.get(ww)
                    if z is not None and (detail or ww != 'stream'):
                        add('waterway', ww, tags.get('name') or None, z, line(o, tol['waterway']))
                    continue
                if tags.get('route') == 'ferry':
                    add('waterway', 'ferry', tags.get('name') or None, S.FERRY_Z, line(o, tol['waterway']))
                    continue
                if tags.get('boundary') == 'administrative':
                    # A border, where the way itself says whose (most of Europe's do; where only the relation says, it is not drawn).
                    # Not the lines out at sea that mark territorial waters.
                    border = S.BORDER.get(tags.get('admin_level'))
                    if border and tags.get('maritime') != 'yes' and (detail or border[0] == 'country'):
                        add('border', border[0], None, border[1], line(o, tol['waterway']))
            elif o.is_area():
                building = tags.get('building')
                cls = S.poi_class(tags)
                water = S.water_class(tags)
                land = None if water else S.land_class(tags)
                natural = tags.get('natural')
                protected = tags.get('boundary') in PROTECTED
                want_bldg = detail and buildings and building is not None and building != 'no'
                want_poi = cls is not None and (detail or cls in S.OVERVIEW_POI)
                if not (want_bldg or want_poi or water or land or protected or natural in ('wetland', 'beach')):
                    continue
                parts = _wkb_parts(bytes.fromhex(wkb.create_multipolygon(o)))[0]
                if not parts:
                    continue
                b = S.bounds(parts)
                if want_poi:
                    poi(cls, label_of(tags), (b[0] + b[1]) / 2, (b[2] + b[3]) / 2)
                if want_bldg:
                    add('bldg', '', None, 15, [S.simplify(parts[0], 0.000002)])
                    n['bldg'] += 1
                if water:
                    z = S.area_z(*b, 7)
                    if detail or z <= 11:
                        n['area'] += area('water', water, tags.get('name') or None, z, parts, tol['water'])
                elif natural == 'wetland':
                    z = S.area_z(*b, 7)
                    if detail or z <= 10:
                        area('wet', 'wetland', tags.get('name') or None, z, parts, tol['land'])
                elif natural == 'beach':
                    if detail:
                        add('beach', 'beach', tags.get('name') or None, S.area_z(*b, 10), list(parts))
                elif land and (detail or land in S.OVERVIEW_LAND):
                    z = S.area_z(*b, 9)
                    if detail:
                        n['area'] += area('land', land, tags.get('name') or None, z, parts, tol['land'])
                    elif land == 'green':
                        # An overview shows the great woods, for finding one's way: those some kilometres across, drawn broadly.
                        # Not every copse, meadow and patch of scrub.
                        if z <= 9 and (tags.get('landuse') == 'forest' or natural == 'wood'):
                            n['area'] += area('land', land, None, z, parts, tol['woods'])
                    elif z <= 10:
                        n['area'] += area('land', land, tags.get('name') or None, z, parts, tol['land'])
                elif protected:
                    z = S.area_z(*b, 8)
                    if detail or z <= 10:
                        area('land', 'park', None, z, parts, tol['park'])
        except (osmium.InvalidLocationError, RuntimeError, ValueError, struct.error):
            # a way with a node the extract does not hold (it crosses the extract's edge), a ring that will not close
            n['bad'] += 1
    flush(force=True)
    if node_index:
        # Once more, now that nothing read from the extracts is held: on Windows a file still mapped can be neither removed nor emptied
        o = tags = None
        import gc
        gc.collect()
        for i in range(len(files)):
            if os.path.exists(f'{node_index}.{i}'):
                forget_nodes(f'{node_index}.{i}')
    if n['bad'] > 200 and n['bad'] > 0.01 * (n['road'] + n['area'] + n['bldg']):
        # A few at an extract's edge are expected. Many means where the nodes are was not kept properly: a map with holes in it must not pass for whole.
        db.close()
        raise RuntimeError(f'{n["bad"]:,} of the map\'s lines and areas could not be placed: the record of where nodes are was not read back whole')
    stamp = min((s for s in stamps if s), default='')
    db.execute('BEGIN')
    if len(files) > 1:      # extracts overlap a little at their edges: one hospital, one town name
        db.execute('DELETE FROM poi WHERE id NOT IN (SELECT MIN(id) FROM poi GROUP BY cls, name, CAST(lon*20000 AS INT), CAST(lat*20000 AS INT))')
        twice = [r[0] for r in db.execute("SELECT id FROM feat WHERE layer='place' AND id NOT IN (SELECT MIN(id) FROM feat WHERE layer='place' GROUP BY cls, name, geom)")]
        db.executemany('DELETE FROM feat WHERE id=?', ((i,) for i in twice))
        db.executemany('DELETE FROM feat_idx WHERE id=?', ((i,) for i in twice))
        n['poi'] = db.execute('SELECT COUNT(*) FROM poi').fetchone()[0]
    db.execute('CREATE INDEX IF NOT EXISTS poi_cls ON poi(cls, lon, lat)')
    db.executemany('INSERT OR IGNORE INTO cover VALUES (?)', ((c,) for c in sorted(cells)))
    found = box[0] < 1e8
    meta = {'source': 'OpenStreetMap contributors, via Geofabrik (ODbL 1.0)', 'data_as_of': stamp, 'level': level, 'name': name,
            'bounds': ','.join(f'{v:.5f}' for v in box) if found else '', 'built': time.strftime('%Y-%m-%dT%H:%M:%S'), 'schema': '1'}
    db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', meta.items())
    db.execute('COMMIT')
    db.close()
    return {'features': n['id'], 'pois': n['poi'], 'places': n['place'], 'roads': n['road'], 'buildings': n['bldg'], 'areas': n['area'], 'unreadable': n['bad'],
            'cells': len(cells), 'data_as_of': stamp, 'box': box if found else None, 'seconds': round(time.time() - t0, 1), 'objects': n['seen']}
