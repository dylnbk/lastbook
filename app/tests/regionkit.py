"""Made-up map regions for tests: a region folder written by hand, a GeoTIFF of chosen heights, a tiny .osm file.
Nothing here needs osmium. Never packaged."""
import json
import sqlite3
import struct
import zlib
from pathlib import Path

from engine.mapbuild import places, schema


def make_region(root, rid, level='detail', name=None, box=None, feats=(), pois=(), outline=None, finder=True, postcodes=(), data_as_of='2026-11-17T21:24:30Z'):
    """root/<rid>/ with region.json, map.sqlite and places.sqlite.
    feats: (layer, cls, name, minz, maxz, parts) with parts as [[lon, lat, lon, lat, ...], ...]. pois: (cls, name, lon, lat)."""
    folder = Path(root) / rid
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / 'map.sqlite')
    db.executescript(schema.SCHEMA)
    cells = set()
    xs, ys = [], []
    for i, (layer, cls, label, minz, maxz, parts) in enumerate(feats, 1):
        minx, maxx, miny, maxy = schema.bounds(parts)
        db.execute('INSERT INTO feat VALUES (?,?,?,?,?,?)', (i, layer, cls, label, minz, schema.pack(parts)))
        db.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (i, minx, maxx, miny, maxy, minz, maxz))
        xs += [minx, maxx]
        ys += [miny, maxy]
        if level == 'detail' and maxx - minx < schema.CELL and maxy - miny < schema.CELL:
            cells.add(schema.cell(parts[0][0], parts[0][1]))
    for i, (cls, label, lon, lat) in enumerate(pois, 1):
        db.execute('INSERT INTO poi VALUES (?,?,?,?,?)', (i, cls, label, lon, lat))
        xs.append(lon)
        ys.append(lat)
        if level == 'detail':
            cells.add(schema.cell(lon, lat))
    db.execute('CREATE INDEX poi_cls ON poi(cls, lon, lat)')
    db.executemany('INSERT INTO cover VALUES (?)', ((c,) for c in cells))
    db.commit()
    db.close()
    box = list(box or (min(xs) - 0.01, min(ys) - 0.01, max(xs) + 0.01, max(ys) + 0.01))
    counts = {'features': len(feats), 'pois': len(pois)}
    if finder:
        counts['finder'] = places.build(folder / 'map.sqlite', folder / 'places.sqlite', name or rid, level=level, postcodes=postcodes, outline=outline)
    info = {'id': rid, 'name': name or rid, 'level': level, 'box': box, 'schema': 1, 'data_as_of': data_as_of, 'counts': counts,
            'credits': ['Map data © OpenStreetMap contributors'] + (['Postcodes: GeoNames (geonames.org), CC BY 4.0'] if postcodes else []),
            'outline': outline or [], 'built': '2026-11-18T10:00:00',
            'bytes': {p.name: p.stat().st_size for p in folder.iterdir() if p.suffix == '.sqlite'}}
    (folder / 'region.json').write_text(json.dumps(info), encoding='utf-8')
    return folder


def add_terrain(folder, tiles):
    """tiles: {(z, x, y): 256x256 array of metres}. Adds terrain.sqlite to a region made by make_region."""
    from engine.mapbuild import terrain
    db = sqlite3.connect(Path(folder) / 'terrain.sqlite')
    db.executescript(terrain.SCHEMA)
    db.executemany('INSERT INTO tile VALUES (?,?,?,?)', [(z, x, y, terrain.png(h)) for (z, x, y), h in tiles.items()])
    db.commit()
    db.close()
    note = Path(folder) / 'region.json'
    info = json.loads(note.read_text(encoding='utf-8'))
    info['bytes']['terrain.sqlite'] = (Path(folder) / 'terrain.sqlite').stat().st_size
    note.write_text(json.dumps(info), encoding='utf-8')


def heights(png):
    """A terrain-RGB PNG -> 256x256 array of metres."""
    import numpy as np
    at, idat = 8, b''
    while at < len(png):
        size, kind = struct.unpack_from('>I4s', png, at)
        if kind == b'IDAT':
            idat += png[at + 8:at + 8 + size]
        at += 12 + size
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(256, 769)
    rgb = np.cumsum(raw[:, 1:], axis=0, dtype=np.uint8).reshape(256, 256, 3).astype(np.int64)
    return -10000 + (rgb[..., 0] * 65536 + rgb[..., 1] * 256 + rgb[..., 2]) * 0.1


def write_geotiff(path, a, west, north, dx, dy, tile=64, deflate=True, predictor=3, nodata=None):
    """A one-band float32 GeoTIFF as Copernicus publishes them: tiled, deflate, floating-point predictor.
    tile=0 writes strips instead. (west, north) is the outer corner of the top-left pixel."""
    import numpy as np
    a = np.asarray(a, np.float32)
    rows, cols = a.shape

    def block(t):
        if predictor == 3:
            b = np.ascontiguousarray(t.astype('>f4')).view(np.uint8).reshape(t.shape[0], t.shape[1], 4).transpose(0, 2, 1).reshape(t.shape[0], -1)
            raw = np.diff(b, axis=1, prepend=np.zeros((t.shape[0], 1), np.uint8)).astype(np.uint8).tobytes()
        else:
            raw = t.astype('<f4').tobytes()
        return zlib.compress(raw) if deflate else raw
    blocks = []
    if tile:
        for r in range(0, rows, tile):
            for c in range(0, cols, tile):
                t = np.zeros((tile, tile), np.float32)
                part = a[r:r + tile, c:c + tile]
                t[:part.shape[0], :part.shape[1]] = part
                blocks.append(block(t))
    else:
        per = 16
        for r in range(0, rows, per):
            blocks.append(block(a[r:r + per]))
    tags = [(256, 4, [cols]), (257, 4, [rows]), (258, 3, [32]), (259, 3, [8 if deflate else 1]), (277, 3, [1]), (317, 3, [predictor]), (339, 3, [3])]
    if tile:
        tags += [(322, 4, [tile]), (323, 4, [tile]), (324, 4, None), (325, 4, [len(b) for b in blocks])]
    else:
        tags += [(273, 4, None), (278, 4, [16]), (279, 4, [len(b) for b in blocks])]
    tags += [(33550, 12, [dx, dy, 0.0]), (33922, 12, [0.0, 0.0, 0.0, west, north, 0.0])]
    if nodata is not None:
        tags.append((42113, 2, (str(nodata) + '\0').encode()))
    tags.sort()
    code = {3: 'H', 4: 'I', 12: 'd'}
    size = {2: 1, 3: 2, 4: 4, 12: 8}
    head = 8 + 2 + 12 * len(tags) + 4
    extra = b''
    where = {}
    for tag, kind, values in tags:
        n = len(values) if values is not None else len(blocks)
        if size[kind] * n > 4:
            where[tag] = head + len(extra)
            extra += b'\0' * (size[kind] * n)
    start = head + len(extra)
    offsets, at = [], start
    for b in blocks:
        offsets.append(at)
        at += len(b)
    out = bytearray(b'II' + struct.pack('<HI', 42, 8) + struct.pack('<H', len(tags)))
    body = bytearray(extra)
    for tag, kind, values in tags:
        if values is None:
            values = offsets
        n = len(values)
        packed = bytes(values) if kind == 2 else struct.pack('<%d%s' % (n, code[kind]), *values)
        if tag in where:
            out += struct.pack('<HHII', tag, kind, n, where[tag])
            body[where[tag] - head:where[tag] - head + len(packed)] = packed
        else:
            out += struct.pack('<HHI', tag, kind, n) + packed.ljust(4, b'\0')
    out += struct.pack('<I', 0) + body
    for b in blocks:
        out += b
    Path(path).write_bytes(bytes(out))
    return path


def write_osm(path, nodes=(), ways=(), relations=()):
    """A small .osm (XML) file. nodes: (id, lon, lat, tags). ways: (id, [node ids], tags). relations: (id, [(way id, role)], tags)."""
    def tags(t):
        return ''.join('<tag k="%s" v="%s"/>' % (k, str(v).replace('&', '&amp;').replace('"', '&quot;')) for k, v in (t or {}).items())
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="lastbook-tests">']
    for i, lon, lat, t in sorted(nodes):
        out.append(f'<node id="{i}" version="1" lat="{lat}" lon="{lon}">{tags(t)}</node>')
    for i, refs, t in sorted(ways):
        out.append(f'<way id="{i}" version="1">' + ''.join(f'<nd ref="{r}"/>' for r in refs) + tags(t) + '</way>')
    for i, members, t in sorted(relations):
        out.append(f'<relation id="{i}" version="1">' + ''.join(f'<member type="way" ref="{r}" role="{role}"/>' for r, role in members) + tags(t) + '</relation>')
    out.append('</osm>')
    Path(path).write_text('\n'.join(out), encoding='utf-8')
    return path
