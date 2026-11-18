#!/usr/bin/env python3
"""Make the map of anywhere in the world, from open data, into a library's derived/geo/regions/<id>/.

  python tools/build_world_region.py isle-of-man                        one region in full detail
  python tools/build_world_region.py great-britain ireland-and-northern-ireland
  python tools/build_world_region.py --overview france spain            main roads, towns, water and help points only
  python tools/build_world_region.py --overview --countries-of europe --skip-built --but great-britain,isle-of-man
  python tools/build_world_region.py --list europe                      what can be asked for (554 regions; no argument: continents)
  python tools/build_world_region.py --remove france
  python tools/build_world_region.py --write-catalog                    refresh content/regions/catalog.json from Geofabrik

  --skip-built       leave alone a region already built at this level
  --only-fetched     build only regions whose extract is already in the sources folder (nothing large is downloaded)
  --data FOLDER      the LastbookData folder to build into (default: the one this copy uses)
  --sources FOLDER   where downloads are kept between builds (default: .lastbook/map-sources beside the library)
  --rate N           download no faster than N megabytes a second
  --workers N        processes for elevation tiles (default 2)
  --no-terrain / --no-buildings / --no-postcodes
  --background       macOS: run at background priority (slower; the computer stays fully free for other work)

Regions are Geofabrik's (download.geofabrik.de); a name is the last part of its address there, e.g. us/california.
Needs: osmium (pyosmium) and numpy. Sources: OpenStreetMap via Geofabrik and osmdata, Copernicus DEM, GeoNames.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('regions', nargs='*')
    ap.add_argument('--overview', action='store_true')
    ap.add_argument('--countries-of')
    ap.add_argument('--but', default='')
    ap.add_argument('--skip-built', action='store_true')
    ap.add_argument('--only-fetched', action='store_true')
    ap.add_argument('--list', nargs='?', const='', default=None)
    ap.add_argument('--remove', action='store_true')
    ap.add_argument('--write-catalog', action='store_true')
    ap.add_argument('--data')
    ap.add_argument('--sources')
    ap.add_argument('--rate', type=float)
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--no-terrain', action='store_true')
    ap.add_argument('--no-buildings', action='store_true')
    ap.add_argument('--no-postcodes', action='store_true')
    ap.add_argument('--background', action='store_true')
    a = ap.parse_args()
    if a.data:
        os.environ['LASTBOOK_DATA'] = str(Path(a.data).resolve())
    from engine import paths
    from engine.setup import download
    from engine.mapbuild import build, catalog

    try:
        os.nice(10)         # whatever else the computer is doing comes first
    except OSError:
        pass
    if a.background and sys.platform == 'darwin':
        subprocess.run(['taskpolicy', '-b', '-p', str(os.getpid())], check=False)
    if a.rate:
        download.THROTTLE.set(a.rate * 1e6)

    def say(*words):
        print(time.strftime('%H:%M:%S'), *words, flush=True)
    b = build.Builder(paths.DATA, a.sources, say=say, workers=a.workers)
    cat = b.catalog
    if a.write_catalog:
        out = paths.APP / 'content' / 'regions' / 'catalog.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(catalog.slim(cat), separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
        say(f'{len(cat.entries)} regions written to {out}')
        return 0
    if a.list is not None:
        for e in (cat.children(a.list) if a.list else [e for e in cat.entries.values() if not e.parent]):
            kids = len(cat.children(e.id))
            done = b.built(e.id)
            print(f'{e.id:38} {e.name:38} {"(" + str(kids) + " inside)" if kids else "":14}{"built: " + done["level"] if done else ""}')
        return 0
    wanted = list(a.regions)
    if a.countries_of:
        wanted += [e.id for e in cat.countries_of(a.countries_of)]
    but = {x for x in a.but.split(',') if x}
    wanted = [w for w in dict.fromkeys(wanted) if w not in but]
    if not wanted:
        ap.error('name a region (see --list)')
    for w in wanted:
        cat.get(w)      # a wrong name stops everything before anything is fetched
    if a.remove:
        for w in wanted:
            say(w, 'removed' if b.remove(w) else 'was not there')
        return 0
    level = 'overview' if a.overview else 'detail'
    failed = []
    for i, w in enumerate(wanted, 1):
        was = b.built(w)
        if a.skip_built and was and was.get('level') == level:
            say(f'[{i}/{len(wanted)}] {w}: already built')
            continue
        if a.only_fetched and not all(b.src.have(f'geofabrik/{p.folder}.osm.pbf') for p in cat.parts(w)):
            say(f'[{i}/{len(wanted)}] {w}: its extract is not here yet')
            continue
        say(f'[{i}/{len(wanted)}]')
        try:
            b.build(w, level=level, terrain=not a.no_terrain, buildings=not a.no_buildings, postcodes=not a.no_postcodes)
        except (download.Failed, download.Stopped) as e:
            say(f'  {w}: could not be fetched: {getattr(e, "message", e)}')
            failed.append(w)
        except Exception as e:      # one region failing does not stop the rest of a continent
            import traceback
            traceback.print_exc()
            say(f'  {w}: FAILED: {type(e).__name__}: {e}')
            failed.append(w)
    b.close()
    if failed:
        say('not built:', ', '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
