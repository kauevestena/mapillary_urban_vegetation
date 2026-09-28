# Running the pipeline on your own machine

Everything the GitHub Action does can be run locally: fetching the data of a
city and building the webmaps. This guide assumes Linux or macOS (on Windows,
use WSL or adapt the shell commands).

## 1. Prerequisites

- **Python 3.10 or newer** and **git**.
- **A Mapillary client token**: sign in at
  <https://www.mapillary.com/dashboard/developers>, register an application
  (any name; only "Read" access is needed) and copy its **client token**
  (it starts with `MLY|`).

## 2. Get the code

The Mapillary helpers live in the `my_mappilary_api` submodule, so clone with
submodules:

```bash
git clone --recurse-submodules https://github.com/kauevestena/mapillary_urban_vegetation.git
cd mapillary_urban_vegetation
# already cloned without --recurse-submodules? then:
git submodule update --init
```

Create a virtual environment and install the dependencies:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Provide the token

Either as an environment variable (recommended):

```bash
export API_TOKEN="MLY|..."
```

or in a file named `mapillary_token` at the repository root, containing only
the token. That file is git-ignored; never commit your token.

## 4. Fetch a city

Check the boundary first; this needs no token and makes no Mapillary request:

```bash
python scripts/fetch_city_vegetation.py "Curitiba" --boundary-only
```

It prints the OpenStreetMap candidates, the chosen boundary, its area and the
number of big tiles, and saves it to `data/<city_slug>/boundary.geojson`
(`data/curitiba/` here: the slug comes from the place, so always use the same
place string for a city, or pass the same `--slug`). A longer place such as
"Curitiba, Parana, Brazil" helps when several places share the name. If
the wrong area was chosen, refine the place name or pass the OpenStreetMap
relation ID with `--osm-relation` (find it on <https://www.openstreetmap.org>),
then delete `data/<city_slug>/` and try again.

Fetch the whole city, one hour at a time:

```bash
python scripts/fetch_city_vegetation.py "Curitiba" --max-minutes 60
```

Run the same command again to continue; it resumes where it stopped (big tiles
are processed from the city center outwards). Stop at any time with Ctrl+C:
the current big tile is checkpointed and continued on the next run.

### Only one big tile

To (re)fetch just the big tile containing a point of the city, add `--point
LAT LON` and the folder name of the city with `--slug`:

```bash
python scripts/fetch_city_vegetation.py "Curitiba" \
  --slug curitiba --point -25.4284 -49.2733
```

The tile is fetched again even if it was already completed (its parquet file
is overwritten). A point outside the city boundary, or invalid coordinates,
stops the script with an error.

### Other options

| Option | Default | Meaning |
|:--|:--|:--|
| `--big-zoom` | 14 | zoom of the output tiles (~2.4 km) |
| `--small-zoom` | 18 | zoom of the query tiles (~150 m) |
| `--min-coverage` | 80 | keep images whose segmentation covers at least this % of the image |
| `--max-minutes` | none | time budget of the run |
| `--max-big-tiles` | none | process at most this many big tiles |
| `--workers` | 8 | parallel detection requests |
| `--limit` | 2000 | images per `/images` request (tiles returning this many are split) |
| `--slug` | from the place | folder name of the city in `data/` |
| `--osm-relation` | none | use this OpenStreetMap relation as the boundary |
| `--data-dir` | `data` | output root |

The zooms and `--min-coverage` of a city are fixed once it has a
`progress.json`; to change them, delete `data/<city_slug>/` and start over.

## 5. What gets written

```
data/<city_slug>/
├── boundary.geojson      the city boundary (OpenStreetMap)
├── progress.json         status of every big tile and a summary
├── tiles/<z>_<x>_<y>.parquet   one GeoParquet per completed big tile
└── partial/              checkpoints of an unfinished big tile
```

Read the data with geopandas:

```python
import glob
import geopandas as gpd
import pandas as pd

tiles = glob.glob("data/curitiba/tiles/*.parquet")
gdf = pd.concat([gpd.read_parquet(p) for p in tiles], ignore_index=True)
print(gdf[["captured_at", "h", "vegetation_percent"]].describe())
```

Columns: `id`, `captured_at`, `lon`, `lat`, `h` (GPS altitude),
`h_computed` (Mapillary's relative altitude), `vegetation_percent`,
`segmented_percent`, `number_available_classes`, `geometry`.

## 6. Build and preview the webmaps

```bash
python scripts/build_webmaps.py
python -m http.server -d maps 8000
```

Then open <http://localhost:8000>: the index map lists every city with data,
and each city map shows its images colored by vegetation percent, with the
Positron, Dark and Regular (Liberty) basemaps from OpenFreeMap.

## 7. Run the tests

```bash
pip install pytest
pytest tests
```

They run offline, against fake OpenStreetMap and Mapillary APIs.

## Troubleshooting

- **"No OpenStreetMap result is named ..."** — the first part of the place
  (before the first comma) must match the OpenStreetMap name, ignoring case
  and accents. Use the local name, or `--osm-relation`.
- **"... is for ..., not for ..."** — `data/<city_slug>/boundary.geojson` was
  made for another place. Delete the folder or use another `--slug`.
- **"Mapillary rejected the token"** — check `API_TOKEN` (the client token,
  starting with `MLY|`).
- **Retries and pauses in the log** — the API is rate limiting; the script
  backs off (longer for HTTP 429) and continues by itself.
- **A big tile takes long** — dense city centers can have tens of thousands of
  images, each needing one detection request. Use `--max-minutes` and run
  again later; progress is kept.
