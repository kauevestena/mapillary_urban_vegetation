"""
Tests of scripts/build_webmaps.py on fake city folders (no network).
"""

import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box, mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_webmaps as bw  # noqa: E402


def make_city(data_dir, slug, rows, display_name="Test City, Somewhere, Country"):
    city = data_dir / slug
    (city / "tiles").mkdir(parents=True)
    boundary = box(-49.30, -25.50, -49.20, -25.40)
    (city / "boundary.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"place": slug, "display_name": display_name},
                      "geometry": mapping(boundary)}],
    }))
    (city / "progress.json").write_text(json.dumps({
        "updated_at": "2026-09-28T12:00:00Z",
        "summary": {"big_tiles": 4, "completed": 1, "in_progress": 0, "not_started": 3},
    }))
    if rows is not None:
        df = pd.DataFrame(rows)
        df["captured_at"] = pd.to_datetime(df["captured_at"], unit="ms", utc=True)
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")
        gdf.to_parquet(city / "tiles" / "14_1_1.parquet", index=False)
    return city


ROWS = [
    {"id": "1", "captured_at": 1749300000000, "lon": -49.25, "lat": -25.45, "h": 931.04, "h_computed": 1.0,
     "vegetation_percent": 12.345, "segmented_percent": 100.0, "number_available_classes": 20},
    {"id": "2", "captured_at": 1749400000000, "lon": -49.24, "lat": -25.44, "h": float("nan"), "h_computed": 2.0,
     "vegetation_percent": 40.0, "segmented_percent": 95.0, "number_available_classes": 25},
]


def test_build(tmp_path, capsys):
    data, out = tmp_path / "data", tmp_path / "maps"
    make_city(data, "test-city", ROWS)
    make_city(data, "empty-city", None)  # boundary only, no tile yet

    entries = bw.build(data, out)

    assert [e["slug"] for e in entries] == ["test-city"]
    assert not (out / "empty-city").exists()
    assert "empty-city: no data yet" in capsys.readouterr().out

    city = out / "test-city"
    points = json.loads((city / "points.geojson").read_text())
    assert len(points["features"]) == 2
    first = points["features"][0]
    assert first["properties"] == {"id": "1", "veg": 12.3, "date": "2025-06-07", "h": 931.0}
    assert points["features"][1]["properties"]["h"] is None  # NaN height
    assert json.loads((city / "boundary.geojson").read_text())["features"][0]["geometry"]["type"] == "Polygon"

    page = (city / "index.html").read_text()
    assert "<title>Test City · Urban vegetation</title>" in page
    assert "../assets/map.js" in page and "initCityMap" in page
    config = json.loads(page.split('id="config">')[1].split("</script>")[0])
    assert config["stats"] == {
        "images": 2, "vegetation_median": 26.2, "vegetation_mean": 26.2,
        "date_min": "2025-06-07", "date_max": "2025-06-08",
        "tiles_completed": 1, "tiles_total": 4, "updated": "2026-09-28",
    }

    cities = json.loads((out / "cities.json").read_text())
    assert [c["name"] for c in cities] == ["Test City"] and len(cities[0]["center"]) == 2
    index_boundaries = json.loads((out / "cities.geojson").read_text())
    assert index_boundaries["features"][0]["properties"]["slug"] == "test-city"
    assert "initIndexMap" in (out / "index.html").read_text()

    script = (out / "assets" / "map.js").read_text()
    for style in ("styles/positron", "styles/dark", "styles/liberty"):
        assert f"https://tiles.openfreemap.org/{style}" in script
    assert (out / "assets" / "style.css").exists()


def test_stale_generated_maps_are_removed(tmp_path):
    data, out = tmp_path / "data", tmp_path / "maps"
    city = make_city(data, "test-city", ROWS)
    (out / "hand-made").mkdir(parents=True)  # not generated: kept
    bw.build(data, out)
    assert (out / "test-city" / "index.html").exists()

    for path in (city / "tiles").glob("*.parquet"):
        path.unlink()
    assert bw.build(data, out) == []
    assert not (out / "test-city").exists()
    assert (out / "hand-made").exists()
    assert json.loads((out / "cities.json").read_text()) == []


def test_city_name_is_escaped_in_the_title(tmp_path):
    data, out = tmp_path / "data", tmp_path / "maps"
    make_city(data, "odd", ROWS, display_name="<b>Odd</b> & Co, Place")
    bw.build(data, out)
    page = (out / "odd" / "index.html").read_text()
    assert "<title>&lt;b&gt;Odd&lt;/b&gt; &amp; Co · Urban vegetation</title>" in page
    assert "</b>" not in page.split('id="config">')[1].split("</script>")[0]  # no "</" in the JSON config


def test_build_with_segments_and_voronoi(tmp_path):
    from shapely.geometry import LineString, Polygon
    data, out = tmp_path / "data", tmp_path / "maps"
    city = make_city(data, "geo-city", ROWS)

    # Save mock segments.parquet and voronoi.parquet
    seg = gpd.GeoDataFrame({"segment_id": [1], "name": ["Main St"], "geometry": [LineString([(0, 0), (1, 1)])]}, crs="EPSG:4326")
    seg.to_parquet(city / "segments.parquet", index=False)

    vor = gpd.GeoDataFrame({"segment_id": [1], "geometry": [Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])]}, crs="EPSG:4326")
    vor.to_parquet(city / "voronoi.parquet", index=False)

    bw.build(data, out)

    city_out = out / "geo-city"
    assert (city_out / "segments.geojson").exists()
    assert (city_out / "voronoi.geojson").exists()
    assert (city_out / "points.geojson").exists()

    config = json.loads((city_out / "index.html").read_text().split('id="config">')[1].split("</script>")[0])
    assert config["has_segments"] is True
    assert config["has_voronoi"] is True
    assert config["files"] == {
        "points": ["points.geojson"],
        "segments": ["segments.geojson"],
        "voronoi": ["voronoi.geojson"],
    }


def test_build_with_sliced_parquet_and_sliced_geojson(tmp_path):
    from shapely.geometry import LineString, Polygon
    data, out = tmp_path / "data", tmp_path / "maps"
    many_rows = [
        {
            "id": str(i),
            "captured_at": 1749300000000 + i * 1000,
            "lon": -49.25 + i * 0.001,
            "lat": -25.45 + i * 0.001,
            "h": 900.0 + i,
            "h_computed": 1.0,
            "vegetation_percent": float(i * 5 % 50),
            "segmented_percent": 100.0,
            "number_available_classes": 20,
        }
        for i in range(10)
    ]
    city = make_city(data, "sliced-city", many_rows)

    # Save sliced voronoi.part000.parquet and voronoi.part001.parquet
    vor0 = gpd.GeoDataFrame({"segment_id": [1], "geometry": [Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])]}, crs="EPSG:4326")
    vor1 = gpd.GeoDataFrame({"segment_id": [2], "geometry": [Polygon([(1, 0), (2, 0), (2, 1), (1, 1), (1, 0)])]}, crs="EPSG:4326")
    vor0.to_parquet(city / "voronoi.part000.parquet", index=False)
    vor1.to_parquet(city / "voronoi.part001.parquet", index=False)

    seg = gpd.GeoDataFrame(
        {"segment_id": [1, 2], "name": ["St 1", "St 2"], "geometry": [LineString([(0, 0), (1, 1)]), LineString([(1, 1), (2, 2)])]},
        crs="EPSG:4326",
    )
    seg.to_parquet(city / "segments.parquet", index=False)

    # Force small max_bytes so points.geojson is sliced into multiple .partXXX.geojson files
    bw.build(data, out, max_bytes=400)

    city_out = out / "sliced-city"
    assert not (city_out / "points.geojson").exists()
    point_parts = sorted(city_out.glob("points.part*.geojson"))
    assert len(point_parts) >= 2

    config = json.loads((city_out / "index.html").read_text().split('id="config">')[1].split("</script>")[0])
    assert config["has_segments"] is True
    assert config["has_voronoi"] is True
    assert config["files"]["points"] == [p.name for p in point_parts]
    total_pts = sum(len(json.loads(p.read_text())["features"]) for p in point_parts)
    assert total_pts == 10
