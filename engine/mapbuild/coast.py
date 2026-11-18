"""The sea drawn where it really is.

The map paints sea everywhere, then a coarse outline of the world's land (web/world-context.json), then each tile:
first its 'ocean' polygons, then its 'island' polygons. So a region gives:

  ocean    its own outline (the area Geofabrik cut the extract to, which runs out to sea): wipes the coarse land there
  island   the real land inside and around that outline, from OpenStreetMap's coastline as osmdata.openstreetmap.de
           publishes it: land polygons cut into pieces of a degree or less

The land is stored several times at falling detail, each for a band of zoom levels (feat_idx.maxz ends a band), so a
view of a whole country does not draw every rock of its coast.

  Land(zip, folder)                    the published file made ready once: the .shp taken out, an index of its pieces
  add(db, land, outline, level, ...)   writes a region's ocean and island rows
"""
import sqlite3
import struct
import zipfile
from array import array
from pathlib import Path

import numpy as np

from engine import sqlitefs
from . import geom
from . import schema as S

# (first zoom, last zoom, grid in degrees a point must leave to be kept). About a pixel at the band's deepest zoom.
BANDS = {'detail': ((5, 6, 0.012), (7, 9, 0.0015), (10, 12, 0.0002), (13, S.MAXZ, 0)),
         'overview': ((5, 6, 0.012), (7, 9, 0.0015), (10, S.MAXZ, 0.0002))}
RASTER = 0.25           # the grid a region's outline is laid on to choose land pieces


class Land:
    """osmdata's land-polygons-split-4326.zip, ready to be asked for the land in a box."""

    def __init__(self, zip_path, folder=None, say=None):
        self.zip = Path(zip_path)
        self.folder = Path(folder or self.zip.parent)
        self.shp = self.folder / 'land_polygons.shp'
        self.index = self.folder / 'land-index.sqlite'
        self.say = say or (lambda *a: None)
        self._prepare()
        self.f = open(self.shp, 'rb')
        self.db = sqlite3.connect(f'file:{self.index}?mode=ro', uri=True)

    def close(self):
        self.f.close()
        self.db.close()

    def _stamp(self):
        st = self.zip.stat()
        return f'{st.st_size}:{int(st.st_mtime)}'

    def _prepare(self):
        try:
            db = sqlite3.connect(f'file:{self.index}?mode=ro', uri=True)
            ok = db.execute("SELECT value FROM meta WHERE key='zip'").fetchone() == (self._stamp(),) and self.shp.is_file()
            db.close()
            if ok:
                return
        except sqlite3.Error:
            pass
        self.say('reading the land polygons (once)')
        self.folder.mkdir(parents=True, exist_ok=True)
        for p in (self.index, self.shp):
            if p.exists():
                p.unlink()
        with zipfile.ZipFile(self.zip) as z:
            name = next(i.filename for i in z.infolist() if i.filename.endswith('.shp'))
            part = self.shp.with_suffix('.shp.part')
            with z.open(name) as src, open(part, 'wb') as out:
                while chunk := src.read(8 << 20):
                    out.write(chunk)
            part.replace(self.shp)
        db = sqlitefs.connect(self.index, lambda d: d.executescript(
            'CREATE VIRTUAL TABLE IF NOT EXISTS piece USING rtree(id, minx, maxx, miny, maxy, +at INTEGER, +points INTEGER);'
            'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);'), synchronous='OFF')
        rows, n = [], 0
        db.execute('BEGIN')
        with open(self.shp, 'rb') as f:
            f.seek(100)
            at = 100
            while True:
                h = f.read(52)                  # record header (8), shape type (4), box (32), parts (4), points (4)
                if len(h) < 52:
                    break
                size = struct.unpack_from('>i', h, 4)[0] * 2
                kind = struct.unpack_from('<i', h, 8)[0]
                if kind == 5:
                    x0, y0, x1, y1 = struct.unpack_from('<4d', h, 12)
                    parts, points = struct.unpack_from('<ii', h, 44)
                    if parts == 1:              # every piece of this file is one ring
                        n += 1
                        rows.append((n, x0, x1, y0, y1, at + 52 + 4, points))
                at += 8 + size
                f.seek(at)
                if len(rows) >= 50000:
                    db.executemany('INSERT INTO piece VALUES (?,?,?,?,?,?,?)', rows)
                    rows.clear()
        db.executemany('INSERT INTO piece VALUES (?,?,?,?,?,?,?)', rows)
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('zip', self._stamp()))
        db.execute('COMMIT')
        db.close()
        self.say(f'{n:,} pieces of land indexed')

    def pieces(self, minx, miny, maxx, maxy):
        """(minx, miny, maxx, maxy, points as an (n, 2) float64 array) for every piece touching the box."""
        for x0, x1, y0, y1, at, points in self.db.execute(
                'SELECT minx, maxx, miny, maxy, at, points FROM piece WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=? ORDER BY at',
                (minx, maxx, miny, maxy)).fetchall():
            self.f.seek(at)
            yield x0, y0, x1, y1, np.frombuffer(self.f.read(points * 16), '<f8').reshape(-1, 2)


def _pack(pts):
    """An (n, 2) array of degrees -> the packed one-ring blob of schema.pack."""
    q = np.rint(pts * 1e6).astype(np.int64)
    d = np.diff(q, axis=0, prepend=np.zeros((1, 2), np.int64)).astype(np.int32)
    return array('i', [1, len(q)]).tobytes() + d.tobytes()


def _thin(pts, grid):
    """Keeps a point when it falls in another grid square than the one before it; the ends always stay."""
    if grid <= 0 or len(pts) < 5:
        return pts
    q = np.floor(pts / grid).astype(np.int64)
    keep = np.ones(len(pts), bool)
    keep[1:] = (q[1:] != q[:-1]).any(axis=1)
    keep[-1] = True
    return pts[keep]


def add(db, land, outline, level='detail', first_id=None, pad=0.02):
    """Writes the ocean and island rows of a region whose outline is `outline` (a multipolygon). Returns counts."""
    box = geom.box_of(outline)
    if box is None:
        return {'ocean': 0, 'island': 0}
    next_id = first_id or (db.execute('SELECT COALESCE(MAX(id), 0) FROM feat').fetchone()[0] + 1)
    feat, idx = [], []

    def put(layer, minz, maxz, blob, x0, x1, y0, y1):
        nonlocal next_id
        feat.append((next_id, layer, '', None, minz, blob))
        idx.append((next_id, x0, x1, y0, y1, minz, maxz))
        next_id += 1

    n_ocean = 0
    first = BANDS[level][0][0]
    for poly in outline:
        parts = [[v for p in ring for v in (p[0], p[1])] for ring in poly if len(ring) >= 4]
        if parts:
            x0, x1, y0, y1 = S.bounds(parts)
            put('ocean', first, S.MAXZ, S.pack(parts), x0, x1, y0, y1)
            n_ocean += 1
    wanted = geom.cells(outline, RASTER)
    n_island = n_points = 0
    for x0, y0, x1, y1, pts in land.pieces(box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad):
        if not any(c in wanted for c in geom.box_cells(x0, y0, x1, y1, RASTER)):
            continue
        for minz, maxz, grid in BANDS[level]:
            ring = _thin(pts, grid)
            if len(ring) < 4:
                continue        # smaller than a pixel at these zooms
            put('island', minz, maxz, _pack(ring), x0, x1, y0, y1)
            n_island += 1
            n_points += len(ring)
        if len(feat) >= 20000:
            db.execute('BEGIN')
            db.executemany('INSERT INTO feat VALUES (?,?,?,?,?,?)', feat)
            db.executemany('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', idx)
            db.execute('COMMIT')
            feat.clear()
            idx.clear()
    db.execute('BEGIN')
    db.executemany('INSERT INTO feat VALUES (?,?,?,?,?,?)', feat)
    db.executemany('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', idx)
    db.execute('COMMIT')
    return {'ocean': n_ocean, 'island': n_island, 'points': n_points}
