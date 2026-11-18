"""Making a map of anywhere, on the person's own computer, from data anyone may copy.

A region is one folder, complete in itself (engine/regions.py reads it; any number sit side by side):

  derived/geo/regions/<id>/region.json      what it is: name, level, box, where each part came from and when
  derived/geo/regions/<id>/map.sqlite       streets, water, land use, buildings, coast, place names, help points
  derived/geo/regions/<id>/places.sqlite    the place finder: towns, streets, postcodes
  derived/geo/regions/<id>/terrain.sqlite   elevation, for relief, contours and the 3D view        (when asked for)

Two levels of detail:
  detail     everything: every street and path, buildings, land use, all help points. A country or a state.
  overview   what a whole continent can carry: main roads, towns, rivers and lakes, built-up areas, and every
             help point. Drawn under and around the detailed regions, never over them.

  schema.py   the tables, the packed geometry, and which OpenStreetMap tags become which map classes
  osm.py      an .osm.pbf extract (Geofabrik) -> map.sqlite
  coast.py    land polygons made from OpenStreetMap's coastline (osmdata) -> the sea drawn where it really is
  places.py   map.sqlite's own names, plus GeoNames postcodes -> places.sqlite
  terrain.py  Copernicus DEM -> terrain.sqlite
  catalog.py  the regions Geofabrik publishes, as shipped in content/regions/catalog.json
  build.py    one region from nothing: fetch what it needs, build it beside the others, put it in place whole

The sources and their licences: OpenStreetMap (ODbL, (c) OpenStreetMap contributors) through Geofabrik and osmdata;
Copernicus DEM (free and open, with its fixed notice); GeoNames postcodes (CC BY 4.0). Each region.json names what
went into it, and the map credits them.

Needs the osmium package (pyosmium) to read .osm.pbf; engine/regions.py, which only reads what is built, needs nothing.
Nothing here opens a connection: downloads go through engine/setup/download.py.
"""
