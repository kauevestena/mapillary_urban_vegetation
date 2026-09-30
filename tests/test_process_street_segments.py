"""
Tests for scripts/process_street_segments.py (unit tests with mock data, no network).
"""

import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import process_street_segments as pss  # noqa: E402


def test_slugify():
    assert pss.slugify("Palotina, Parana, Brazil") == "palotina-parana-brazil"
    assert pss.slugify("São Paulo, Brazil") == "sao-paulo-brazil"
    assert pss.slugify("curitiba") == "curitiba"


def test_densify_segment_points_length_and_spacing():
    # 10m line in projected CRS
    line = LineString([(0, 0), (10, 0)])
    df = gpd.GeoDataFrame({"segment_id": [0], "geometry": [line]}, crs="EPSG:3857")

    coords, seg_ids = pss.densify_segment_points(df, step=1.0)
    assert len(coords) == 10
    assert len(seg_ids) == 10
    assert np.all(seg_ids == 0)

    # Coordinates should be spaced at (0.5, 0), (1.5, 0), ..., (9.5, 0)
    expected_x = np.arange(10) + 0.5
    assert np.allclose(coords[:, 0], expected_x)
    assert np.allclose(coords[:, 1], 0.0)


def test_densify_segment_points_short_segment():
    # 0.4m line with step 1.0m -> should produce 1 point at midpoint (0.2, 0)
    line = LineString([(0, 0), (0.4, 0)])
    df = gpd.GeoDataFrame({"segment_id": [1], "geometry": [line]}, crs="EPSG:3857")

    coords, seg_ids = pss.densify_segment_points(df, step=1.0)
    assert len(coords) == 1
    assert np.allclose(coords[0], [0.2, 0.0])
    assert seg_ids[0] == 1


def test_densify_adjacent_segments_no_collision_at_intersection():
    # Two perpendicular segments meeting at (0, 0)
    line1 = LineString([(0, 0), (5, 0)])
    line2 = LineString([(0, 0), (0, 5)])
    df = gpd.GeoDataFrame({"segment_id": [10, 20], "geometry": [line1, line2]}, crs="EPSG:3857")

    coords, seg_ids = pss.densify_segment_points(df, step=1.0)
    assert len(coords) == 10  # 5 points each
    # Verify no coordinates are placed at (0, 0)
    assert not any(np.allclose(c, [0.0, 0.0]) for c in coords)
    # Verify all coordinates are unique
    assert len(np.unique(np.round(coords, 3), axis=0)) == 10


def test_compute_segment_statistics():
    # 1. Empty data
    empty = pss.compute_segment_statistics(pd.Series([], dtype=float))
    assert empty["count"] == 0
    assert empty["veg_median"] is None
    assert empty["veg_mean"] is None
    assert empty["h_median"] is None

    # 2. Single item with default min_photos=5 -> considered no data
    single_default = pss.compute_segment_statistics(pd.Series([25.4]), pd.Series([920.5]))
    assert single_default["count"] == 1
    assert single_default["veg_median"] is None
    assert single_default["veg_mean"] is None

    # Single item with min_photos=1
    single = pss.compute_segment_statistics(pd.Series([25.4]), pd.Series([920.5]), min_photos=1)
    assert single["count"] == 1
    assert single["veg_median"] == 25.4
    assert single["veg_mean"] == 25.4
    assert single["veg_min"] == 25.4
    assert single["veg_max"] == 25.4
    assert single["veg_std"] == 0.0
    assert single["veg_skew"] == 0.0
    assert single["veg_kurt"] == 0.0
    assert single["veg_mode"] == 25.4
    assert single["veg_iqr"] == 0.0
    assert single["h_median"] == 920.5

    # 3. Multiple items (>= 5) with known distributions
    vals = pd.Series([10.0, 20.0, 20.0, 30.0, 40.0])
    heights = pd.Series([100.0, 105.0, 110.0, 115.0, 120.0])
    stats = pss.compute_segment_statistics(vals, heights)

    assert stats["count"] == 5
    assert stats["veg_median"] == 20.0
    assert stats["veg_mean"] == 24.0
    assert stats["veg_min"] == 10.0
    assert stats["veg_max"] == 40.0
    assert stats["veg_mode"] == 20.0
    assert stats["veg_q1"] == 20.0
    assert stats["veg_q3"] == 30.0
    assert stats["veg_iqr"] == 10.0
    assert stats["h_median"] == 110.0
    assert stats["pct_high_veg"] == 40.0  # 30.0 and 40.0 are >= 30%


def test_generate_line_voronoi_and_attribution_pipeline(tmp_path):
    # Create two parallel street segments in local coordinates (e.g. Parana EPSG:32722)
    # Using EPSG:4326 input coordinates
    # ~ -53.84, -24.29
    s1 = LineString([(-53.840, -24.290), (-53.840, -24.288)])
    s2 = LineString([(-53.838, -24.290), (-53.838, -24.288)])
    segments = gpd.GeoDataFrame({"segment_id": [0, 1], "name": ["Street A", "Street B"], "geometry": [s1, s2]}, crs="EPSG:4326")

    boundary = box(-53.845, -24.295, -53.835, -24.285)

    # Generate Voronoi with step 5 meters (for quick unit test)
    voronoi, utm_crs = pss.generate_line_voronoi(segments, boundary, step=5.0)

    assert len(voronoi) == 2
    assert set(voronoi["segment_id"]) == {0, 1}
    assert all(voronoi.geom_type.isin(["Polygon", "MultiPolygon"]))

    # Synthetic Mapillary points:
    # 3 points near Street A (lon ~ -53.840)
    # 0 points near Street B
    pts = [
        Point(-53.8401, -24.2890),
        Point(-53.8400, -24.2891),
        Point(-53.8399, -24.2892),
    ]
    points_gdf = gpd.GeoDataFrame(
        {
            "id": ["p1", "p2", "p3"],
            "vegetation_percent": [15.0, 25.0, 35.0],
            "h": [300.0, 305.0, 310.0],
            "geometry": pts,
        },
        crs="EPSG:4326",
    )

    # 1. With default min_photos=5, 3 points is considered no data for stats
    segs_default, _ = pss.attribute_vegetation_statistics(segments.copy(), voronoi.copy(), points_gdf, utm_crs)
    row0_def = segs_default[segs_default["segment_id"] == 0].iloc[0]
    assert row0_def["count"] == 3
    assert pd.isna(row0_def["veg_median"])

    # 2. With min_photos=3, stats are computed
    segs_attr, vor_attr = pss.attribute_vegetation_statistics(segments, voronoi, points_gdf, utm_crs, min_photos=3)

    assert len(segs_attr) == 2
    row0 = segs_attr[segs_attr["segment_id"] == 0].iloc[0]
    row1 = segs_attr[segs_attr["segment_id"] == 1].iloc[0]

    # Street A should have 3 points
    assert row0["count"] == 3
    assert row0["veg_median"] == 25.0
    assert row0["veg_mean"] == 25.0
    assert row0["h_median"] == 305.0
    assert row0["length_m"] > 0
    assert row0["image_density"] > 0

    # Street B should have 0 points
    assert row1["count"] == 0
    assert pd.isna(row1["veg_median"])
    assert row1["image_density"] == 0.0

    # Check that voronoi also inherited the stats
    vor0 = vor_attr[vor_attr["segment_id"] == 0].iloc[0]
    assert vor0["count"] == 3
    assert vor0["veg_median"] == 25.0


def test_export_geojson(tmp_path):
    s = LineString([(0, 0), (1, 1)])
    gdf = gpd.GeoDataFrame({"segment_id": [1], "geometry": [s]}, crs="EPSG:4326")
    out_file = tmp_path / "test.geojson"
    pss.export_geojson(gdf, out_file, tolerance=0.0)

    assert out_file.exists()
    loaded = gpd.read_file(out_file)
    assert len(loaded) == 1
    assert loaded["segment_id"].iloc[0] == 1
