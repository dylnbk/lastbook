#!/usr/bin/env python3
"""Makes the zoomed-out tiles of a library's regions ahead of time (zoom 5 to 9), outside the app.

  python tools/warm_region_tiles.py [--data FOLDER] [--workers 3] [--max-zoom 9] [region ...]

A tile that takes in half a country is the slowest thing the map does: every main road in it is read and drawn small.
Measured on Great Britain: 2.6 s at zoom 5, 7.6 s at zoom 8. The app keeps each one once it has made it
(derived/geo/regions/.tiles.sqlite), so only the first look is slow; this takes that first look for the person.
What is kept is for the regions as they are now: after a region is added, rebuilt or removed, run it again.

Uses the app's own code for making a tile (server.map_tile), so it needs the app's Python, not the map builder's.
"""
import argparse
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
CELL = 0.25


def tiles_of(outline, zooms):
    """Every tile at these zooms that an outline touches."""
    from engine.mapbuild import geom
    out = set()
    for c, r in geom.cells(outline, CELL):
        west, south = c * CELL - 180, r * CELL - 90
        east, north = west + CELL, min(85.0, south + CELL)
        south = max(-85.0, south)
        for z in zooms:
            n = 2 ** z
            y = lambda lat: int((0.5 - math.log((1 + math.sin(math.radians(lat))) / (1 - math.sin(math.radians(lat)))) / (4 * math.pi)) * n)
            for x in range(int((west + 180) / 360 * n), int((east + 180 - 1e-9) / 360 * n) + 1):
                for yy in range(y(north), y(south) + 1):
                    if 0 <= x < n and 0 <= yy < n:
                        out.add((z, x, yy))
    return out


def one(t):
    import server       # in each worker, once
    t0 = time.time()
    data = server.map_tile(*t)
    return t, len(data), time.time() - t0


def quiet():
    try:
        os.nice(10)
    except OSError:
        pass
    sys.modules.setdefault('onnxruntime', None)     # the tiles need no AI


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('regions', nargs='*', help='only these (folder names); default: every region in the library')
    ap.add_argument('--data')
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--min-zoom', type=int, default=5)
    ap.add_argument('--max-zoom', type=int, default=9)
    a = ap.parse_args()
    if a.data:
        os.environ['LASTBOOK_DATA'] = str(Path(a.data).resolve())
    os.environ.setdefault('SURVIVAL_DISABLE_INDEX_WATCH', '1')
    quiet()
    from engine import paths, regions
    atlas = regions.Atlas(os.environ.get('LASTBOOK_REGIONS') or paths.DATA / 'derived' / 'geo' / 'regions')
    wanted = [r for r in atlas.regions() if not a.regions or r.id in a.regions]
    if not wanted:
        print('no regions here')
        return 1
    zooms = range(max(3, a.min_zoom), min(9, a.max_zoom) + 1)       # the app keeps tiles up to zoom 9; beyond that they are quick
    todo = set()
    for r in wanted:
        box = [[[r.west, r.south], [r.east, r.south], [r.east, r.north], [r.west, r.north], [r.west, r.south]]]
        todo |= tiles_of(r.outline or [box], zooms)
    todo = sorted(todo)
    print(f'{len(wanted)} regions, {len(todo):,} tiles at zoom {zooms[0]} to {zooms[-1]}', flush=True)
    t0, slow, total = time.time(), 0.0, 0
    with ProcessPoolExecutor(max(1, a.workers), initializer=quiet) as pool:
        for i, (t, size, took) in enumerate(pool.map(one, todo, chunksize=4), 1):
            slow = max(slow, took)
            total += size
            if i % 250 == 0 or i == len(todo):
                print(f'{time.strftime("%H:%M:%S")} {i:,} of {len(todo):,}; slowest so far {slow:.1f} s; {total / 1e6:,.0f} MB of tiles', flush=True)
    print(f'done in {time.time() - t0:.0f} s', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
