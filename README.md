# mapillary_urban_vegetation

Webmaps of urban vegetation using Mapillary data.

For every Mapillary image of a city that has a full semantic segmentation,
the pipeline stores its location, height and **vegetation percent** (share
of the image classified as `nature--vegetation` by Mapillary's own
segmentation). Only image metadata is downloaded, never the pictures.

Test city: **Curitiba, Parana, Brazil**.

## Folders

| Folder | Content |
|:--|:--|
| `scripts/` | `fetch_city_vegetation.py`, the resumable data fetcher |
| `data/` | one folder per city: `data/<city_slug>/` |
| `maps/` | the webmaps (to come) |
| `my_mappilary_api/` | git submodule with the Mapillary helpers |

Clone with the submodule: `git clone --recurse-submodules <url>`
(or `git submodule update --init` after a normal clone).

## How it works

1. The city polygon is retrieved from OpenStreetMap (Nominatim) and saved to
   `data/<city_slug>/boundary.geojson` (reused afterwards). Only results
   named like the first part of the place (e.g. "Curitiba" for "Curitiba,
   Parana, Brazil", ignoring case and accents) are considered, preferring
   administrative areas and settlements, then the most important one. Its
   polygon comes from Nominatim (lookup) or polygons.openstreetmap.fr. When
   OpenStreetMap only has a point for the city (as for Curitiba), the
   administrative boundary with the same name is taken from the point's
   address hierarchy (Nominatim details), or from the boundaries containing
   it (Overpass API, several public instances). The polygon must contain the city's own location. The candidates are printed in the log; use
   `--osm-relation <id>` to choose the boundary explicitly.
2. The polygon is covered with **big tiles** (zoom 14, ~2.4 km) and each big
   tile with **small tiles** (zoom 18, ~150 m).
3. For each small tile, all image metadata is requested from Mapillary. While
   the API refuses a tile or returns as many images as the request limit
   (2000, so possibly truncated), the tile is recursively split into its 4
   children, so that no image is lost.
   Requests are retried with [tenacity](https://tenacity.readthedocs.io):
   exponential backoff with jitter on network errors and HTTP 5xx, and a
   longer wait on rate limiting (HTTP 429), during which all parallel
   requests pause. If Mapillary rejects the token, the run checkpoints and
   stops.
4. For each image inside the city, its detections (semantic segmentation) are
   requested, and the vegetation percent is computed straight from the encoded
   polygons. Images without a full-scene segmentation (e.g. old images that
   only kept a few sign detections) are dropped.
5. Big tiles are processed from the city center outwards (unfinished ones
   first). When all small tiles of a big tile are done, it is written to
   `data/<city_slug>/tiles/<z>_<x>_<y>.parquet`.

### Output schema (GeoParquet, EPSG:4326)

| Column | Description |
|:--|:--|
| `id` | Mapillary image ID |
| `captured_at` | capture time (UTC) |
| `lon`, `lat` | image location (Mapillary's computed location when available) |
| `h` | altitude in meters (computed altitude when available, may be empty) |
| `vegetation_percent` | % of the image covered by `nature--vegetation` |
| `number_available_classes` | number of segmentation classes in the image |
| `geometry` | point (lon, lat) |

### Progress and resuming

`data/<city_slug>/progress.json` records every big tile as `not_started`,
`in_progress` or `completed`, plus counts and a summary. When a run stops
(time budget, Ctrl+C, SIGTERM) the finished small tiles of the current big
tile are checkpointed in `data/<city_slug>/partial/<key>.parquet` and listed
in the progress file; the next run continues from there. A small tile whose
requests fail is simply retried on the next run.

## Usage

```bash
pip install -r requirements.txt
export API_TOKEN="MLY|..."   # Mapillary client token
python scripts/fetch_city_vegetation.py "Curitiba, Parana, Brazil" --max-minutes 60
```

Options: `--big-zoom` (14), `--small-zoom` (18), `--limit` (2000 images per
request), `--max-minutes`, `--max-big-tiles`, `--workers` (8 parallel
detection requests), `--data-dir` (`data`), `--slug`, `--osm-relation` (use
this OSM relation as the boundary), `--boundary-only` (resolve and save the
boundary, print its area and number of tiles, then exit; no token needed).
See `--help`.

To start a city over (e.g. after a wrong boundary), delete `data/<city_slug>/`.

## GitHub Action

`.github/workflows/fetch-vegetation.yml` runs the fetcher every 6 hours (and
on demand, with the place, zooms, an optional OSM relation and an optional
maximum number of big tiles as inputs) with a 320-minute budget, then commits
`data/` (except for cancelled runs). It needs the Mapillary token as the `API_TOKEN` repository
secret (Settings → Secrets and variables → Actions).

## Tests

```bash
pip install pytest
pytest tests
```

The tests run offline against fake Nominatim and Mapillary APIs.
