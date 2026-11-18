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
               'healthcare', 'place', 'building', 'boundary')
AREA_KEYS = ('natural', 'landuse', 'leisure', 'amenity', 'waterway', 'boundary', 'building', 'shop', 'tourism', 'man_made', 'healthcare')
BATCH = 40000
PROTECTED = {'national_park', 'protected_area'}


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
    pairs += [('railway', 'rail'), ('waterway', 'river'), ('waterway', 'canal'), ('natural', 'wetland'), ('natural', 'beach')]
    pairs += [('place', v) for v in S.OVERVIEW_PLACES] + [('place', k) for k, v in S.PLACE_AS.items() if v in S.OVERVIEW_PLACES]
    pairs += [k for k, cls in S.LAND.items() if cls in S.OVERVIEW_LAND] + list(S.WATER)
    pairs += [k for k, cls in S.POI.items() if cls in S.OVERVIEW_POI]
    pairs += [('boundary', v) for v in PROTECTED]
    return osmium.filter.TagFilter(*sorted(set(pairs)))


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
                try:
                    os.unlink(f'{node_index}.{i}')
                except OSError:
                    pass

    for o in objects():
        n['seen'] += 1
        tags = o.tags
        try:
            if o.is_node():
                place = tags.get('place')
                if place is not None:
                    cls = S.PLACE_AS.get(place, place)
                    label = label_of(tags)
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
                    add('road', hw, tags.get('name') or tags.get('ref') or None, z, _wkb_parts(bytes.fromhex(wkb.create_linestring(o)))[0])
                    n['road'] += 1
                    continue
                rw = tags.get('railway')
                if rw is not None:
                    z = S.RAIL_Z.get(rw)
                    if z is not None and tags.get('service') not in ('yard', 'siding', 'spur', 'crossover') and (detail or rw == 'rail'):
                        add('rail', rw, tags.get('name') or None, z, _wkb_parts(bytes.fromhex(wkb.create_linestring(o)))[0])
                    continue
                ww = tags.get('waterway')
                if ww is not None:
                    z = S.WATERWAY_Z.get(ww)
                    if z is not None and (detail or ww != 'stream'):
                        add('waterway', ww, tags.get('name') or None, z, _wkb_parts(bytes.fromhex(wkb.create_linestring(o)))[0])
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
                tol = 0.00001 if detail else 0.00005
                if water:
                    z = S.area_z(*b, 7)
                    if detail or z <= 11:
                        add('water', water, tags.get('name') or None, z, [S.simplify(p, tol) for p in parts])
                        n['area'] += 1
                elif natural == 'wetland':
                    z = S.area_z(*b, 7)
                    if detail or z <= 10:
                        add('wet', 'wetland', tags.get('name') or None, z, [S.simplify(p, tol) for p in parts])
                elif natural == 'beach':
                    if detail:
                        add('beach', 'beach', tags.get('name') or None, S.area_z(*b, 10), list(parts))
                elif land and (detail or land in S.OVERVIEW_LAND):
                    z = S.area_z(*b, 9)
                    if detail or z <= 10:
                        add('land', land, tags.get('name') or None, z, [S.simplify(p, tol) for p in parts])
                        n['area'] += 1
                elif protected:
                    z = S.area_z(*b, 8)
                    if detail or z <= 10:
                        add('land', 'park', None, z, [S.simplify(p, 0.00002 if detail else 0.0001) for p in parts])
        except (osmium.InvalidLocationError, RuntimeError, ValueError, struct.error):
            # a way with a node the extract does not hold (it crosses the extract's edge), a ring that will not close
            n['bad'] += 1
    flush(force=True)
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
