import shapely
"""
Build the MapLibre webmaps from the fetched data.

For every city folder in data/ that has a boundary and at least one
completed tile with images, writes maps/<slug>/ (index.html, points.geojson,
boundary.geojson); then writes the index map (maps/index.html,
maps/cities.json, maps/cities.geojson) that opens them. The basemaps are
OpenFreeMap's Positron, Dark and Liberty ("Regular") styles.

Usage:
    python scripts/build_webmaps.py                  # data/ -> maps/
    python scripts/build_webmaps.py --data-dir data --out maps

Preview: python -m http.server -d maps  (then open http://localhost:8000)
"""

import argparse
import html
import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import mapping, shape

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "webmap"

# marks the city folders created by this script (only these are ever removed)
GENERATED_MARKER = ".generated"

# boundary simplification tolerances, in degrees (~5 m and ~50 m)
CITY_BOUNDARY_TOLERANCE = 0.00005
INDEX_BOUNDARY_TOLERANCE = 0.0005

# Maximum file size before slicing into .partXXX files (98 MB limit)
MAX_GITHUB_FILE_BYTES = 98 * 1000 * 1000


def parquet_parts(path):
    """Return existing file path(s) for a logical parquet path (single file or .partXXX slices)."""
    path = Path(path)
    if path.exists():
        return [path]
    return sorted(path.parent.glob(f"{path.stem}.part*{path.suffix}"))


def parquet_exists(path):
    """True if either the single parquet file or its .partXXX slices exist."""
    return bool(parquet_parts(path))


def read_parquet_sliced(path):
    """Read a GeoParquet dataset from a single file or .partXXX slices."""
    parts = parquet_parts(path)
    if not parts:
        raise FileNotFoundError(f"No parquet file or slices found for {path}")
    if len(parts) == 1:
        return gpd.read_parquet(parts[0])
    frames = [gpd.read_parquet(p) for p in parts]
    crs = frames[0].crs
    combined = pd.concat(frames, ignore_index=True)
    return gpd.GeoDataFrame(combined, geometry="geometry", crs=crs)


def write_json(data, path):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def write_geojson_sliced(data, path, max_bytes=MAX_GITHUB_FILE_BYTES):
    """Write a GeoJSON FeatureCollection to `path`, or slice into `<stem>.partXXX.geojson` if > max_bytes.

    Returns the list of written filenames (relative to `path.parent`).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    features = data.get("features", [])

    if len(raw) <= max_bytes or len(features) <= 1:
        for old_part in path.parent.glob(f"{path.stem}.part*{path.suffix}"):
            old_part.unlink(missing_ok=True)
        tmp = path.parent / f"{path.name}.tmp"
        tmp.write_bytes(raw)
        os.replace(tmp, path)
        return [path.name]

    n_initial = max(2, math.ceil(len(raw) / (max_bytes * 0.85)))
    step_size = math.ceil(len(features) / n_initial)
    initial_slices = [features[i : i + step_size] for i in range(0, len(features), step_size)]

    encoded_chunks = []

    def _encode_slice(sub_feats):
        sub_bytes = json.dumps(
            {"type": "FeatureCollection", "features": sub_feats},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(sub_bytes) > max_bytes and len(sub_feats) > 1:
            mid = len(sub_feats) // 2
            _encode_slice(sub_feats[:mid])
            _encode_slice(sub_feats[mid:])
        else:
            encoded_chunks.append(sub_bytes)

    for s in initial_slices:
        _encode_slice(s)

    path.unlink(missing_ok=True)
    for old_part in path.parent.glob(f"{path.stem}.part*{path.suffix}"):
        old_part.unlink(missing_ok=True)

    written_names = []
    for i, chunk_bytes in enumerate(encoded_chunks):
        part_path = path.parent / f"{path.stem}.part{i:03d}{path.suffix}"
        tmp = path.parent / f"{part_path.name}.tmp"
        tmp.write_bytes(chunk_bytes)
        os.replace(tmp, part_path)
        written_names.append(part_path.name)
    return written_names


def write_pmtiles_sliced(
    gdf,
    path,
    layer=None,
    minzoom=9,
    maxzoom=14,
    max_bytes=MAX_GITHUB_FILE_BYTES,
    **kwargs,
):
    """Write a GeoDataFrame to PMTiles at `path`, or slice into `<stem>.partXXX.pmtiles` if > max_bytes.

    Returns the list of written filenames (relative to `path.parent`).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    layer_name = layer or path.stem
    pm_opts = {
        "MINZOOM": minzoom,
        "MAXZOOM": maxzoom,
        "MAX_SIZE": 5000000,
        "MAX_FEATURES": 500000,
        **kwargs,
    }

    for stale_tmp in path.parent.glob(f"{path.stem}.*.tmp.pmtiles"):
        stale_tmp.unlink(missing_ok=True)

    tmp_main = path.parent / f"{path.stem}.main.tmp.pmtiles"
    gdf.to_file(tmp_main, driver="PMTiles", layer=layer_name, **pm_opts)
    total_size = tmp_main.stat().st_size

    if total_size <= max_bytes or len(gdf) <= 1:
        for old_part in path.parent.glob(f"{path.stem}.part*{path.suffix}"):
            old_part.unlink(missing_ok=True)
        for old_gj in path.parent.glob(f"{path.stem}*.geojson"):
            old_gj.unlink(missing_ok=True)
        os.replace(tmp_main, path)
        return [path.name]

    tmp_main.unlink(missing_ok=True)
    try:
        order = np.argsort(gdf.geometry.bounds["minx"].values)
        sorted_gdf = gdf.iloc[order]
    except Exception:
        sorted_gdf = gdf

    n_initial = max(2, math.ceil(total_size / (max_bytes * 0.85)))
    initial_slices = [sorted_gdf.iloc[idx] for idx in np.array_split(np.arange(len(sorted_gdf)), n_initial) if len(idx) > 0]

    chunk_tmps = []

    def _write_chunk(sub_gdf):
        tmp_chunk = path.parent / f"{path.stem}.chunk_{len(chunk_tmps)}.tmp.pmtiles"
        sub_gdf.to_file(tmp_chunk, driver="PMTiles", layer=layer_name, **pm_opts)
        if tmp_chunk.stat().st_size > max_bytes and len(sub_gdf) > 1:
            tmp_chunk.unlink(missing_ok=True)
            mid = len(sub_gdf) // 2
            _write_chunk(sub_gdf.iloc[:mid])
            _write_chunk(sub_gdf.iloc[mid:])
        else:
            chunk_tmps.append(tmp_chunk)

    for sub in initial_slices:
        _write_chunk(sub)

    path.unlink(missing_ok=True)
    for old_part in path.parent.glob(f"{path.stem}.part*{path.suffix}"):
        old_part.unlink(missing_ok=True)
    for old_gj in path.parent.glob(f"{path.stem}*.geojson"):
        old_gj.unlink(missing_ok=True)

    written_names = []
    for i, tmp_chunk in enumerate(chunk_tmps):
        part_path = path.parent / f"{path.stem}.part{i:03d}{path.suffix}"
        os.replace(tmp_chunk, part_path)
        written_names.append(part_path.name)
    return written_names


def _round(value, digits):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def load_city(city_dir):
    """The data of a city folder, or None if it has no image to map yet."""
    boundary_path = city_dir / "boundary.geojson"
    tiles = sorted((city_dir / "tiles").glob("*.parquet"))
    if not boundary_path.exists() or not tiles:
        return None

    frames = [gpd.read_parquet(path) for path in tiles]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return None
    points = pd.concat(frames, ignore_index=True).drop_duplicates(subset="id")

    with open(boundary_path, encoding="utf-8") as f:
        feature = json.load(f)["features"][0]
    progress = {}
    if (city_dir / "progress.json").exists():
        with open(city_dir / "progress.json", encoding="utf-8") as f:
            progress = json.load(f)

    properties = feature["properties"]
    display_name = properties.get("display_name") or properties.get("place") or city_dir.name
    return {
        "slug": city_dir.name,
        "name": display_name.split(",")[0].strip(),
        "display_name": display_name,
        "boundary": shape(feature["geometry"]),
        "points": points,
        "progress": progress,
        "city_dir": city_dir,
    }


def city_stats(city):
    points, summary = city["points"], city["progress"].get("summary", {})
    dates = points["captured_at"].dropna()
    return {
        "images": int(len(points)),
        "vegetation_median": _round(points["vegetation_percent"].median(), 1),
        "vegetation_mean": _round(points["vegetation_percent"].mean(), 1),
        "date_min": dates.min().strftime("%Y-%m-%d") if len(dates) else None,
        "date_max": dates.max().strftime("%Y-%m-%d") if len(dates) else None,
        "tiles_completed": summary.get("completed"),
        "tiles_total": summary.get("big_tiles"),
        "updated": (city["progress"].get("updated_at") or "")[:10] or None,
    }


def points_geojson(points):
    features = []
    for row in points.itertuples(index=False):
        captured = row.captured_at
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(row.lon, 6), round(row.lat, 6)]},
            "properties": {
                "id": str(row.id),
                "veg": _round(row.vegetation_percent, 1),
                "date": captured.strftime("%Y-%m-%d") if not pd.isna(captured) else None,
                "h": _round(getattr(row, "h", None), 1),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def points_gdf(points):
    df = pd.DataFrame({
        "id": points["id"].astype(str),
        "veg": pd.to_numeric(points["vegetation_percent"], errors="coerce").round(1),
        "date": pd.to_datetime(points["captured_at"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d"),
        "h": pd.to_numeric(points["h"], errors="coerce").round(1) if "h" in points.columns else np.nan,
    })
    geom = points["geometry"] if "geometry" in points.columns else gpd.points_from_xy(points["lon"], points["lat"])
    return gpd.GeoDataFrame(df, geometry=geom, crs="EPSG:4326")


def render(template_name, replacements):
    text = (TEMPLATES / template_name).read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def config_json(config):
    # safe inside <script type="application/json">
    return json.dumps(config, ensure_ascii=False).replace("</", "<\\/")


def pmtiles_parts(path):
    """Return existing filename(s) for a logical PMTiles path (single file or .partXXX slices)."""
    path = Path(path)
    if path.exists():
        return [path.name]
    return [p.name for p in sorted(path.parent.glob(f"{path.stem}.part*{path.suffix}"))]


def build_city(city, out_dir, max_bytes=MAX_GITHUB_FILE_BYTES, reuse_pmtiles=False):
    city_out = out_dir / city["slug"]
    city_out.mkdir(parents=True, exist_ok=True)
    (city_out / GENERATED_MARKER).write_text("generated by scripts/build_webmaps.py\n", encoding="utf-8")

    stats = city_stats(city)
    boundary = city["boundary"].simplify(CITY_BOUNDARY_TOLERANCE, preserve_topology=True)
    minx, miny, maxx, maxy = city["boundary"].bounds
    city_dir = city.get("city_dir")
    has_segments = False
    has_voronoi = False
    segments_files = []
    voronoi_files = []

    if city_dir:
        seg_file = city_dir / "segments.parquet"
        vor_file = city_dir / "voronoi.parquet"
        existing_seg_pm = pmtiles_parts(city_out / "segments.pmtiles") if reuse_pmtiles else []
        if existing_seg_pm:
            segments_files = existing_seg_pm
            has_segments = True
        elif parquet_exists(seg_file):
            try:
                segs = read_parquet_sliced(seg_file)
                if not segs.empty:
                    segs_simple = segs.copy()
                    segs_simple["geometry"] = segs_simple.geometry.simplify(0.00002, preserve_topology=True)
                    segments_files = write_pmtiles_sliced(
                        segs_simple,
                        city_out / "segments.pmtiles",
                        layer="segments",
                        minzoom=9,
                        maxzoom=14,
                        max_bytes=max_bytes,
                    )
                    has_segments = True
            except Exception as e:
                print(f"   warning: failed to write segments.pmtiles: {e}")

        existing_vor_pm = pmtiles_parts(city_out / "voronoi.pmtiles") if reuse_pmtiles else []
        if existing_vor_pm:
            voronoi_files = existing_vor_pm
            has_voronoi = True
        elif parquet_exists(vor_file):
            try:
                vor = read_parquet_sliced(vor_file)
                if not vor.empty:
                    vor = vor[
                        vor.geometry.notnull()
                        & ~vor.geometry.is_empty
                        & vor.geom_type.isin(["Polygon", "MultiPolygon"])
                    ].copy()
                    # Simplify shared edges topologically while strictly preserving boundaries and coverage
                    if hasattr(shapely, "coverage_simplify"):
                        sim = shapely.coverage_simplify(vor.geometry.values, tolerance=0.00005, simplify_boundary=False)
                        vor_out = vor.copy()
                        vor_out["geometry"] = sim
                    else:
                        vor_out = vor
                    voronoi_files = write_pmtiles_sliced(
                        vor_out,
                        city_out / "voronoi.pmtiles",
                        layer="voronoi",
                        minzoom=9,
                        maxzoom=14,
                        max_bytes=max_bytes,
                    )
                    has_voronoi = True
            except Exception as e:
                print(f"   warning: failed to write voronoi.pmtiles: {e}")

    existing_pts_pm = pmtiles_parts(city_out / "points.pmtiles") if reuse_pmtiles else []
    if existing_pts_pm:
        points_files = existing_pts_pm
    else:
        points_files = write_pmtiles_sliced(
            points_gdf(city["points"]),
            city_out / "points.pmtiles",
            layer="points",
            minzoom=10,
            maxzoom=13,
            EXTENT=8192,
            max_bytes=max_bytes,
        )

    existing_bnd_pm = pmtiles_parts(city_out / "boundary.pmtiles") if reuse_pmtiles else []
    if existing_bnd_pm:
        boundary_files = existing_bnd_pm
    else:
        boundary_gdf = gpd.GeoDataFrame(
            {"slug": [city["slug"]], "name": [city["name"]]},
            geometry=[boundary.boundary],
            crs="EPSG:4326",
        )
        boundary_files = write_pmtiles_sliced(
            boundary_gdf,
            city_out / "boundary.pmtiles",
            layer="boundary",
            minzoom=6,
            maxzoom=14,
            max_bytes=max_bytes,
        )

    downloads = {"segments": [], "voronoi": [], "points": []}
    if city_dir:
        pts_pq = city_dir / "points.parquet"
        if not parquet_exists(pts_pq) and not city["points"].empty:
            try:
                pts_gdf = gpd.GeoDataFrame(city["points"], geometry="geometry", crs="EPSG:4326")
                pts_gdf.to_parquet(pts_pq, index=False)
            except Exception:
                pass
        downloads = {
            "segments": [p.name for p in parquet_parts(city_dir / "segments.parquet")],
            "voronoi": [p.name for p in parquet_parts(city_dir / "voronoi.parquet")],
            "points": [p.name for p in parquet_parts(pts_pq)],
        }

    config = {
        "slug": city["slug"],
        "name": city["name"],
        "display_name": city["display_name"],
        "bounds": [minx, miny, maxx, maxy],
        "stats": stats,
        "has_segments": has_segments,
        "has_voronoi": has_voronoi,
        "files": {
            "points": points_files,
            "segments": segments_files,
            "voronoi": voronoi_files,
            "boundary": boundary_files,
        },
        "downloads": downloads,
    }

    (city_out / "index.html").write_text(
        render("city.html", {"__TITLE__": html.escape(city["name"]), "__CONFIG__": config_json(config)}),
        encoding="utf-8",
    )

    center = city["boundary"].representative_point()
    return {**config, "center": [round(center.x, 6), round(center.y, 6)]}


def build(data_dir, out_dir, max_bytes=MAX_GITHUB_FILE_BYTES, reuse_pmtiles=False):
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries, index_rows = [], []
    for city_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()) if data_dir.exists() else []:
        city = load_city(city_dir)
        if city is None:
            print(f"   {city_dir.name}: no data yet, skipped")
            continue
        entry = build_city(city, out_dir, max_bytes=max_bytes, reuse_pmtiles=reuse_pmtiles)
        entries.append(entry)
        index_rows.append({
            "slug": entry["slug"],
            "name": entry["name"],
            "geometry": city["boundary"].simplify(INDEX_BOUNDARY_TOLERANCE, preserve_topology=True),
        })
        print(f"🗺️  {entry['slug']}: {entry['stats']['images']} images")

    # remove the maps of cities that no longer have data
    built = {e["slug"] for e in entries}
    for folder in out_dir.iterdir():
        if folder.is_dir() and (folder / GENERATED_MARKER).exists() and folder.name not in built:
            shutil.rmtree(folder)
            print(f"   {folder.name}: removed (no data anymore)")

    assets = out_dir / "assets"
    assets.mkdir(exist_ok=True)
    for name in ("map.js", "style.css"):
        shutil.copyfile(TEMPLATES / name, assets / name)

    write_json(entries, out_dir / "cities.json")
    if index_rows:
        if not (reuse_pmtiles and pmtiles_parts(out_dir / "cities.pmtiles")):
            cities_gdf = gpd.GeoDataFrame(index_rows, geometry="geometry", crs="EPSG:4326")
            write_pmtiles_sliced(
                cities_gdf,
                out_dir / "cities.pmtiles",
                layer="cities",
                minzoom=0,
                maxzoom=10,
                max_bytes=max_bytes,
            )
    else:
        (out_dir / "cities.pmtiles").unlink(missing_ok=True)
        for old_p in out_dir.glob("cities.part*.pmtiles"):
            old_p.unlink(missing_ok=True)
        (out_dir / "cities.geojson").unlink(missing_ok=True)

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (out_dir / "index.html").write_text(render("index.html", {"__UPDATED__": updated}), encoding="utf-8")
    print(f"✅ {len(entries)} city maps and the index written to {out_dir}")
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"), help="Fetched data (default: data/)")
    parser.add_argument("--out", default=str(REPO_ROOT / "maps"), help="Output folder of the site (default: maps/)")
    parser.add_argument(
        "--reuse-pmtiles",
        action="store_true",
        help="Reuse existing .pmtiles files in maps/ if present instead of re-encoding them",
    )
    args = parser.parse_args(argv)
    build(args.data_dir, args.out, reuse_pmtiles=args.reuse_pmtiles)


if __name__ == "__main__":
    main()
