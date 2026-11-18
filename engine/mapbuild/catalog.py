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


def slim(catalog):
    """What the app carries for choosing regions with no connection: no outlines, only a box each."""
    out = []
    for e in sorted(catalog.entries.values(), key=lambda e: (e.parent or '', e.name)):
        out.append({'id': e.id, 'name': e.name, 'parent': e.parent, 'box': [round(v, 3) for v in e.box],
                    'iso': catalog.iso_of(e.id), 'composite': e.id in COMPOSITE, 'parts': e.id in FROM_PARTS})
    return {'source': 'Geofabrik download server index (download.geofabrik.de/index-v1.json)', 'regions': out}
