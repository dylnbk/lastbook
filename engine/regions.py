"""Maps of anywhere, side by side: reading derived/geo/regions/<id>/.

Each region is a folder complete in itself (engine/mapbuild makes them; this module only reads):

  region.json      id, name, level ('detail' or 'overview'), box [west, south, east, north], outline, credits, dates
  map.sqlite       feat + feat_idx (with minz and maxz) + poi + cover + meta
  places.sqlite    place + place_fts, as derived/geo/places.sqlite
  terrain.sqlite   tile(z, x, y, png), as derived/geo/terrain.sqlite            (optional)

A folder whose name starts with a dot is one being built or removed, and is not seen. Adding a region is putting its
folder there; removing one is taking it away. The app notices within a few seconds, with no restart.

Where a detailed region and an overview both cover a place, the detailed one is drawn and the overview's help points
there are left out (the overview's own 'cover' is empty; a detailed region lists the 0.05 degree cells it maps).

  Atlas(root).regions()                       the regions here now
  .rows(z, west, south, east, north)          features for a tile
  .poi_sources(west, south, east, north)      [(connection, extra SQL)] for help points
  .terrain(z, x, y)                           an elevation tile, or None
  .place_dbs()                                the place finders
  .contains(lon, lat) / .level_at(lon, lat)   is this point on one of these maps
"""
import json
import os
import sqlite3
import struct
import threading
import time
import zlib
from pathlib import Path

CELL = 0.05
RESCAN = 4.0            # seconds between looks at the folder
TILE_SQL = ('SELECT f.layer,f.cls,f.name,f.geom FROM feat_idx i JOIN feat f ON f.id=i.id '
            'WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=? AND i.minz<=? AND i.maxz>=? ORDER BY f.id')
_tls = threading.local()


def _ro(path):
    return 'file:' + str(path).replace('?', '%3f').replace('#', '%23') + '?mode=ro'


class Region:
    def __init__(self, folder, info, stamp):
        self.folder = Path(folder)
        self.info = info
        self.stamp = stamp
        self.id = info.get('id') or self.folder.name
        self.name = info.get('name') or self.id
        self.level = 'overview' if info.get('level') == 'overview' else 'detail'
        box = info.get('box') or [-180, -85, 180, 85]
        self.west, self.south, self.east, self.north = (float(v) for v in box[:4])
        self.box = [self.west, self.south, self.east, self.north]
        self.map = self.folder / 'map.sqlite'
        self.places = self.folder / 'places.sqlite'
        self.terrain = self.folder / 'terrain.sqlite'
        self.has_places = self.places.is_file()
        self.has_terrain = self.terrain.is_file()
        self.outline = info.get('outline') or []
        self._cover = None
        self._lock = threading.Lock()

    def touches(self, west, south, east, north):
        return self.east >= west and self.west <= east and self.north >= south and self.south <= north

    def inside(self, lon, lat):
        """In the region's outline (its box, when it has no outline)."""
        if not (self.west <= lon <= self.east and self.south <= lat <= self.north):
            return False
        if not self.outline:
            return True
        for poly in self.outline:
            hit = False
            for ring in poly:
                j = len(ring) - 1
                for i in range(len(ring)):
                    xi, yi = ring[i]
                    xj, yj = ring[j]
                    if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                        hit = not hit
                    j = i
            if hit:
                return True
        return False

    def db(self, what='map'):
        """This thread's read-only connection to one of the region's files."""
        conns = getattr(_tls, 'conns', None)
        if conns is None:
            conns = _tls.conns = {}
        key = (str(self.folder), what, self.stamp)
        c = conns.get(key)
        if c is None:
            for old in [k for k in conns if k[0] == key[0] and k[1] == what]:       # the region was rebuilt: let the old file go
                try:
                    conns.pop(old).close()
                except sqlite3.Error:
                    pass
            c = conns[key] = sqlite3.connect(_ro(self.folder / f'{what}.sqlite'), uri=True, check_same_thread=False)
            c.execute('PRAGMA query_only=ON')
        return c

    def cover(self):
        """The 0.05 degree cells this region maps in detail (empty for an overview)."""
        if self._cover is None:
            with self._lock:
                if self._cover is None:
                    cells = frozenset()
                    if self.level == 'detail':
                        try:
                            cells = frozenset(r[0] for r in self.db().execute('SELECT cell FROM cover'))
                        except sqlite3.Error:
                            pass
                    self._cover = cells
        return self._cover

    def summary(self):
        i = self.info
        counts = i.get('counts') or {}
        return {'id': self.id, 'name': self.name, 'level': self.level, 'box': self.box, 'data_as_of': i.get('data_as_of') or '',
                'built': i.get('built') or '', 'features': counts.get('features', 0), 'help_points': counts.get('pois', 0),
                'terrain': self.has_terrain, 'bytes': sum((i.get('bytes') or {}).values())}


class Atlas:
    def __init__(self, root):
        self.root = Path(root)
        self._regions = []
        self._at = 0.0
        self._sig = None
        self._version = 'r0'
        self._lock = threading.Lock()
        self._cells = None
        self._classes = None

    # ------------------------------------------------------------ what is here ----
    def _scan(self):
        found = []
        try:
            entries = sorted(os.scandir(self.root), key=lambda e: e.name)
        except OSError:
            entries = []
        for e in entries:
            if e.name.startswith('.') or not e.is_dir():
                continue
            note = os.path.join(e.path, 'region.json')
            try:
                st = os.stat(note)
                if not os.path.isfile(os.path.join(e.path, 'map.sqlite')):
                    continue
                found.append((e.path, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        return found

    def regions(self):
        now = time.monotonic()
        if now - self._at < RESCAN and self._sig is not None:
            return self._regions
        with self._lock:
            if now - self._at < RESCAN and self._sig is not None:
                return self._regions
            sig = self._scan()
            if sig != self._sig:
                regions = []
                for path, mtime, size in sig:
                    try:
                        with open(os.path.join(path, 'region.json'), encoding='utf-8') as f:
                            info = json.load(f)
                        regions.append(Region(path, info, mtime))
                    except (OSError, ValueError, TypeError):
                        continue
                regions.sort(key=lambda r: (r.level != 'detail', r.name))       # detailed first: theirs is the tile to prefer
                self._regions, self._sig = regions, sig
                self._cells = self._classes = None
                self._version = 'r%x' % (zlib.crc32(repr(sig).encode()) & 0xffffffff) if sig else 'r0'
            self._at = now
            return self._regions

    def version(self):
        self.regions()
        return self._version

    def __bool__(self):
        return bool(self.regions())

    # ------------------------------------------------------------------ cover ----
    def cells(self):
        """Every cell some detailed region maps."""
        regions = self.regions()
        if self._cells is None:
            out = set()
            for r in regions:
                out |= r.cover()
            self._cells = frozenset(out)
        return self._cells

    def covered(self, lon, lat):
        return int((lon + 180) / CELL) * 100000 + int((lat + 90) / CELL) in self.cells()

    def box_covered(self, west, south, east, north):
        """Is every cell of this box mapped in detail?"""
        cells = self.cells()
        if not cells:
            return False
        return all(i * 100000 + j in cells for i in range(int((west + 180) / CELL), int((east + 180) / CELL) + 1)
                   for j in range(int((south + 90) / CELL), int((north + 90) / CELL) + 1))

    def contains(self, lon, lat):
        return any(r.inside(lon, lat) for r in self.regions())

    def at(self, lon, lat):
        """The region a point is on, a detailed one before an overview, or None."""
        for r in self.regions():        # detailed regions come first
            if r.inside(lon, lat):
                return r
        return None

    def level_at(self, lon, lat):
        """'detail', 'overview' or None for a point."""
        r = self.at(lon, lat)
        return r.level if r else None

    def bounds(self):
        rs = self.regions()
        if not rs:
            return None
        return [min(r.west for r in rs), min(r.south for r in rs), max(r.east for r in rs), max(r.north for r in rs)]

    # ------------------------------------------------------------------ tiles ----
    def rows(self, z, west, south, east, north):
        """(layer, cls, name, geom) for everything the regions draw in this box at this zoom."""
        rows = []
        skip_overview = z >= 11 and self.box_covered(west, south, east, north)
        for r in self.regions():
            if not r.touches(west, south, east, north) or (skip_overview and r.level == 'overview'):
                continue
            try:
                rows += r.db().execute(TILE_SQL, (west, east, south, north, z, z)).fetchall()
            except sqlite3.Error:
                continue        # being replaced this moment: the next tile sees the new one
        return rows

    # ------------------------------------------------------------ help points ----
    def poi_sources(self, west, south, east, north):
        """(connection, SQL to add to a WHERE) for each region with help points in the box."""
        out = []
        details = [r for r in self.regions() if r.level == 'detail']
        for r in self.regions():
            if not r.touches(west, south, east, north):
                continue
            try:
                db = r.db()
            except sqlite3.Error:
                continue
            extra = ''
            if r.level == 'overview' and any(d.touches(r.west, r.south, r.east, r.north) for d in details):
                db.create_function('lb_covered', 2, lambda lo, la: 1 if self.covered(lo, la) else 0)    # each time: cheap, and never stale
                extra = ' AND lb_covered(lon,lat)=0'
            out.append((db, extra))
        return out

    def classes(self):
        """Every kind of help point any region holds."""
        regions = self.regions()
        if self._classes is None:
            out = set()
            for r in regions:
                try:
                    out |= {row[0] for row in r.db().execute('SELECT DISTINCT cls FROM poi')}
                except sqlite3.Error:
                    pass
            self._classes = frozenset(out)
        return self._classes

    def place_dbs(self):
        out = []
        for r in self.regions():
            if r.has_places:
                try:
                    out.append((r, r.db('places')))
                except sqlite3.Error:
                    pass
        return out

    # ---------------------------------------------------------------- terrain ----
    def terrain(self, z, x, y):
        """The elevation tile, or None. Below the zoom a region's tiles are whole at, each region knows only its own ground:
        the tiles of every region there are laid together."""
        n = 2 ** z
        west, east = x / n * 360 - 180, (x + 1) / n * 360 - 180
        import math
        north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
        got = []
        for r in self.regions():
            if not r.has_terrain or not r.touches(west, south, east, north):
                continue
            try:
                row = r.db('terrain').execute('SELECT png FROM tile WHERE z=? AND x=? AND y=?', (z, x, y)).fetchone()
            except sqlite3.Error:
                continue
            if row:
                whole = z >= (8 if r.level == 'detail' else 6)
                if whole:
                    return row[0]
                got.append(row[0])
        if not got:
            return None
        if len(got) == 1:
            return got[0]
        return _merged(tuple(got))

    def has_terrain(self):
        return any(r.has_terrain for r in self.regions())

    def terrain_detail(self):
        """Is there elevation fine enough for contour lines (a detailed region with terrain)?"""
        return any(r.has_terrain and r.level == 'detail' for r in self.regions())

    def summary(self):
        return [r.summary() for r in self.regions()]

    def credits(self):
        out = []
        for r in self.regions():
            for c in r.info.get('credits') or []:
                if c not in out:
                    out.append(c)
        return out

    def data_as_of(self):
        """(oldest, newest) date of the map data, as YYYY-MM-DD, or None."""
        days = sorted(d[:10] for d in (r.info.get('data_as_of') or '' for r in self.regions()) if len(d) >= 10)
        return (days[0], days[-1]) if days else None


class TileStore:
    """Zoomed-out map tiles, which take longest to put together, kept once made: one SQLite file beside the regions
    (regions/.tiles.sqlite), not a file per tile. A drive that cannot be written to simply goes without."""

    def __init__(self, path):
        self.path = Path(path)
        self.db = None
        self.off = False
        self.lock = threading.Lock()

    def _open(self):
        if self.db is None and not self.off:
            with self.lock:
                if self.db is None and not self.off:
                    try:
                        from engine import sqlitefs
                        self.db = sqlitefs.Database(self.path, lambda d: d.execute(
                            'CREATE TABLE IF NOT EXISTS tile(z INT, x INT, y INT, version TEXT, data BLOB, PRIMARY KEY(z, x, y)) WITHOUT ROWID'), synchronous='OFF')
                    except (sqlite3.Error, OSError):
                        self.off = True
        return self.db

    def get(self, version, z, x, y):
        db = self._open()
        if db is None:
            return None
        try:
            row = db.run(lambda c: c.execute('SELECT data FROM tile WHERE z=? AND x=? AND y=? AND version=?', (z, x, y, version)).fetchone())
            return zlib.decompress(row[0]) if row else None
        except (sqlite3.Error, OSError, zlib.error):
            return None

    def put(self, version, z, x, y, data):
        db = self._open()
        if db is None:
            return
        try:
            db.run(lambda c: c.execute('INSERT OR REPLACE INTO tile VALUES (?,?,?,?,?)', (z, x, y, version, zlib.compress(data, 1))))
        except (sqlite3.Error, OSError):
            self.off = True


_stores = {}


def tile_store(root):
    key = str(root)
    if key not in _stores:
        _stores[key] = TileStore(Path(root) / '.tiles.sqlite')
    return _stores[key]


_merge_lock = threading.Lock()
_merge_cache = {}


def _merged(pngs):
    """Several regions' copies of one low-zoom elevation tile -> one: at each point the first that has ground there."""
    key = tuple(zlib.crc32(p) for p in pngs)
    with _merge_lock:
        hit = _merge_cache.get(key)
    if hit is not None:
        return hit
    try:
        import numpy as np
    except ImportError:
        return pngs[0]
    out = None
    for png in pngs:
        v = _decode(png, np)
        if v is None:
            continue
        out = v if out is None else np.where(out == 100000, v, out)       # 100000 is sea level: height 0
    if out is None:
        return pngs[0]
    data = _encode(out, np)
    with _merge_lock:
        if len(_merge_cache) > 256:
            _merge_cache.clear()
        _merge_cache[key] = data
    return data


def _decode(png, np):
    """A 256x256 terrain-RGB PNG as written by engine/mapbuild/terrain.py -> its 24-bit values, or None."""
    try:
        at, idat = 8, b''
        while at < len(png):
            size, kind = struct.unpack_from('>I4s', png, at)
            if kind == b'IHDR':
                w, h, depth, colour = struct.unpack_from('>IIBB', png, at + 8)
                if (w, h, depth, colour) != (256, 256, 8, 2):
                    return None
            elif kind == b'IDAT':
                idat += png[at + 8:at + 8 + size]
            at += 12 + size
        raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(256, 769)
        if not (raw[:, 0] == 2).all():
            return None         # another filter than 'Up': not one of ours
        rgb = np.cumsum(raw[:, 1:], axis=0, dtype=np.uint8).reshape(256, 256, 3).astype(np.uint32)
        return (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
    except (struct.error, zlib.error, ValueError):
        return None


def _encode(v, np):
    rgb = np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], -1).astype(np.uint8).reshape(256, -1)
    up = rgb.copy()
    up[1:] = rgb[1:] - rgb[:-1]
    raw = np.hstack([np.full((256, 1), 2, np.uint8), up]).tobytes()

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 256, 256, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))
