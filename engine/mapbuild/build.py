"""One region from nothing: fetch what it needs, build it in a folder of its own beside the others, put it in place whole.

  Builder(data, sources_folder).build('isle-of-man', level='detail')   -> the region.json it wrote

A region being built lives in regions/.building-<id>/ (a dot-folder: the app does not see it). Only when every part is
finished is it renamed to regions/<id>/, so the app never opens half a map, and a build that stops leaves the map that
was there before untouched. Building a region again replaces it.
"""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from engine import sqlitefs
from . import catalog as catalogs
from . import geom, sources
from . import schema as S

# What this code makes of an extract. It goes up when a region built before would come out different now (new kinds of thing
# on the map, names written another way), so "leave alone what is already built" does not leave an older making in place.
#   2: summits, ferry routes, borders; a place's English name beside its own; help points mapped as closed left out;
#      an overview's shapes drawn more simply and only its great woods kept.
BUILDER = 2
FULL_POSTCODES = {'GB': 'GB_full.csv.zip', 'NL': 'NL_full.csv.zip', 'CA': 'CA_full.csv.zip'}     # GeoNames' fuller files, where it has them
BIG_PBF = 300 << 20         # above this, where nodes are is kept in a file rather than in memory


def regions_root(data):
    return Path(data) / 'derived' / 'geo' / 'regions'


def scratch():
    """Where the one memory-mapped file of a build goes: the record of where every node is (16 bytes a node: about
    twice the size of the .osm.pbf, gone when the region is read).

    Not beside the library. Measured on an exFAT drive under macOS, with the map being written to the same drive: the
    same extract read twice gave 6,452,334 features with this file on the computer's own disk and 6,444,709 with it on
    the drive. Pages written through a memory map were lost, and nothing reported an error. Everything else a build
    writes goes through ordinary writes, which that drive keeps; this file is the exception, so it lives in the
    system's temporary folder (LASTBOOK_SCRATCH to choose another, on a disk formatted for the computer)."""
    return Path(os.environ.get('LASTBOOK_SCRATCH') or tempfile.gettempdir())


class Builder:
    def __init__(self, data, sources_folder=None, say=None, control=None, workers=2):
        self.root = regions_root(data)
        self.say = say or (lambda *a: None)
        self.control = control
        self.workers = workers
        self.src = sources.Sources(sources_folder or sources.default_folder(), control=control, say=self.say)
        self._catalog = self._land = None

    @property
    def catalog(self):
        if self._catalog is None:
            index = self.src.get(sources.GEOFABRIK_INDEX, 'geofabrik/index-v1.json', max_age_days=30)
            self._catalog = catalogs.Catalog(index)
        return self._catalog

    @property
    def land(self):
        if self._land is None:
            from . import coast
            path = self.src.get(sources.LAND_SPLIT, 'osmdata/land-polygons-split-4326.zip', max_age_days=120)
            self._land = coast.Land(path, say=self.say)
        return self._land

    def built(self, rid):
        """The region.json of a region already in place, or None."""
        try:
            return json.loads((self.root / catalogs.folder_id(rid) / 'region.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def current(self, rid, level):
        """Is the region here, at this level, made by this code as it is now?"""
        was = self.built(rid)
        return bool(was) and was.get('level') == level and was.get('builder') == BUILDER

    def remove(self, rid):
        folder = self.root / catalogs.folder_id(rid)
        if folder.is_dir():
            gone = self.root / f'.old-{folder.name}-{int(time.time())}'
            folder.rename(gone)
            shutil.rmtree(gone, ignore_errors=True)
            return True
        return False

    def build(self, rid, level='detail', terrain=True, buildings=True, postcodes=True, coastline=True):
        from . import coast, osm, places
        from . import terrain as terrains
        if level not in ('detail', 'overview'):
            raise ValueError(level)
        say, t0 = self.say, time.time()
        entry = self.catalog.get(rid)
        work = self.root / f'.building-{entry.folder}'
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        say(f'{entry.name}: {level}')
        parts = self.catalog.parts(entry.id)
        pbfs = []
        for p in parts:
            pbfs.append(self.src.get(p.pbf, f'geofabrik/{p.folder}.osm.pbf'))
        heavy = max(os.path.getsize(p) for p in pbfs) > BIG_PBF
        nodes = scratch() / f'lastbook-nodes-{os.getpid()}-{entry.folder}'
        if heavy:
            need = 3 * max(os.path.getsize(p) for p in pbfs)
            if shutil.disk_usage(nodes.parent).free < need:
                raise OSError(f'{nodes.parent} needs {need / 1e9:.1f} GB free while {entry.name} is read (set LASTBOOK_SCRATCH to a folder with room)')
        try:
            counts = osm.build(pbfs if len(pbfs) > 1 else pbfs[0], work / 'map.sqlite', level=level, buildings=buildings, name=entry.name,
                               node_index=str(nodes) if heavy else None, control=self.control,
                               progress=lambda what, n: say(f'  {n:,} {what}') if what == 'features' and n % 400000 < 40000 else None)
        finally:
            for left in nodes.parent.glob(nodes.name + '.*'):
                left.unlink(missing_ok=True)
        say(f'  map: {counts["features"]:,} features, {counts["pois"]:,} help points in {counts["seconds"]:.0f}s')
        info = {'id': entry.folder, 'geofabrik': entry.id, 'name': entry.name, 'level': level, 'parent': entry.parent,
                'box': [round(v, 5) for v in entry.box], 'iso': self.catalog.iso_of(entry.id), 'schema': 1, 'builder': BUILDER,
                'data_as_of': counts['data_as_of'], 'counts': dict(counts), 'credits': [sources.CREDITS['osm']],
                'sources': {'osm': [self.src.source_note(f'geofabrik/{p.folder}.osm.pbf') for p in parts]},
                'outline': [[[[round(x, 4), round(y, 4)] for x, y in geom.simplify_ring(ring, 0.01)] for ring in poly] for poly in entry.outline]}
        if coastline:
            db = sqlitefs.connect(work / 'map.sqlite', synchronous='OFF')
            made = coast.add(db, self.land, entry.outline, level)
            db.close()
            info['counts']['coast'] = made
            info['credits'].append(sources.CREDITS['coast'])
            info['sources']['coast'] = self.src.source_note('osmdata/land-polygons-split-4326.zip')
            say(f'  coast: {made["island"]:,} pieces of land')
        zips = []
        if postcodes:
            for cc in info['iso']:
                name = FULL_POSTCODES.get(cc, f'{cc}.zip')
                got = self.src.get(sources.POSTCODES.format(file=name), f'geonames/{name}', missing_ok=True, max_age_days=120)
                if got is None and name != f'{cc}.zip':
                    got = self.src.get(sources.POSTCODES.format(file=f'{cc}.zip'), f'geonames/{cc}.zip', missing_ok=True, max_age_days=120)
                if got is not None:
                    zips.append(got)
        found = places.build(work / 'map.sqlite', work / 'places.sqlite', entry.name, level=level, postcodes=zips, outline=entry.outline, control=self.control)
        info['counts']['finder'] = found
        if found['postcodes']:
            info['credits'].append(sources.CREDITS['postcodes'])
            info['sources']['postcodes'] = [self.src.source_note(f'geonames/{Path(z).name}') for z in zips]
        say(f'  place finder: {found["places"]:,} places, {found["streets"]:,} streets, {found["postcodes"]:,} postcodes')
        if terrain:
            made = terrains.build(work / 'terrain.sqlite', entry.outline, self.src, level=level, workers=self.workers, say=say, control=self.control)
            if made['tiles']:
                info['counts']['terrain'] = made
                info['credits'].append(sources.CREDITS['dem'])
                info['sources']['dem'] = f'Copernicus DEM GLO-{made["metres"]} (AWS Open Data)'
                say(f'  elevation: {made["tiles"]:,} tiles to zoom {made["maxz"]}')
            else:
                (work / 'terrain.sqlite').unlink(missing_ok=True)
        info['bytes'] = {p.name: p.stat().st_size for p in sorted(work.iterdir()) if p.suffix == '.sqlite'}
        info['built'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        info['seconds'] = round(time.time() - t0)
        (work / 'region.json').write_text(json.dumps(info, separators=(',', ':')), encoding='utf-8')
        final = self.root / entry.folder
        if final.exists():
            gone = self.root / f'.old-{entry.folder}-{int(time.time())}'
            final.rename(gone)
            work.rename(final)
            shutil.rmtree(gone, ignore_errors=True)
        else:
            work.rename(final)
        say(f'  done in {info["seconds"]}s: {sum(info["bytes"].values()) / 1e6:,.1f} MB')
        return info

    def close(self):
        if self._land is not None:
            self._land.close()
            self._land = None
