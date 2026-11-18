"""Elevation for a region: terrain.sqlite, in the form derived/geo/terrain.sqlite has (tools/build_terrain.py), so the
map's shaded relief, contour lines, spot heights and 3D view work on it unchanged.

  tile(z, x, y, png)    256 px web-mercator "terrain-RGB" PNGs: height_m = -10000 + (R*65536 + G*256 + B) * 0.1

Source: Copernicus DEM, the European Space Agency's worldwide surface model, as 1 degree GeoTIFFs on AWS Open Data.
GLO-30 (about 30 m) for a detailed region, to zoom 12; GLO-90 for an overview, to zoom 9. Cells of open sea are not
published; a cell that is missing is sea. A tile that is flat sea all over is not stored.

The GeoTIFFs are read here (tiled, deflate, floating-point predictor): numpy is the only thing needed.
"""
import math
import struct
import time
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache

import numpy as np

from engine import sqlitefs
from . import geom, sources

SCHEMA = '''
CREATE TABLE IF NOT EXISTS tile(z INT, x INT, y INT, png BLOB, PRIMARY KEY(z, x, y)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
'''
LEVELS = {'detail': {'metres': 30, 'maxz': 12, 'superz': 8}, 'overview': {'metres': 90, 'maxz': 9, 'superz': 6}}
MINZ = 4


# ------------------------------------------------------------ GeoTIFF ----
def read_geotiff(path):
    """(heights float32 [rows, cols] with NaN for no data, west edge, north edge, degrees per column, degrees per row)."""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:2] == b'II':
        e = '<'
    elif data[:2] == b'MM':
        e = '>'
    else:
        raise ValueError('not a TIFF')
    if struct.unpack_from(e + 'H', data, 2)[0] != 42:
        raise ValueError('a BigTIFF: not read here')
    at = struct.unpack_from(e + 'I', data, 4)[0]
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8}
    codes = {1: 'B', 2: 'c', 3: 'H', 4: 'I', 6: 'b', 7: 'B', 8: 'h', 9: 'i', 11: 'f', 12: 'd', 16: 'Q'}
    tags = {}
    for i in range(struct.unpack_from(e + 'H', data, at)[0]):
        tag, kind, count, _ = struct.unpack_from(e + 'HHII', data, at + 2 + 12 * i)
        if kind not in codes:
            continue
        size = sizes[kind] * count
        where = at + 2 + 12 * i + 8 if size <= 4 else struct.unpack_from(e + 'I', data, at + 2 + 12 * i + 8)[0]
        tags[tag] = data[where:where + size] if kind == 2 else struct.unpack_from(e + str(count) + codes[kind], data, where)
    width, height = tags[256][0], tags[257][0]
    bits, fmt = tags.get(258, (32,))[0], tags.get(339, (1,))[0]
    compression, predictor = tags.get(259, (1,))[0], tags.get(317, (1,))[0]
    if (bits, fmt) != (32, 3) or tags.get(277, (1,))[0] != 1:
        raise ValueError(f'expected one band of 32-bit floats, found {bits} bits of format {fmt}')
    if compression not in (1, 8, 32946) or predictor not in (1, 3):
        raise ValueError(f'compression {compression} with predictor {predictor} is not read here')
    out = np.empty((height, width), np.float32)

    def block(raw, rows, cols):
        if compression != 1:
            raw = zlib.decompress(raw)
        if predictor == 3:      # each row: the four bytes of every float gathered by significance, then differenced along the row
            b = np.frombuffer(raw, np.uint8).reshape(rows, cols * 4)
            b = np.cumsum(b, axis=1, dtype=np.uint8).reshape(rows, 4, cols)
            return np.ascontiguousarray(b.transpose(0, 2, 1)).view('>f4').reshape(rows, cols)
        return np.frombuffer(raw, e + 'f4').reshape(rows, cols)
    if 322 in tags:
        tw, th = tags[322][0], tags[323][0]
        across = (width + tw - 1) // tw
        for n, (o, c) in enumerate(zip(tags[324], tags[325])):
            r0, c0 = (n // across) * th, (n % across) * tw
            if r0 >= height:
                break
            t = block(data[o:o + c], th, tw)
            out[r0:r0 + th, c0:c0 + tw] = t[:height - r0, :width - c0]
    else:
        per = tags.get(278, (height,))[0]
        for n, (o, c) in enumerate(zip(tags[273], tags[279])):
            r0 = n * per
            rows = min(per, height - r0)
            out[r0:r0 + rows] = block(data[o:o + c], rows, width)
    nodata = tags.get(42113)
    if nodata:
        try:
            out[out == np.float32(float(nodata.split(b'\0')[0].decode().strip()))] = np.nan
        except ValueError:
            pass
    out[out < -1000] = np.nan
    scale, tie = tags[33550], tags[33922]
    return out, tie[3] - tie[0] * scale[0], tie[4] + tie[1] * scale[1], scale[0], scale[1]


# -------------------------------------------------------------- tiles ----
def tile_lonlat(z, x, y, n):
    """Pixel-centre longitudes (n,) and latitudes (n,) for an n-px square covering tile z/x/y."""
    t = (np.arange(n) + 0.5) / n
    lon = (x + t) / 2 ** z * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * (y + t) / 2 ** z))))
    return lon, lat


def png(h):
    """256x256 heights (m, NaN = sea level) -> terrain-RGB PNG bytes (filter 'Up' per row for compression)."""
    v = np.clip(np.round((np.nan_to_num(h, nan=0.0) + 10000) * 10), 0, 2 ** 24 - 1).astype(np.uint32)
    rgb = np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], -1).astype(np.uint8).reshape(h.shape[0], -1)
    up = rgb.copy()
    up[1:] = rgb[1:] - rgb[:-1]
    raw = np.hstack([np.full((h.shape[0], 1), 2, np.uint8), up]).tobytes()

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', h.shape[1], h.shape[0], 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def down(H):
    """Halve resolution, ignoring NaN (mean of available samples)."""
    s = H.reshape(H.shape[0] // 2, 2, H.shape[1] // 2, 2)
    if not np.isnan(s).any():
        return s.mean(axis=(1, 3))
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        return np.nanmean(s, axis=(1, 3)).astype(np.float32)


_CELLS = {}


@lru_cache(maxsize=8)
def _dem(path):
    return read_geotiff(path)


def sample(lon, lat):
    """Bilinear heights (m) on the lon x lat grid; NaN where there is no source data."""
    H = np.full((lat.size, lon.size), np.nan, np.float32)
    for south in range(math.floor(lat.min()), math.floor(lat.max()) + 1):
        for west in range(math.floor(lon.min()), math.floor(lon.max()) + 1):
            path = _CELLS.get((south, west))
            if path is None:
                continue
            a, ox, oy, sx, sy = _dem(path)
            rows = np.nonzero((lat >= south) & (lat < south + 1))[0]
            cols = np.nonzero((lon >= west) & (lon < west + 1))[0]
            if not rows.size or not cols.size:
                continue
            fr = (oy - lat[rows]) / sy - 0.5
            fc = (lon[cols] - ox) / sx - 0.5
            r0 = np.clip(np.floor(fr).astype(int), 0, a.shape[0] - 2)
            c0 = np.clip(np.floor(fc).astype(int), 0, a.shape[1] - 2)
            wr = np.clip(fr - r0, 0, 1)[:, None].astype(np.float32)
            wc = np.clip(fc - c0, 0, 1)[None, :].astype(np.float32)
            H[np.ix_(rows, cols)] = (a[np.ix_(r0, c0)] * (1 - wr) * (1 - wc) + a[np.ix_(r0 + 1, c0)] * wr * (1 - wc)
                                     + a[np.ix_(r0, c0 + 1)] * (1 - wr) * wc + a[np.ix_(r0 + 1, c0 + 1)] * wr * wc)
    return H


def _land(h):
    return not np.isnan(h).all() and bool(np.nanmax(np.abs(h)) > 0.05)


_COVER = set()
COVER_CELL = 0.25


def _init(cells, cover=()):
    """This build's cells and ground, and no other's: a process that builds several regions starts each one clean."""
    _CELLS.clear()
    _CELLS.update(cells)
    _COVER.clear()
    _COVER.update(cover)
    _dem.cache_clear()


def tile_box(z, x, y):
    """west, south, east, north"""
    n = 2 ** z
    return (x / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n)))),
            (x + 1) / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))))


def _wanted(z, x, y):
    """Does this tile touch the region? (A region's block of tiles reaches past it, over its neighbours' hills.)"""
    if not _COVER:
        return True
    w, s, e, n = tile_box(z, x, y)
    return any(c in _COVER for c in geom.box_cells(w, s, e, n, COVER_CELL))


def _supertile(args):
    """Every tile from superz down to maxz inside one supertile. Returns (key, tiles, heights at superz or None)."""
    x, y, superz, maxz = args
    span = 2 ** (maxz - superz)
    lon, lat = tile_lonlat(superz, x, y, 256 * span)
    H = sample(lon, lat)
    if not _land(H):
        return (x, y), [], None
    out = []
    for z in range(maxz, superz - 1, -1):
        n = 2 ** (z - superz)
        for j in range(n):
            for i in range(n):
                h = H[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256]
                if _land(h) and _wanted(z, x * n + i, y * n + j):
                    out.append((z, x * n + i, y * n + j, png(h)))
        if z > superz:
            H = down(H)
    return (x, y), out, H.astype(np.float32)


def tile_of(lon, lat, z):
    n = 2 ** z
    s = math.sin(math.radians(max(-85.05, min(85.05, lat))))
    return int((lon + 180) / 360 * n), int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)


def build(out, outline, src, level='detail', workers=2, say=None, control=None):
    """out: a new terrain.sqlite. outline: the region's multipolygon. src: sources.Sources (DEM cells are fetched through it)."""
    say = say or (lambda *a: None)
    spec = LEVELS[level]
    metres, maxz, superz = spec['metres'], spec['maxz'], spec['superz']
    t0 = time.time()
    box = geom.box_of(outline)
    covered = geom.cells(outline, COVER_CELL)
    x0, y1 = tile_of(box[0], box[1], superz)
    x1, y0 = tile_of(box[2], box[3], superz)
    jobs, wanted = [], set()
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            w, s, e, n_ = tile_box(superz, x, y)
            if any(c in covered for c in geom.box_cells(w, s, e, n_, COVER_CELL)):
                jobs.append((x, y, superz, maxz))
                # every cell under the block, not only those under the region: a tile is whole, whichever region made it
                wanted.update(geom.box_cells(w + 1e-9, s + 1e-9, e - 1e-9, n_ - 1e-9, 1.0))
    jobs.sort(key=lambda j: (j[1] // 2, j[0]))        # neighbours together, so the cells they share stay in memory
    cells, absent = {}, 0
    wanted = sorted(wanted)
    for n, (c, r) in enumerate(wanted, 1):
        if control is not None:
            control.check()
        lat, lon = r - 90, c - 180
        url = sources.dem_url(lat, lon, metres)
        path = src.get(url, f'dem{metres}/{url.rsplit("/", 1)[1]}', missing_ok=True)
        if path is None:
            absent += 1
        else:
            cells[(lat, lon)] = str(path)
        if n % 25 == 0:
            say(f'  elevation cells: {n} of {len(wanted)}')
    say(f'  {len(cells)} elevation cells ({absent} are open sea)')
    db = sqlitefs.connect(out, lambda d: d.executescript(SCHEMA), synchronous='OFF')
    db.execute('PRAGMA journal_mode=OFF')
    low, tiles, done = {}, 0, 0

    def keep(result):
        nonlocal tiles, done
        key, made, h = result
        db.execute('BEGIN')
        db.executemany('INSERT OR REPLACE INTO tile VALUES (?,?,?,?)', made)
        db.execute('COMMIT')
        tiles += len(made)
        done += 1
        if h is not None:
            low[key] = h
        if done % 20 == 0:
            say(f'  elevation: {done} of {len(jobs)} blocks, {tiles:,} tiles')
        if control is not None:
            control.check()
    if workers and workers > 1:
        with ProcessPoolExecutor(workers, initializer=_init, initargs=(cells, covered)) as pool:
            for f in as_completed([pool.submit(_supertile, j) for j in jobs]):
                keep(f.result())
    else:
        _init(cells, covered)
        for j in jobs:
            keep(_supertile(j))
    level_now = low
    db.execute('BEGIN')
    for z in range(superz - 1, MINZ - 1, -1):
        nxt = {}
        for (x, y) in {(x // 2, y // 2) for x, y in level_now}:
            q = np.full((512, 512), np.nan, np.float32)
            for dy in (0, 1):
                for dx in (0, 1):
                    h = level_now.get((2 * x + dx, 2 * y + dy))
                    if h is not None:
                        q[dy * 256:(dy + 1) * 256, dx * 256:(dx + 1) * 256] = h
            h = down(q)
            nxt[(x, y)] = h
            db.execute('INSERT OR REPLACE INTO tile VALUES (?,?,?,?)', (z, x, y, png(h)))
            tiles += 1
        level_now = nxt
    meta = {'built': time.strftime('%Y-%m-%dT%H:%M:%S'), 'source': f'Copernicus DEM GLO-{metres}', 'credit': sources.CREDITS['dem'],
            'maxz': str(maxz), 'minz': str(MINZ), 'wholez': str(superz), 'cells': str(len(cells))}
    db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', meta.items())
    db.execute('COMMIT')
    db.close()
    return {'tiles': tiles, 'cells': len(cells), 'maxz': maxz, 'metres': metres, 'seconds': round(time.time() - t0, 1)}
