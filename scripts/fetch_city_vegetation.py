"""
Fetch the location, height and vegetation percent of every Mapillary image of
a city that has a full-scene semantic segmentation.

The city polygon is retrieved from OpenStreetMap (Nominatim) and covered with
"big" tiles (zoom 14 by default). Each big tile is queried through its "small"
tiles (zoom 18 by default); when all of them are done, the big tile is written
to data/<city_slug>/tiles/<z>_<x>_<y>.parquet (GeoParquet, EPSG:4326).

Progress is kept in data/<city_slug>/progress.json, so the script can be
stopped (time budget, Ctrl+C, SIGTERM) and resumed: a big tile is
"not_started", "in_progress" (its finished small tiles are checkpointed in
data/<city_slug>/partial/) or "completed".

Usage:
    python scripts/fetch_city_vegetation.py "Curitiba, Parana, Brazil"
    python scripts/fetch_city_vegetation.py "Curitiba, Parana, Brazil" --max-minutes 60

The Mapillary token is read from the API_TOKEN environment variable (or the
other sources supported by my_mappilary_api) and is never written anywhere.
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import mercantile
import pandas as pd
import requests
from shapely.geometry import Point, box, mapping, shape
from shapely.prepared import prep

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "my_mappilary_api"))

import mapillary_api as mly  # noqa: E402

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "mapillary_urban_vegetation (https://github.com/kauevestena/mapillary_urban_vegetation)"

IMAGE_FIELDS = ["id", "geometry", "computed_geometry", "altitude", "computed_altitude", "captured_at"]
VEGETATION_CLASS = "nature--vegetation"

# how many zoom levels a small tile can be split into when the API refuses it
# or its result is truncated
MAX_EXTRA_ZOOM = 3

# checkpoint the in-progress big tile at least this often (seconds)
CHECKPOINT_INTERVAL = 600

COLUMNS = ["id", "captured_at", "lon", "lat", "h", "vegetation_percent", "number_available_classes"]

# replaced in tests
now = time.monotonic
sleep = time.sleep

STOP_REQUESTED = False


def _request_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"\n⏹️  Signal {signum} received: finishing the current small tile, then checkpointing.", flush=True)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text):
    """'Curitiba, Paraná, Brazil' -> 'curitiba-parana-brazil'"""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def tile_key(tile):
    return f"{tile.z}_{tile.x}_{tile.y}"


def tile_box(tile):
    b = mercantile.bounds(tile)
    return box(b.west, b.south, b.east, b.north)


def write_json_atomic(data, path):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# City boundary
# ---------------------------------------------------------------------------


def fetch_boundary(place, timeout=60):
    """Get the (Multi)Polygon of a place from Nominatim, preferring administrative boundaries."""
    response = requests.get(
        NOMINATIM_URL,
        params={"q": place, "format": "jsonv2", "polygon_geojson": 1, "limit": 10},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    results = [
        r for r in response.json()
        if r.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")
    ]
    if not results:
        raise ValueError(f"No polygon found on OpenStreetMap for '{place}'")

    def rank(result):
        administrative = result.get("category") == "boundary" and result.get("type") == "administrative"
        return (administrative, float(result.get("importance") or 0))

    best = max(results, key=rank)
    return shape(best["geojson"]), best


def load_or_fetch_boundary(place, city_dir):
    """Reuse data/<slug>/boundary.geojson if present, otherwise fetch and save it."""
    path = city_dir / "boundary.geojson"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            feature = json.load(f)["features"][0]
        return shape(feature["geometry"])

    polygon, result = fetch_boundary(place)
    city_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "place": place,
                        "display_name": result.get("display_name"),
                        "osm_type": result.get("osm_type"),
                        "osm_id": result.get("osm_id"),
                        "retrieved_at": utc_now(),
                    },
                    "geometry": mapping(polygon),
                }
            ],
        },
        path,
    )
    print(f"🗺️  Boundary: {result.get('display_name')} ({result.get('osm_type')} {result.get('osm_id')})")
    return polygon


# ---------------------------------------------------------------------------
# Tiling and progress
# ---------------------------------------------------------------------------


def big_tiles_for(polygon, zoom):
    """Tiles at `zoom` intersecting the polygon."""
    prepared = prep(polygon)
    minx, miny, maxx, maxy = polygon.bounds
    return [t for t in mercantile.tiles(minx, miny, maxx, maxy, zoom) if prepared.intersects(tile_box(t))]


def small_tiles_for(big_tile, polygon, zoom):
    """Children of a big tile at `zoom` intersecting the polygon."""
    prepared = prep(polygon)
    return [t for t in mercantile.children(big_tile, zoom=zoom) if prepared.intersects(tile_box(t))]


def load_progress(path, place, slug, big_zoom, small_zoom, big_tiles):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            progress = json.load(f)
        if progress["big_zoom"] != big_zoom or progress["small_zoom"] != small_zoom:
            raise SystemExit(
                f"❌ {path} was created with big zoom {progress['big_zoom']} and small zoom "
                f"{progress['small_zoom']}; use the same zooms (or another --data-dir/--slug)."
            )
    else:
        progress = {
            "place": place,
            "slug": slug,
            "big_zoom": big_zoom,
            "small_zoom": small_zoom,
            "created_at": utc_now(),
            "tiles": {},
        }
    for tile in big_tiles:
        progress["tiles"].setdefault(
            tile_key(tile),
            {"status": "not_started", "small_tiles_total": None, "small_tiles_done": [], "images": 0, "images_segmented": 0},
        )
    return progress


def save_progress(progress, path):
    tiles = progress["tiles"].values()
    progress["updated_at"] = utc_now()
    progress["summary"] = {
        "big_tiles": len(progress["tiles"]),
        "completed": sum(t["status"] == "completed" for t in tiles),
        "in_progress": sum(t["status"] == "in_progress" for t in tiles),
        "not_started": sum(t["status"] == "not_started" for t in tiles),
        "images_segmented": sum(t["images_segmented"] for t in tiles),
    }
    write_json_atomic(progress, path)


# ---------------------------------------------------------------------------
# Mapillary queries
# ---------------------------------------------------------------------------


def with_retries(call, token, attempts=3, base_delay=2):
    for attempt in range(attempts):
        try:
            return call()
        except Exception as e:
            message = str(e).lower()
            # a refused (too dense) query is answered by splitting the tile, not by retrying
            if attempt == attempts - 1 or "reduce the amount of data" in message:
                raise
            delay = base_delay * 2**attempt * (5 if "429" in message or "rate" in message else 1)
            print(f"   retrying in {delay}s: {mly.redact_token(e, token)[:200]}", flush=True)
            sleep(delay)


def images_of_tile(tile, token, limit, max_zoom):
    """
    All images whose location falls in the tile. The tile is split into its
    children when the API refuses it or returns a possibly truncated list.
    Returns (images, saturated), saturated meaning max_zoom was reached with a
    full result.
    """
    b = mercantile.bounds(tile)
    try:
        data = with_retries(
            lambda: mly.get_mapillary_images_metadata(
                b.west, b.south, b.east, b.north, fields=IMAGE_FIELDS, token=token, limit=limit
            ),
            token,
        )
        images = data.get("data", [])
        too_much = len(images) >= limit
    except Exception as e:
        if "reduce the amount of data" not in str(e).lower() or tile.z >= max_zoom:
            raise
        images, too_much = [], True

    if too_much and tile.z < max_zoom:
        all_images, saturated = [], False
        for child in mercantile.children(tile):
            child_images, child_saturated = images_of_tile(child, token, limit, max_zoom)
            all_images.extend(child_images)
            saturated = saturated or child_saturated
        return all_images, saturated

    # keep only the images located in this tile, so that images lying on a
    # shared edge are assigned to exactly one tile
    own = [
        i for i in images
        if i.get("geometry") and mercantile.tile(*i["geometry"]["coordinates"][:2], tile.z) == tile
    ]
    return own, too_much


def image_row(image, detections):
    """The output row of an image, or None if it has no full-scene segmentation."""
    values = {d.get("value") for d in detections}
    if not any(mly.detection_class_group(v) == "surface" for v in values):
        return None

    summary = mly.detections_summary(detections)
    location = image.get("computed_geometry") or image["geometry"]
    lon, lat = location["coordinates"][:2]
    h = image.get("computed_altitude")
    if h is None:
        h = image.get("altitude")
    return {
        "id": str(image["id"]),
        "captured_at": image.get("captured_at"),
        "lon": float(lon),
        "lat": float(lat),
        "h": float(h) if h is not None else float("nan"),
        "vegetation_percent": float(summary["class_percents"].get(VEGETATION_CLASS, 0.0)),
        "number_available_classes": int(summary["number_available_classes"]),
    }


def process_small_tile(tile, polygon, token, args, executor):
    """
    Fetch the rows of one small tile. Raises if anything fails, so the tile
    stays not done and is retried later. Returns (rows, images_count).
    """
    images, saturated = images_of_tile(tile, token, args.limit, tile.z + MAX_EXTRA_ZOOM)
    if saturated:
        print(f"⚠️  {tile_key(tile)}: more than {args.limit} images even at zoom {tile.z + MAX_EXTRA_ZOOM}; some may be missing")

    prepared = prep(polygon)
    images = [i for i in images if prepared.contains(Point(i["geometry"]["coordinates"][:2]))]

    def fetch(image):
        return with_retries(
            lambda: mly.get_image_detections(image["id"], token=token, fields=["value", "geometry"]),
            token,
        )

    rows = []
    for image, detections in zip(images, executor.map(fetch, images)):
        row = image_row(image, detections)
        if row:
            row["_small_tile"] = tile_key(tile)
            rows.append(row)
    return rows, len(images)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def rows_to_gdf(rows):
    df = pd.DataFrame(rows, columns=COLUMNS + ["_small_tile"])
    df["captured_at"] = pd.to_datetime(df["captured_at"], unit="ms", utc=True)
    for column in ("lon", "lat", "h", "vegetation_percent"):
        df[column] = df[column].astype(float)
    df["number_available_classes"] = df["number_available_classes"].astype("int64")
    df["id"] = df["id"].astype(str)
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")


def write_parquet(rows, path, keep_small_tile=False):
    gdf = rows_to_gdf(rows).drop_duplicates(subset="id")
    if not keep_small_tile:
        gdf = gdf.drop(columns="_small_tile")
    tmp = f"{path}.tmp"
    gdf.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return len(gdf)


def load_partial(path, done):
    """Rows of a partial checkpoint, restricted to the small tiles recorded as done."""
    if not path.exists():
        return []
    gdf = gpd.read_parquet(path)
    gdf = gdf[gdf["_small_tile"].isin(done)]
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    # back to epoch milliseconds, whatever the datetime resolution read from parquet
    epoch = pd.Timestamp(0, tz="UTC")
    df["captured_at"] = (df["captured_at"] - epoch) / pd.Timedelta(milliseconds=1)
    return df.to_dict("records")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run(args, token):
    slug = args.slug or slugify(args.place)
    city_dir = Path(args.data_dir) / slug
    (city_dir / "tiles").mkdir(parents=True, exist_ok=True)
    (city_dir / "partial").mkdir(parents=True, exist_ok=True)
    progress_path = city_dir / "progress.json"

    polygon = load_or_fetch_boundary(args.place, city_dir)
    big_tiles = big_tiles_for(polygon, args.big_zoom)
    progress = load_progress(progress_path, args.place, slug, args.big_zoom, args.small_zoom, big_tiles)
    save_progress(progress, progress_path)

    started = now()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None

    def out_of_time():
        return STOP_REQUESTED or (deadline is not None and now() >= deadline)

    order = {"in_progress": 0, "not_started": 1}
    todo = sorted(
        (t for t in big_tiles if progress["tiles"][tile_key(t)]["status"] != "completed"),
        key=lambda t: (order[progress["tiles"][tile_key(t)]["status"]], tile_key(t)),
    )
    if args.max_big_tiles:
        todo = todo[: args.max_big_tiles]

    print(
        f"🏙️  {args.place} → {city_dir}: {len(big_tiles)} big tiles (zoom {args.big_zoom}), "
        f"{progress['summary']['completed']} completed, {len(todo)} to process this run",
        flush=True,
    )

    completed_now = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for big_tile in todo:
            if out_of_time():
                break

            key = tile_key(big_tile)
            state = progress["tiles"][key]
            partial_path = city_dir / "partial" / f"{key}.parquet"
            small_tiles = small_tiles_for(big_tile, polygon, args.small_zoom)

            done = set(state["small_tiles_done"])
            rows = load_partial(partial_path, done)
            state.update(status="in_progress", small_tiles_total=len(small_tiles))
            save_progress(progress, progress_path)
            print(f"🧩 {key}: {len(done)}/{len(small_tiles)} small tiles already done", flush=True)

            last_checkpoint = now()
            failures = 0
            for small_tile in small_tiles:
                small_key = tile_key(small_tile)
                if small_key in done:
                    continue
                if out_of_time():
                    break
                try:
                    tile_rows, images = process_small_tile(small_tile, polygon, token, args, executor)
                except Exception as e:
                    failures += 1
                    print(f"❌ {small_key}: {mly.redact_token(e, token)[:300]}", flush=True)
                    continue
                rows.extend(tile_rows)
                done.add(small_key)
                state["images"] += images
                if now() - last_checkpoint >= CHECKPOINT_INTERVAL:
                    checkpoint(rows, done, state, partial_path, progress, progress_path)
                    last_checkpoint = now()

            if len(done) == len(small_tiles):
                count = write_parquet(rows, city_dir / "tiles" / f"{key}.parquet")
                state.update(
                    status="completed", small_tiles_done=[], images_segmented=count, completed_at=utc_now()
                )
                partial_path.unlink(missing_ok=True)
                save_progress(progress, progress_path)
                completed_now += 1
                print(f"✅ {key}: completed, {count} segmented images of {state['images']}", flush=True)
            else:
                checkpoint(rows, done, state, partial_path, progress, progress_path)
                reason = "failed small tiles will be retried next run" if failures and not out_of_time() else "checkpointed"
                print(f"⏸️  {key}: {len(done)}/{len(small_tiles)} small tiles done, {reason}", flush=True)

    elapsed = (now() - started) / 60
    summary = progress["summary"]
    print(
        f"🏁 {completed_now} big tiles completed this run ({elapsed:.1f} min). Overall: "
        f"{summary['completed']}/{summary['big_tiles']} completed, {summary['in_progress']} in progress, "
        f"{summary['images_segmented']} segmented images",
        flush=True,
    )
    return progress


def checkpoint(rows, done, state, partial_path, progress, progress_path):
    """Save the rows of the finished small tiles, then record them as done."""
    write_parquet(rows, partial_path, keep_small_tile=True)
    state["small_tiles_done"] = sorted(done)
    state["images_segmented"] = len({r["id"] for r in rows})
    save_progress(progress, progress_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("place", help='Place to query on OpenStreetMap, e.g. "Curitiba, Parana, Brazil"')
    parser.add_argument("--big-zoom", type=int, default=14, help="Zoom of the output tiles (default: 14)")
    parser.add_argument("--small-zoom", type=int, default=18, help="Zoom of the query tiles (default: 18)")
    parser.add_argument("--limit", type=int, default=2000, help="Images per /images request (default: 2000)")
    parser.add_argument("--max-minutes", type=float, default=None, help="Stop (and checkpoint) after this many minutes")
    parser.add_argument("--max-big-tiles", type=int, default=None, help="Process at most this many big tiles")
    parser.add_argument("--workers", type=int, default=8, help="Parallel detection requests (default: 8)")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"), help="Output root (default: data/)")
    parser.add_argument("--slug", default=None, help="Folder name of the city (default: from the place)")
    args = parser.parse_args(argv)

    if args.small_zoom <= args.big_zoom:
        parser.error("--small-zoom must be larger than --big-zoom")

    token = mly.get_mapillary_token()
    if not token:
        parser.error("No Mapillary token found: set the API_TOKEN environment variable")

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    run(args, token)


if __name__ == "__main__":
    main()
