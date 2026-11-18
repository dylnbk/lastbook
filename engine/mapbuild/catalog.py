"""The regions a map can be made of: every extract Geofabrik publishes (554 of them: continents, countries, and the states
and provinces of the larger countries), with each one's outline.

  Catalog(index)            from Geofabrik's index-v1.json (fetched through sources.Sources, kept between builds)
  .get(id) -> Entry         id, name, parent, iso codes, the .osm.pbf address, outline, box
  .children(id)             the regions inside one
  .parts(id)                what to build it from: itself, or its children where its own file cannot be fetched
  slim(catalog)             the short list the app ships (content/regions/catalog.json) for choosing regions while offline

A region's folder name is its Geofabrik id with '/' written '_' (us/california -> us_california).
"""
import json
import re

from . import geom

# Where Geofabrik names no country for an extract. Used to choose postcode files.
ISO = {'great-britain': ['GB'], 'united-kingdom': ['GB'], 'britain-and-ireland': ['GB', 'IE'], 'ireland-and-northern-ireland': ['IE', 'GB'],
       'isle-of-man': ['IM'], 'guernsey-jersey': ['GG', 'JE'], 'england': ['GB'], 'scotland': ['GB'], 'wales': ['GB']}
# Extracts that are other extracts joined together: offered, but left out when "every country of a continent" is asked for.
COMPOSITE = {'alps', 'britain-and-ireland', 'dach', 'united-kingdom', 'azores', 'us', 'us-midwest', 'us-northeast', 'us-pacific', 'us-south', 'us-west',
             'south-africa-and-lesotho', 'israel-and-palestine', 'haiti-and-domrep', 'malaysia-singapore-brunei', 'gcc-states', 'senegal-and-gambia'}
# Extracts whose own file is served from a mirror this app does not trust the certificate of: built from their parts instead.
FROM_PARTS = {'germany'}
SAFE = re.compile(r'[a-z0-9][a-z0-9_-]{0,80}')


def folder_id(rid):
    out = rid.replace('/', '_')
    if not SAFE.fullmatch(out):
        raise ValueError(f'not a region name: {rid!r}')
    return out


class Entry:
    def __init__(self, feature):
        p = feature['properties']
        self.id = p['id']
        self.folder = folder_id(self.id)
        self.parent = p.get('parent')
        name = p.get('name') or self.id
        self.name = name.split('/')[-1].replace('-', ' ').title() if '/' in name else name
        self.iso = list(p.get('iso3166-1:alpha2') or [])
        self.iso2 = list(p.get('iso3166-2') or [])
        self.pbf = (p.get('urls') or {}).get('pbf')
        self.outline = geom.multipolygon(feature.get('geometry'))
        self.box = geom.box_of(self.outline)


class Catalog:
    def __init__(self, index):
        """index: the parsed index-v1.json, or a path to it."""
        if not isinstance(index, dict):
            with open(index, encoding='utf-8') as f:
                index = json.load(f)
        self.entries = {}
        for feature in index.get('features', []):
            try:
                e = Entry(feature)
            except (KeyError, ValueError):
                continue
            if e.pbf and e.box:
                self.entries[e.id] = e
        self.by_folder = {e.folder: e for e in self.entries.values()}

    def get(self, rid):
        e = self.entries.get(rid) or self.by_folder.get(rid)
        if e is None:
            raise KeyError(f'no region called {rid!r}')
        return e

    def children(self, rid):
        return sorted((e for e in self.entries.values() if e.parent == rid), key=lambda e: e.name)

    def countries_of(self, rid):
        """The regions directly inside a continent, without the ones that are others joined together."""
        return [e for e in self.children(rid) if e.id not in COMPOSITE]

    def parts(self, rid):
        e = self.get(rid)
        if e.id in FROM_PARTS:
            kids = self.children(e.id)
            if kids:
                return kids
        return [e]

    def iso_of(self, rid):
        """Country codes for a region: its own, or the nearest region above it that has one."""
        e = self.get(rid)
        seen = set()
        while e is not None and e.id not in seen:
            seen.add(e.id)
            if e.id in ISO:
                return ISO[e.id]
            if e.iso:
                return e.iso
            e = self.entries.get(e.parent)
        return []


def slim(catalog, sizes=None, as_of=None):
    """What the app carries for choosing regions with no connection: no outlines, only a box each.
    sizes: {region id: megabytes of its extract}, so a choice can be given a size before anything is downloaded."""
    out = []
    for e in sorted(catalog.entries.values(), key=lambda e: (e.parent or '', e.name)):
        d = {'id': e.id, 'name': e.name, 'parent': e.parent, 'box': [round(v, 3) for v in e.box],
             'iso': catalog.iso_of(e.id), 'composite': e.id in COMPOSITE, 'parts': e.id in FROM_PARTS}
        if sizes and sizes.get(e.id):
            d['mb'] = sizes[e.id]
        out.append(d)
    # A continent, and a country built from its parts, is as large as what is inside it. (The server sends its largest
    # files from another address, which Lastbook does not follow, so their own sizes are not asked.)
    for d in out:
        if 'mb' not in d:
            inside = [x['mb'] for x in out if x['parent'] == d['id'] and not x['composite'] and x.get('mb')]
            if inside:
                d['mb'], d['summed'] = round(sum(inside), 1), True
    made = {'source': 'Geofabrik download server index (download.geofabrik.de/index-v1.json)', 'regions': out}
    if as_of:
        made['sizes_as_of'] = as_of
    return made


# What a built region comes to, from the ones built for docs/WORLD-MAPS.md (4 at detail, 30 at overview, November 2026):
# map and places together as a share of the extract, and elevation per square degree of the region's box.
BUILT = {'detail': {'data': 2.75, 'terrain_mb': 3.3, 'dem_mb': 6.0, 'minutes_per_gb': 28},
         'overview': {'data': 0.17, 'terrain_mb': 0.4, 'dem_mb': 1.5, 'minutes_per_gb': 6}}
LAND_MB = 930           # the world's coastline, which every build needs once, whatever the regions: the download...
LAND_UNPACKED_MB = 1400     # ...and the room it takes unpacked and indexed, while maps are being built


def estimate(region, level='detail', terrain=True):
    """{'bytes', 'download_bytes', 'minutes'} for one region of the slim catalog, or None when its extract's size is not known.
    An estimate, and a loose one: a region of open farmland and one of dense streets differ by three times either way."""
    mb = region.get('mb')
    if not mb:
        return None
    k = BUILT['overview' if level == 'overview' else 'detail']
    w, s, e, n = region['box']
    area = max(0.0, (e - w) * (n - s))
    built = mb * k['data'] + (area * k['terrain_mb'] if terrain else 0)
    fetched = mb + (area * k['dem_mb'] if terrain else 0)
    return {'bytes': int(built * 1e6), 'download_bytes': int(fetched * 1e6), 'minutes': max(1, round(mb / 1000 * k['minutes_per_gb']))}
