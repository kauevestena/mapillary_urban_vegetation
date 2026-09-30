"""
Process street segments and compute vegetation statistics via Line Voronoi.

Workflow:
1. Downloads the full road network of a city using OSMnx and extracts street
   segments as undirected edges between intersections.
2. Serializes the street segments to data/<slug>/segments.parquet (GeoParquet, EPSG:4326).
3. Generates Line Voronoi diagrams for each segment:
   - Samples points every x meters (default: 1.0 m) along each segment in a local UTM projection.
   - Computes a point Voronoi diagram on all sampled points across the city.
   - Dissolves Voronoi cells by segment ID using coverage_union_all, producing line Voronoi polygons.
   - Clips the Voronoi polygons to the city boundary.
   - Serializes data/<slug>/voronoi.parquet.
4. Performs a spatial join with all currently completed Mapillary points in data/<slug>/tiles/*.parquet.
5. Computes comprehensive descriptive statistics for each segment (median, mean, min,
   max, std, skewness, kurtosis, mode, IQR, Q1, Q3, median height, etc.).
6. Joins these statistics to the segments and overwrites data/<slug>/segments.parquet (and updates voronoi.parquet).
7. Optionally exports webmap-ready GeoJSON layers to maps/<slug>/.

Usage:
    python scripts/process_street_segments.py "Palotina, Parana, Brazil"
    python scripts/process_street_segments.py palotina-parana-brazil --step 1.0
    python scripts/process_street_segments.py palotina-parana-brazil --export-webmap
    python scripts/process_street_segments.py --all
"""

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
import shapely
from shapely.geometry import MultiPoint, box, mapping, shape

REPO_ROOT = Path(__file__).resolve().parent.parent


def slugify(text):
    """Normalize a place name into a filesystem-safe folder slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _round(value, digits=2):
    if value is None or (isinstance(value, (float, np.floating)) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(float(value), digits)


def load_city_boundary(city_dir):
    """Load boundary polygon from data/<slug>/boundary.geojson."""
    boundary_path = city_dir / "boundary.geojson"
    if not boundary_path.exists():
        raise FileNotFoundError(f"Boundary file not found at {boundary_path}")
    with open(boundary_path, encoding="utf-8") as f:
        data = json.load(f)
    feature = data["features"][0]
    return shape(feature["geometry"]), feature["properties"]


def fetch_street_network(boundary_geom, network_type="all"):
    """
    Download road network using OSMnx and extract undirected street segments.
    Each segment represents a physical street block between junctions.
    """
    # Truncate edges crossing the boundary to stay inside the city
    G = ox.graph_from_polygon(boundary_geom, network_type=network_type, retain_all=True, truncate_by_edge=True)
    if len(G.edges) == 0:
        raise ValueError("No road segments found inside the provided boundary.")

    Gu = ox.convert.to_undirected(G)
    edges = ox.convert.graph_to_gdfs(Gu, nodes=False, edges=True)

    # Standardize geometries to LineStrings
    # Filter out empty or non-LineString geometries
    edges = edges[edges.geometry.notnull() & ~edges.geometry.is_empty].copy()
    edges = edges.explode(ignore_index=True)
    edges = edges[edges.geom_type == "LineString"].copy()
    # Filter out zero-length lines without triggering geographic CRS warning
    edges = edges[edges.geometry.apply(lambda g: len(set(g.coords)) > 1)].copy()

    # Reset index and assign sequential segment_id
    edges = edges.reset_index(drop=True)
    edges["segment_id"] = np.arange(len(edges), dtype=int)

    # Clean and simplify metadata columns
    keep_attrs = ["segment_id", "name", "highway", "maxspeed", "lanes", "surface", "oneway", "geometry"]
    available_attrs = [c for c in keep_attrs if c in edges.columns]
    segments = edges[available_attrs].copy()

    # Flatten list attributes if OSM returned list for multiple tag values
    for col in ["name", "highway", "maxspeed", "lanes", "surface"]:
        if col in segments.columns:
            segments[col] = segments[col].apply(
                lambda v: ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else (str(v) if pd.notna(v) else None)
            )

    return segments


def densify_segment_points(segments_utm, step=1.0):
    """
    Sample points every `step` meters along each segment in UTM coordinates.
    Points are positioned at interval centers (i + 0.5) * step to ensure
    end nodes at street intersections never produce duplicate coordinates.
    """
    all_coords = []
    all_segment_ids = []

    for seg_id, line in zip(segments_utm["segment_id"], segments_utm.geometry):
        length = line.length
        if length <= step:
            pt = line.interpolate(length / 2.0)
            all_coords.append((pt.x, pt.y))
            all_segment_ids.append(seg_id)
        else:
            n_steps = int(math.floor(length / step))
            dists = (np.arange(n_steps, dtype=float) + 0.5) * step
            for d in dists:
                pt = line.interpolate(d)
                all_coords.append((pt.x, pt.y))
                all_segment_ids.append(seg_id)

    coords_arr = np.array(all_coords, dtype=np.float64)
    seg_ids_arr = np.array(all_segment_ids, dtype=int)

    # Defensive check against duplicate coordinates (e.g. self-intersecting or overlapping lines)
    # Round to millimeter resolution
    coords_rounded = np.round(coords_arr, 3)
    _, unique_indices = np.unique(coords_rounded, axis=0, return_index=True)
    if len(unique_indices) < len(coords_arr):
        unique_indices = np.sort(unique_indices)
        coords_arr = coords_arr[unique_indices]
        seg_ids_arr = seg_ids_arr[unique_indices]

    return coords_arr, seg_ids_arr


def safe_union_cells(cell_list):
    """Robustly union a list of Voronoi polygon cells handling GEOS topology edge cases."""
    if len(cell_list) == 1:
        return cell_list[0]
    try:
        return shapely.coverage_union_all(cell_list)
    except Exception:
        pass
    try:
        return shapely.unary_union(cell_list)
    except Exception:
        pass
    try:
        # Buffer by 1 cm to cleanly merge micro-slivers, then union
        return shapely.unary_union([c.buffer(0.01) for c in cell_list])
    except Exception:
        return shapely.unary_union([shapely.make_valid(c.buffer(0.01)) for c in cell_list])


def generate_line_voronoi(segments_gdf, boundary_geom, step=1.0):
    """
    Generate Line Voronoi polygons for each street segment:
    1. Project to local metric UTM.
    2. Densify points every `step` meters with centered spacing.
    3. Compute point Voronoi diagram with GEOS ordered=True.
    4. Group cells by segment_id and dissolve with safe_union_cells.
    5. Clip to city boundary.
    """
    utm_crs = segments_gdf.estimate_utm_crs()
    segments_utm = segments_gdf.to_crs(utm_crs)

    t0 = time.time()
    coords, seg_ids = densify_segment_points(segments_utm, step=step)
    t_densify = time.time() - t0

    t1 = time.time()
    mp = MultiPoint(coords)
    # ordered=True preserves 1-to-1 index alignment with input points
    vor_res = shapely.voronoi_polygons(mp, ordered=True)
    t_voronoi = time.time() - t1

    t2 = time.time()
    # Group Voronoi polygon cells by segment_id
    cells = np.array(vor_res.geoms)
    df_cells = pd.DataFrame({"segment_id": seg_ids, "cell": cells})
    groups = df_cells.groupby("segment_id")["cell"].apply(list)

    dissolved_dict = {}
    for seg_id, cell_list in groups.items():
        dissolved_dict[seg_id] = safe_union_cells(cell_list)

    voronoi_utm = gpd.GeoDataFrame(
        {"segment_id": list(dissolved_dict.keys()), "geometry": list(dissolved_dict.values())},
        crs=utm_crs,
    )
    t_dissolve = time.time() - t2

    # Clip Voronoi polygons to boundary polygon
    boundary_utm = gpd.GeoSeries([boundary_geom], crs="EPSG:4326").to_crs(utm_crs).iloc[0]
    voronoi_clipped = voronoi_utm.clip(boundary_utm)
    voronoi_clipped = voronoi_clipped[voronoi_clipped.geometry.notnull() & ~voronoi_clipped.geometry.is_empty].copy()

    # Reproject back to EPSG:4326
    voronoi_4326 = voronoi_clipped.to_crs("EPSG:4326")

    print(
        f"   Voronoi generation ({len(coords):,} points): densify {t_densify:.2f}s, "
        f"tessellate {t_voronoi:.2f}s, dissolve {t_dissolve:.2f}s"
    )
    return voronoi_4326, utm_crs


def load_mapillary_points(city_dir):
    """Load all currently available Mapillary points from completed tiles."""
    tiles_dir = city_dir / "tiles"
    if not tiles_dir.exists():
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    parquet_files = sorted(tiles_dir.glob("*.parquet"))
    if not parquet_files:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    frames = [gpd.read_parquet(p) for p in parquet_files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    combined = pd.concat(frames, ignore_index=True)
    if "id" in combined.columns:
        combined = combined.drop_duplicates(subset="id")
    return gpd.GeoDataFrame(combined, geometry="geometry", crs="EPSG:4326")


def compute_segment_statistics(series_veg, series_h=None):
    """Compute descriptive statistics for a segment's points."""
    veg = series_veg.dropna()
    n = len(veg)
    if n == 0:
        return {
            "count": 0,
            "veg_median": None,
            "veg_mean": None,
            "veg_min": None,
            "veg_max": None,
            "veg_std": None,
            "veg_skew": None,
            "veg_kurt": None,
            "veg_mode": None,
            "veg_q1": None,
            "veg_q3": None,
            "veg_iqr": None,
            "veg_mad": None,
            "pct_high_veg": None,
            "h_median": None,
            "h_mean": None,
        }

    med = float(veg.median())
    q1 = float(np.percentile(veg, 25))
    q3 = float(np.percentile(veg, 75))
    mad = float(np.median(np.abs(veg - med)))

    # Mode rounded to 1 decimal
    rounded_veg = veg.round(1)
    mode_series = rounded_veg.mode()
    mode_val = float(mode_series.iloc[0]) if not mode_series.empty else None

    # Heights
    h_med, h_mean = None, None
    if series_h is not None:
        h_valid = series_h.dropna()
        if not h_valid.empty:
            h_med = float(h_valid.median())
            h_mean = float(h_valid.mean())

    return {
        "count": n,
        "veg_median": _round(med, 2),
        "veg_mean": _round(veg.mean(), 2),
        "veg_min": _round(veg.min(), 2),
        "veg_max": _round(veg.max(), 2),
        "veg_std": _round(veg.std(ddof=1), 2) if n > 1 else 0.0,
        "veg_skew": _round(veg.skew(), 3) if n > 2 else 0.0,
        "veg_kurt": _round(veg.kurtosis(), 3) if n > 3 else 0.0,
        "veg_mode": _round(mode_val, 1),
        "veg_q1": _round(q1, 2),
        "veg_q3": _round(q3, 2),
        "veg_iqr": _round(q3 - q1, 2),
        "veg_mad": _round(mad, 2),
        "pct_high_veg": _round((veg >= 30.0).mean() * 100.0, 1),
        "h_median": _round(h_med, 1),
        "h_mean": _round(h_mean, 1),
    }


def attribute_vegetation_statistics(segments_gdf, voronoi_gdf, points_gdf, utm_crs):
    """
    Match Mapillary points into each segment Voronoi polygon and compute statistics.
    Joins the statistics back to both segments_gdf and voronoi_gdf.
    """
    # Calculate metric length
    segments_utm = segments_gdf.to_crs(utm_crs)
    segments_gdf["length_m"] = segments_utm.geometry.length.round(1)

    if points_gdf.empty or voronoi_gdf.empty:
        # No points yet (incomplete / initial run)
        empty_stats = compute_segment_statistics(pd.Series([], dtype=float))
        for key, val in empty_stats.items():
            segments_gdf[key] = val
            voronoi_gdf[key] = val
        segments_gdf["image_density"] = 0.0
        voronoi_gdf["image_density"] = 0.0
        return segments_gdf, voronoi_gdf

    points_utm = points_gdf.to_crs(utm_crs)
    voronoi_utm = voronoi_gdf.to_crs(utm_crs)

    # Point-in-polygon spatial join
    cols_to_join = ["id", "vegetation_percent", "geometry"]
    if "h" in points_utm.columns:
        cols_to_join.append("h")

    joined = gpd.sjoin(
        points_utm[cols_to_join],
        voronoi_utm[["segment_id", "geometry"]],
        how="inner",
        predicate="intersects",
    )
    if "id" in joined.columns:
        joined = joined.drop_duplicates(subset="id")

    stats_list = []
    by_segment = joined.groupby("segment_id") if not joined.empty else []

    all_segment_ids = set(segments_gdf["segment_id"])
    matched_ids = set()

    for seg_id, group in by_segment:
        matched_ids.add(seg_id)
        stat = compute_segment_statistics(group["vegetation_percent"], group.get("h"))
        stat["segment_id"] = seg_id
        stats_list.append(stat)

    # Add empty stats for segments without points
    empty_template = compute_segment_statistics(pd.Series([], dtype=float))
    for seg_id in all_segment_ids - matched_ids:
        stat = dict(empty_template)
        stat["segment_id"] = seg_id
        stats_list.append(stat)

    stats_df = pd.DataFrame(stats_list)

    # Merge stats back onto segments and voronoi
    # Drop existing stat columns if re-running
    stat_keys = [k for k in empty_template.keys() if k != "segment_id"] + ["image_density"]
    for col in stat_keys:
        if col in segments_gdf.columns:
            segments_gdf = segments_gdf.drop(columns=[col])
        if col in voronoi_gdf.columns:
            voronoi_gdf = voronoi_gdf.drop(columns=[col])

    merged_segs = segments_gdf.merge(stats_df, on="segment_id", how="left")
    merged_segs["count"] = merged_segs["count"].fillna(0).astype(int)
    density = (merged_segs["count"] / (merged_segs["length_m"] / 100.0)).replace([np.inf, -np.inf], 0.0).fillna(0.0)
    merged_segs["image_density"] = density.round(2)
    segments_gdf = gpd.GeoDataFrame(merged_segs, geometry="geometry", crs=segments_gdf.crs)

    merged_vor = voronoi_gdf.merge(stats_df, on="segment_id", how="left")
    merged_vor["count"] = merged_vor["count"].fillna(0).astype(int)
    merged_vor["image_density"] = merged_segs["image_density"]
    voronoi_gdf = gpd.GeoDataFrame(merged_vor, geometry="geometry", crs=voronoi_gdf.crs)

    return segments_gdf, voronoi_gdf


def export_geojson(gdf, out_path, tolerance=0.00002):
    """Export GeoDataFrame as a lightweight GeoJSON file with simplified geometries."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    simplified = gdf.copy()
    if tolerance > 0:
        simplified["geometry"] = simplified.geometry.simplify(tolerance, preserve_topology=True)

    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(simplified.to_json(drop_id=True))
    os.replace(tmp_path, out_path)


def process_city(
    city_dir,
    step=1.0,
    network_type="all",
    rebuild_network=False,
    export_webmap=False,
    maps_dir=None,
    tolerance_segments=0.00002,
    tolerance_voronoi=0.00005,
):
    """Full execution pipeline for a single city folder."""
    city_dir = Path(city_dir)
    print(f"\n🏙️  Processing city: {city_dir.name}")

    boundary_geom, boundary_props = load_city_boundary(city_dir)
    segments_path = city_dir / "segments.parquet"
    voronoi_path = city_dir / "voronoi.parquet"

    # Step 1: Road network & street segments
    if segments_path.exists() and voronoi_path.exists() and not rebuild_network:
        print(f"   Loading existing road segments from {segments_path.name}")
        segments = gpd.read_parquet(segments_path)
        voronoi = gpd.read_parquet(voronoi_path)
        utm_crs = segments.estimate_utm_crs()
    else:
        print(f"   Downloading OSM road network ({network_type})...")
        segments = fetch_street_network(boundary_geom, network_type=network_type)
        print(f"   Extracted {len(segments):,} physical street segments.")

        # Save initial segments parquet
        segments.to_parquet(segments_path, index=False)
        print(f"   Saved initial {segments_path.name}")

        # Step 2: Line Voronoi synthesis
        print(f"   Generating Line Voronoi polygons (step={step}m)...")
        voronoi, utm_crs = generate_line_voronoi(segments, boundary_geom, step=step)
        voronoi.to_parquet(voronoi_path, index=False)
        print(f"   Saved initial {voronoi_path.name} ({len(voronoi):,} polygons)")

    # Step 3: Load Mapillary data points
    points = load_mapillary_points(city_dir)
    print(f"   Loaded {len(points):,} Mapillary images from completed tiles.")

    # Step 4: Attribute descriptive statistics
    print("   Computing descriptive statistics for segments...")
    segments, voronoi = attribute_vegetation_statistics(segments, voronoi, points, utm_crs)

    # Step 5: Overwrite segments.parquet and voronoi.parquet
    segments.to_parquet(segments_path, index=False)
    voronoi.to_parquet(voronoi_path, index=False)
    print(f"✅ Overwrote {segments_path.name} with joined descriptive statistics.")
    print(f"✅ Overwrote {voronoi_path.name} with joined descriptive statistics.")

    # Step 6: Optional webmap GeoJSON export
    if export_webmap:
        if maps_dir is None:
            maps_dir = REPO_ROOT / "maps"
        city_maps_dir = Path(maps_dir) / city_dir.name
        city_maps_dir.mkdir(parents=True, exist_ok=True)

        print(f"   Exporting webmap layers to {city_maps_dir}...")
        export_geojson(segments, city_maps_dir / "segments.geojson", tolerance=tolerance_segments)
        export_geojson(voronoi, city_maps_dir / "voronoi.geojson", tolerance=tolerance_voronoi)
        print(f"✅ Webmap layers exported to {city_maps_dir}")

    return {
        "slug": city_dir.name,
        "segments": len(segments),
        "voronoi": len(voronoi),
        "points": len(points),
        "surveyed_segments": int((segments["count"] > 0).sum()),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "place_or_slug",
        nargs="?",
        default=None,
        help="City name or folder slug in data/ (e.g. 'Palotina, Parana, Brazil' or 'palotina-parana-brazil')",
    )
    parser.add_argument(
        "--data-dir",
        default=str(REPO_ROOT / "data"),
        help="Directory containing city data folders (default: data/)",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=1.0,
        help="Point sampling interval along street segments in meters (default: 1.0)",
    )
    parser.add_argument(
        "--network-type",
        default="all",
        help="OSMnx network type: 'all', 'drive', 'bike', 'walk' (default: all)",
    )
    parser.add_argument(
        "--rebuild-network",
        action="store_true",
        help="Force re-downloading OSM road network and re-generating Voronoi",
    )
    parser.add_argument(
        "--export-webmap",
        action="store_true",
        help="Export segments.geojson and voronoi.geojson to maps/<slug>/",
    )
    parser.add_argument(
        "--maps-dir",
        default=str(REPO_ROOT / "maps"),
        help="Output maps directory (default: maps/)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="process_all",
        help="Process all cities found in data/",
    )

    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    if args.process_all:
        city_dirs = sorted([d for d in data_dir.iterdir() if d.is_dir() and (d / "boundary.geojson").exists()])
        if not city_dirs:
            print(f"No city directories with boundary.geojson found in {data_dir}")
            return
        for cdir in city_dirs:
            process_city(
                cdir,
                step=args.step,
                network_type=args.network_type,
                rebuild_network=args.rebuild_network,
                export_webmap=args.export_webmap,
                maps_dir=args.maps_dir,
            )
    else:
        if not args.place_or_slug:
            parser.error("Specify place_or_slug or use --all")

        # Resolve slug
        slug = slugify(args.place_or_slug)
        city_dir = data_dir / slug
        if not city_dir.exists():
            # Check if an exact match exists directly
            city_dir = data_dir / args.place_or_slug

        if not city_dir.exists() or not (city_dir / "boundary.geojson").exists():
            sys.exit(f"Error: {city_dir} does not exist or has no boundary.geojson. Run fetch_city_vegetation.py first.")

        process_city(
            city_dir,
            step=args.step,
            network_type=args.network_type,
            rebuild_network=args.rebuild_network,
            export_webmap=args.export_webmap,
            maps_dir=args.maps_dir,
        )


if __name__ == "__main__":
    main()
