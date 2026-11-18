# Maps of anywhere: regions built from open data

The map that comes with a pack is of the United States, and [MAPS.md](MAPS.md) describes how it is put together from
US federal data. This page describes the other way a library can get a map: **regions**, each built on the person's
own computer from data anyone may copy, for anywhere in the world, and kept side by side.

A library can hold both. The app draws whatever is there.

## What a region is

One folder under `derived/geo/regions/`, complete in itself:

| File | Holds |
|---|---|
| `region.json` | Name, level, box and outline, the date of the data, what each part was made from, credits |
| `map.sqlite` | Streets, paths, railways, water, land use, buildings, coast, place names, help points |
| `places.sqlite` | The place finder: towns, villages, streets, postcodes, named help points |
| `terrain.sqlite` | Elevation tiles for shaded relief, contour lines, spot heights and the 3D view (optional) |

Adding a region is putting its folder there. Removing one is deleting its folder. The app looks every few seconds and
needs no restart. A folder whose name starts with a dot is one being built or removed, and is not seen: a region is
built in `.building-<id>/` and renamed into place only when every part is finished, so the app never opens half a map
and a build that stops leaves the map that was there before.

There are two levels:

- **detail**: every street and path, buildings, land use, railways, streams, and every help point. For a country or a
  state. Elevation to about 30 m (zoom 12): relief, contours and 3D.
- **overview**: what a whole continent can carry. Main roads (motorway to tertiary), railways, rivers and lakes,
  built-up areas, cities to villages, and still **every help point that has a layer** (hospitals, pharmacies, fire
  and police stations, water, fuel, food shops, shelters, defibrillators). Elevation to about 90 m (zoom 9): relief
  only.

Where a detailed region and an overview cover the same ground, the detailed one is drawn, and the overview's help
points there are left out so nothing is counted twice.

## Building one

```sh
python tools/build_world_region.py isle-of-man                       # in full detail
python tools/build_world_region.py great-britain ireland-and-northern-ireland
python tools/build_world_region.py --overview france spain
python tools/build_world_region.py --overview --countries-of europe --skip-built --but great-britain,isle-of-man
python tools/build_world_region.py --list europe                     # what can be asked for
python tools/build_world_region.py --remove france
```

`--data` chooses the library (default: the one this copy uses), `--sources` where downloads are kept between builds,
`--rate` caps the download speed, `--background` runs at background priority on a Mac.

It needs two things the app itself does not carry: `osmium` (pyosmium) to read OpenStreetMap extracts, and `numpy`.

A region's name is Geofabrik's: the 554 extracts it publishes, from continents down to the states and provinces of
the larger countries (`us/california`, `germany/bayern` is `bayern`). `content/regions/catalog.json` is the short
list the app ships so regions can be chosen with no connection; `--write-catalog` refreshes it.

Measured on an Apple M4, reading from and writing to an external SSD:

| Region | Level | Extract | Time | Size |
|---|---|---|---|---|
| Isle of Man | detail | 6 MB | 14 s | 19 MB |
| Guernsey and Jersey | detail | 4 MB | 75 s (most of it fetching elevation) | 13 MB |

## Where the data comes from

Every address is on a host `engine/setup/download.py` allows, and every one verifies against the pinned roots in
`engine/setup/roots.pem`. `region.json` records the address, size, SHA-256 and date of each file that went in.

| Part | Source | Licence |
|---|---|---|
| Streets, places, land, water, buildings, help points | OpenStreetMap, as `.osm.pbf` extracts from `download.geofabrik.de` | ODbL 1.0, © OpenStreetMap contributors |
| The coast | Land polygons made from OpenStreetMap's coastline, `osmdata.openstreetmap.de` | ODbL 1.0 |
| Elevation | Copernicus DEM GLO-30 and GLO-90 on AWS Open Data | Free and open, with its fixed notice (in `region.json` and the map's panel) |
| Postcodes | GeoNames postal code files, `download.geonames.org` | CC BY 4.0 |
| Region names and outlines | Geofabrik's `index-v1.json` | — |

Geofabrik publishes ready-made layers (`.gpkg`, `.shp`) only for its smaller extracts; Great Britain has none. So
regions are read from `.osm.pbf`, which every extract has, and the tags are turned into the same classes the US map
uses (`engine/mapbuild/schema.py`). The page that draws the map needed no new styles.

Germany's country extract is served from a mirror whose certificate is outside the pinned roots. It is refused, and
Germany is built from its sixteen state extracts instead, which Geofabrik serves itself.

## How the sea is drawn

The page paints sea, then a coarse outline of the world's land, then each tile's `ocean` polygons, then its `island`
polygons. A region writes its own outline as `ocean` (wiping the coarse land) and the real land from OpenStreetMap's
coastline as `island`. The land is stored four times at falling detail, each for a band of zoom levels
(`feat_idx.maxz` ends a band), so a view of a whole country does not draw every rock of its coast.

Outside every region the coarse outline is all there is. The note under the map says so.

## What is different from the US map, and what is missing

- **No house numbers.** The US address search uses an index of every address. No open source gives that for the UK,
  and OpenStreetMap's house numbers are too patchy to promise. So the finder holds every named **street** once per
  neighbourhood, and postcodes. An address typed in full gives the street and says the house number was not found.
  The number is read before the street (`10 Downing Street`) or after it (`Hauptstraße 12`).
- **Postcodes** are GeoNames', which vary by country: Great Britain's are the full postcodes (1.7 million), most
  countries' are complete, and Ireland's are only the 139 routing keys (Eircodes are not open data).
- **Elevation is a surface model.** Copernicus DEM measures the top of what is there: in forest and towns that is
  treetops and roofs, not ground. Summits come out a few metres low because each cell is an average (Snaefell: 616 m
  against a surveyed 621 m; Scafell Pike: 971 m against 978 m).
- **No aerial photos**, no lidar, no crop or water-body inventories: those layers of the US map come from US agencies.
- **Russia** is not among Geofabrik's European extracts; it is a region of its own.
- Measures are metric where a library has regions (kilometres, metres, contours every 10 to 50 m). "Distances in
  miles" in the Layers panel switches back.
- Four kinds of help point were added because they matter outside the US and in an emergency: wells and water
  pumps, defibrillators, ambulance stations and emergency assembly points.

## One thing learned the hard way

A build keeps a record of where every OpenStreetMap node is (16 bytes each) in a memory-mapped file. With that file
on an exFAT drive under macOS, while the map was being written to the same drive, pages written through the memory
map were lost and nothing reported an error: the same extract read twice gave 6,452,334 features with the file on
the computer's own disk and 6,444,709 with it on the drive. So that one file goes in the system's temporary folder
(`LASTBOOK_SCRATCH` to choose another), never beside the library. It is about twice the size of the extract and is
deleted as soon as the extract is read. Everything else a build writes uses ordinary writes, which the drive keeps.

## In the code

| Where | What |
|---|---|
| `engine/regions.py` | Reading: which regions are here, their tiles, help points, place finders, elevation |
| `engine/mapbuild/` | Building: `osm.py`, `coast.py`, `places.py`, `terrain.py`, `catalog.py`, `sources.py`, `build.py` |
| `tools/build_world_region.py` | The command above |
| `server.py` | `/api/map/tile`, `/pins`, `/members`, `/near`, `/terrain`, `/api/places` read regions too; `/api/map/info` lists them; `/api/map/where` says which map a point is on |
| `web/map.js` | The "Maps here" panel, metric measures, wording, the four new layers |
| `app/tests/test_regions.py` | Reading, and every part of building that needs no OpenStreetMap reader |
| `app/tests/test_mapbuild.py` | Reading an extract and building a whole region (needs `osmium`; skipped without it) |
