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
import threading
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
from shapely.ops import unary_union
from shapely.prepared import prep
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "my_mappilary_api"))

import mapillary_api as mly  # noqa: E402

NOMINATIM_URL = "https://nominatim.openstreetmap.org"
POLYGONS_OSM_FR_URL = "https://polygons.openstreetmap.fr/get_geojson.py"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SETTLEMENT_TYPES = {"city", "town", "village", "municipality"}
USER_AGENT = "mapillary_urban_vegetation (https://github.com/kauevestena/mapillary_urban_vegetation)"

IMAGE_FIELDS = ["id", "geometry", "computed_geometry", "altitude", "computed_altitude", "captured_at"]
VEGETATION_CLASS = "nature--vegetation"

# A tile is split into its 4 children until each request returns fewer images
# than the limit. This zoom (~0.5 m tiles) is only a safety floor: reaching it
# means more than `limit` images share the same spot.
MAX_SPLIT_ZOOM = 26

# retries of a failed request (network errors, HTTP 5xx, rate limiting)
RETRY_ATTEMPTS = 6
# rate-limited requests wait this many times longer than other retries
RATE_LIMIT_FACTOR = 5

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


def _normalize(text):
    """Accent- and case-insensitive form of a name: 'Paraná ' -> 'parana'"""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def _nominatim(path, params, timeout=60):
    response = requests.get(
        f"{NOMINATIM_URL}/{path}", params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout
    )
    response.raise_for_status()
    sleep(1)  # Nominatim usage policy: at most 1 request per second
    return response.json()


def _as_polygon(geojson):
    """A (Multi)Polygon from a GeoJSON geometry (collections are merged), or None."""
    if not geojson:
        return None
    geometry = shape(geojson)
    if geometry.geom_type == "GeometryCollection":
        parts = [g for g in geometry.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        geometry = unary_union(parts) if parts else None
    if geometry is None or geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return geometry if geometry.is_valid else geometry.buffer(0)


def _describe(result):
    return (
        f"{result.get('display_name')} [{result.get('osm_type')} {result.get('osm_id')}, "
        f"{result.get('category')}/{result.get('type')}, importance {float(result.get('importance') or 0):.2f}]"
    )


def _is_area(result):
    category, kind = result.get("category"), result.get("type")
    return (category == "boundary" and kind == "administrative") or (category == "place" and kind in SETTLEMENT_TYPES)


def _relation_or_way_polygon(osm_type, osm_id, result):
    """Polygon of a relation or way: Nominatim lookup, then polygons.openstreetmap.fr (relations)."""
    try:
        found = _nominatim(
            "lookup", {"osm_ids": f"{osm_type[0].upper()}{osm_id}", "format": "jsonv2", "polygon_geojson": 1}
        )
        for item in found:
            polygon = _as_polygon(item.get("geojson"))
            if polygon is not None:
                return polygon, {**result, **item}
    except Exception as e:
        print(f"   Nominatim lookup of {osm_type} {osm_id} failed: {e}")

    if osm_type == "relation":
        try:
            response = requests.get(
                POLYGONS_OSM_FR_URL, params={"id": osm_id, "params": 0}, headers={"User-Agent": USER_AGENT}, timeout=120
            )
            response.raise_for_status()
            polygon = _as_polygon(response.json())
            if polygon is not None:
                return polygon, {**result, "osm_type": "relation", "osm_id": osm_id}
        except Exception as e:
            print(f"   polygons.openstreetmap.fr for relation {osm_id} failed: {e}")

    return None, None


def _enclosing_boundaries(lat, lon):
    """Administrative relations containing a point (Overpass API), as [{'id', 'name', 'admin_level'}]."""
    query = f"[out:json][timeout:90];is_in({lat},{lon})->.a;rel(pivot.a)[boundary=administrative];out tags;"
    response = requests.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=120)
    response.raise_for_status()
    boundaries = []
    for element in response.json().get("elements", []):
        tags = element.get("tags", {})
        try:
            level = int(tags.get("admin_level", 0))
        except ValueError:
            level = 0
        boundaries.append({"id": element["id"], "name": tags.get("name"), "admin_level": level})
    return boundaries


def _polygon_of(result, wanted_name):
    """
    The polygon of a Nominatim result. Relations and ways: their own polygon.
    Nodes (e.g. place=city): the administrative boundary with the same name
    that contains the node (the most local one if several), found with the
    Overpass API, then Nominatim reverse geocoding as a fallback.
    Returns (polygon, source result) or (None, None).
    """
    osm_type, osm_id = result.get("osm_type"), result.get("osm_id")

    if osm_type in ("relation", "way"):
        return _relation_or_way_polygon(osm_type, osm_id, result)

    if osm_type != "node" or result.get("lat") is None:
        return None, None

    try:
        boundaries = _enclosing_boundaries(result["lat"], result["lon"])
        same_name = [b for b in boundaries if _normalize(b["name"]) == wanted_name]
        print(
            f"   boundaries around node {osm_id}: "
            + (", ".join(f"{b['name']} (relation {b['id']}, level {b['admin_level']})" for b in boundaries) or "none")
        )
        for boundary in sorted(same_name, key=lambda b: b["admin_level"], reverse=True):
            polygon, source = _relation_or_way_polygon("relation", boundary["id"], {"name": boundary["name"]})
            if polygon is not None:
                return polygon, source
    except Exception as e:
        print(f"   Overpass query around node {osm_id} failed: {e}")

    try:
        found = _nominatim(
            "reverse",
            {"lat": result["lat"], "lon": result["lon"], "zoom": 10, "format": "jsonv2", "polygon_geojson": 1},
        )
        polygon = _as_polygon(found.get("geojson"))
        if polygon is None:
            print(f"   reverse geocoding of node {osm_id} gave no polygon ({_describe(found)})")
        elif _normalize(found.get("name")) != wanted_name:
            print(f"   reverse geocoding of node {osm_id} gave another area: {_describe(found)}")
        else:
            return polygon, found
    except Exception as e:
        print(f"   Nominatim reverse geocoding of node {osm_id} failed: {e}")

    return None, None


def _contains_own_point(polygon, result):
    if result.get("lat") is None or result.get("lon") is None:
        return True
    return polygon.buffer(0.01).contains(Point(float(result["lon"]), float(result["lat"])))


def fetch_boundary(place, osm_relation=None):
    """
    Get the (Multi)Polygon of a place from OpenStreetMap.

    The candidates of a Nominatim search whose name matches the first part of
    `place` (e.g. "Curitiba" for "Curitiba, Parana, Brazil", ignoring case and
    accents) are tried from the best ranked (administrative area or
    settlement, then importance); the first one whose polygon can be resolved
    and contains the candidate's own location is used. With `osm_relation`,
    that relation is used directly.

    Returns (polygon, metadata of the OSM feature).
    """
    if osm_relation:
        result = {"osm_type": "relation", "osm_id": int(osm_relation)}
        polygon, source = _polygon_of(result, wanted_name=None)
        if polygon is None:
            raise ValueError(f"Could not get the polygon of OSM relation {osm_relation}")
        return polygon, source

    wanted = _normalize(place.split(",")[0])
    candidates = _nominatim("search", {"q": place, "format": "jsonv2", "limit": 10, "addressdetails": 1})
    print(f"🔎 Nominatim candidates for {place!r}:")
    for candidate in candidates:
        print(f"   - {_describe(candidate)}")

    matching = [c for c in candidates if _normalize(c.get("name")) == wanted]
    if not matching:
        raise ValueError(
            f"No OpenStreetMap result is named {place.split(',')[0].strip()!r}; candidates: "
            + "; ".join(_describe(c) for c in candidates)
            + ". Refine the place or pass --osm-relation."
        )

    ranked = sorted(matching, key=lambda c: (_is_area(c), float(c.get("importance") or 0)), reverse=True)
    for candidate in ranked:
        polygon, source = _polygon_of(candidate, wanted)
        if polygon is None:
            continue
        if not _contains_own_point(polygon, candidate):
            print(f"   the polygon of {_describe(source)} does not contain {_describe(candidate)}, skipping")
            continue
        return polygon, source

    raise ValueError(
        f"Could not get a polygon for {place!r} from: " + "; ".join(_describe(c) for c in ranked)
        + ". Pass --osm-relation to choose the boundary explicitly."
    )


def load_or_fetch_boundary(place, city_dir, osm_relation=None):
    """Reuse data/<slug>/boundary.geojson if it is for this place, otherwise fetch and save it."""
    path = city_dir / "boundary.geojson"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            feature = json.load(f)["features"][0]
        properties = feature["properties"]
        same = (
            str(properties.get("osm_id")) == str(osm_relation) and properties.get("osm_type") == "relation"
            if osm_relation
            else properties.get("place") == place
        )
        if not same:
            raise SystemExit(
                f"❌ {path} is for {properties.get('display_name')!r} (place {properties.get('place')!r}, "
                f"{properties.get('osm_type')} {properties.get('osm_id')}), not for {place!r}"
                + (f" / relation {osm_relation}" if osm_relation else "")
                + f". Delete {city_dir} to start this city over, or use another --slug."
            )
        print(f"🗺️  Boundary: {properties.get('display_name')} ({properties.get('osm_type')} {properties.get('osm_id')})")
        return shape(feature["geometry"])

    polygon, result = fetch_boundary(place, osm_relation)
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


def area_km2(polygon):
    return float(gpd.GeoSeries([polygon], crs="EPSG:4326").to_crs("EPSG:6933").area.iloc[0] / 1e6)


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


class TokenRejected(Exception):
    """The API rejected the token: no point in trying other tiles."""


def is_refused(error):
    """The API refuses a too dense query; the tile must be split, not retried."""
    return "reduce the amount of data" in str(error).lower()


# Status codes as they appear in error messages: "429 Client Error: ..." from
# requests, "(HTTP 429)" from my_mappilary_api. Bare numbers are not matched,
# since messages also contain URLs with coordinates.
def _has_status(message, pattern):
    return re.search(rf"\b({pattern}) (client|server) error|\(http ({pattern})\)", message) is not None


def is_rate_limited(error):
    message = str(error).lower()
    return _has_status(message, "429") or "too many requests" in message or "rate limit" in message


def is_token_rejected(error):
    message = str(error).lower()
    return (
        _has_status(message, "401|403")
        or "oauth" in message
        or "invalid token" in message
        or "access token" in message
    )


def is_transient(error):
    """Errors worth retrying: network failures, HTTP 5xx and rate limiting."""
    if is_refused(error) or is_token_rejected(error):
        return False
    return (
        isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
        or is_rate_limited(error)
        or _has_status(str(error).lower(), r"5\d\d")
    )


# When any request is rate limited, every worker thread pauses until then.
_pause_lock = threading.Lock()
_pause_until = 0.0


def _wait_for_pause():
    with _pause_lock:
        remaining = _pause_until - now()
    if remaining > 0:
        sleep(remaining)


def _retry_wait(retry_state):
    delay = wait_exponential_jitter(initial=2, max=120, jitter=2)(retry_state)
    if is_rate_limited(retry_state.outcome.exception()):
        delay *= RATE_LIMIT_FACTOR
    return delay


def with_retries(call, token):
    """Call the API with tenacity: exponential backoff, longer and shared when rate limited."""

    def before_sleep(retry_state):
        global _pause_until
        error = retry_state.outcome.exception()
        delay = retry_state.next_action.sleep
        if is_rate_limited(error):
            with _pause_lock:
                _pause_until = max(_pause_until, now() + delay)
        print(
            f"   retry {retry_state.attempt_number}/{RETRY_ATTEMPTS - 1} in {delay:.0f}s: "
            f"{mly.redact_token(error, token)[:200]}",
            flush=True,
        )

    retrying = Retrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=_retry_wait,
        sleep=lambda seconds: sleep(seconds),  # module-level sleep, replaced in tests
        before_sleep=before_sleep,
        reraise=True,
    )
    for attempt in retrying:
        with attempt:
            _wait_for_pause()
            result = call()
    return result


def images_of_tile(tile, token, limit, max_zoom=MAX_SPLIT_ZOOM):
    """
    All images whose location falls in the tile. The tile is recursively split
    into its 4 children while the API refuses it or returns `limit` images (a
    possibly truncated list), so that no image is lost. Returns (images,
    saturated), saturated meaning the safety floor max_zoom was reached with a
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
        if not is_refused(e) or tile.z >= max_zoom:
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
    images, saturated = images_of_tile(tile, token, args.limit)
    if saturated:
        print(f"⚠️  {tile_key(tile)}: {args.limit}+ images at a single spot (zoom {MAX_SPLIT_ZOOM}); some may be missing")

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
    # always millisecond resolution, whether the values come from the API or a checkpoint
    df["captured_at"] = pd.to_datetime(df["captured_at"], unit="ms", utc=True).astype("datetime64[ms, UTC]")
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
    # back to integer epoch milliseconds (no float round trip, which loses precision)
    captured = df["captured_at"].astype("datetime64[ms, UTC]")
    df["captured_at"] = captured.astype("int64").astype(object).where(captured.notna(), None)
    return df.to_dict("records")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run(args, token):
    slug = args.slug or slugify(args.place)
    city_dir = Path(args.data_dir) / slug
    progress_path = city_dir / "progress.json"

    polygon = load_or_fetch_boundary(args.place, city_dir, args.osm_relation)
    big_tiles = big_tiles_for(polygon, args.big_zoom)
    if args.boundary_only:
        print(f"📐 {area_km2(polygon):.1f} km², {len(big_tiles)} big tiles at zoom {args.big_zoom}")
        return None
    (city_dir / "tiles").mkdir(parents=True, exist_ok=True)
    (city_dir / "partial").mkdir(parents=True, exist_ok=True)
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
                    if is_token_rejected(e):
                        checkpoint(rows, done, state, partial_path, progress, progress_path)
                        raise SystemExit(f"❌ Mapillary rejected the token: {mly.redact_token(e, token)[:300]}")
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
    parser.add_argument("--osm-relation", type=int, default=None, help="Use this OpenStreetMap relation as the boundary")
    parser.add_argument("--boundary-only", action="store_true", help="Only resolve and save the boundary, then exit")
    args = parser.parse_args(argv)

    if args.small_zoom <= args.big_zoom:
        parser.error("--small-zoom must be larger than --big-zoom")

    token = mly.get_mapillary_token()
    if not token and not args.boundary_only:
        parser.error("No Mapillary token found: set the API_TOKEN environment variable")

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    run(args, token)


if __name__ == "__main__":
    main()
