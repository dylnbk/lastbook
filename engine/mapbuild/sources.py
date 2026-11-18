"""Where the open data a map is made from comes from, and a folder it is kept in between builds.

Every address here is on a host engine/setup/download.py allows. A file already fetched is not fetched again: the note
beside it (<file>.fetched.json) says what arrived and from where, and region.json repeats it.

  Sources(folder).get(url, name)   -> Path, fetching it if it is not there (resumable)
"""
import json
import os
import time
from pathlib import Path

from engine.setup import download

GEOFABRIK_INDEX = 'https://download.geofabrik.de/index-v1.json'
LAND_SPLIT = 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip'               # full detail, cut into pieces
LAND_SIMPLE = 'https://osmdata.openstreetmap.de/download/simplified-land-polygons-complete-3857.zip'  # for zoomed-out views
DEM = {30: 'https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif',
       90: 'https://copernicus-dem-90m.s3.amazonaws.com/Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif'}
POSTCODES = 'https://download.geonames.org/export/zip/{file}'       # CC BY 4.0; GB_full.csv.zip for Great Britain's full postcodes

CREDITS = {
    'osm': 'Map data © OpenStreetMap contributors, Open Database Licence 1.0 (extracts by Geofabrik)',
    'coast': 'Land outline from OpenStreetMap coastlines (osmdata.openstreetmap.de), ODbL 1.0',
    'dem': 'Elevation: © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved',
    'postcodes': 'Postcodes: GeoNames (geonames.org), CC BY 4.0',
}


def dem_url(lat, lon, metres=30):
    """The 1 degree cell whose south-west corner is lat, lon."""
    return DEM[metres].format(ns='N' if lat >= 0 else 'S', lat=abs(lat), ew='E' if lon >= 0 else 'W', lon=abs(lon))


class Sources:
    def __init__(self, folder, control=None, say=None):
        self.folder = Path(folder)
        self.control = control
        self.say = say or (lambda *a: None)

    def note(self, path):
        try:
            return json.loads(Path(str(path) + '.fetched.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def have(self, name):
        path = self.folder / name
        note = self.note(path)
        try:
            return path if note and path.stat().st_size == note.get('size') else None
        except OSError:
            return None

    def get(self, url, name, missing_ok=False, max_age_days=None):
        """The file, fetched if need be. missing_ok: None when the publisher has no such file (a DEM cell of open sea)."""
        path = self.folder / name
        got = self.have(name)
        if got is not None:
            if max_age_days is None or time.time() - os.path.getmtime(got) < max_age_days * 86400:
                return got
        gone = Path(str(path) + '.absent')
        if missing_ok and gone.exists():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        last = [0.0]

        def progress(done, total, phase):
            if time.time() - last[0] > 30:
                last[0] = time.time()
                self.say(f'  {name}: {done / 1e6:,.0f} of {(total or 0) / 1e6:,.0f} MB')
        try:
            download.Fetch(url, path, control=self.control, progress=progress).run()
        except download.Failed as e:
            if missing_ok and e.extra.get('status') in (403, 404):     # S3 answers 404 (or 403) for a key that is not there
                gone.write_text('')
                return None
            if got is not None:         # a refresh that failed: the copy already here still serves
                return got
            raise
        return path

    def source_note(self, name):
        """What region.json keeps about a source: its address, size, checksum and when it was fetched."""
        note = self.note(self.folder / name) or {}
        return {k: note.get(k) for k in ('url', 'size', 'sha256', 'modified') if note.get(k) is not None}


def default_folder():
    """Where sources are kept when nobody says: beside the library, on its drive, never in the home folder."""
    from engine import paths
    return Path(os.environ.get('LASTBOOK_MAP_SOURCES') or paths.DATA.parent / '.lastbook' / 'map-sources')
