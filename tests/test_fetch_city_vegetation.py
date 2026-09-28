"""
Offline tests of scripts/fetch_city_vegetation.py against fake Nominatim and
Mapillary APIs (no network, no token needed).
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import geopandas as gpd
import mapbox_vector_tile
import mercantile
import pandas as pd
import pytest
import requests
from shapely.geometry import MultiPolygon, box, mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_city_vegetation as fcv  # noqa: E402

TOKEN = "MLY|123|secret"
EXTENT = 4096
BIG_ZOOM, SMALL_ZOOM = 16, 18
BIG_A = mercantile.tile(-49.27, -25.43, BIG_ZOOM)
BIG_B = mercantile.Tile(BIG_A.x + 1, BIG_A.y, BIG_ZOOM)
INSET = 1e-7  # keep the polygon strictly inside the two big tiles
POLYGON = MultiPolygon([fcv.tile_box(t).buffer(-INSET) for t in (BIG_A, BIG_B)])

DENSE_TILE = next(iter(mercantile.children(BIG_A, zoom=SMALL_ZOOM)))  # more images than --limit
REFUSED_TILE = list(mercantile.children(BIG_B, zoom=SMALL_ZOOM))[5]  # "reduce the amount of data"
DEEP_TILE = list(mercantile.children(BIG_B, zoom=SMALL_ZOOM))[2]  # 8 images packed in ~9 m


# --- fake world ---------------------------------------------------------------

def encoded(*geoms):
    tile = mapbox_vector_tile.encode(
        [{"name": "mpy-or", "features": [{"geometry": g.wkt, "properties": {}} for g in geoms]}],
        default_options={"y_coord_down": True, "extents": EXTENT},
    )
    return base64.b64encode(tile).decode()


# a full segmentation: sky 25% + vegetation 25% + road 50% = 100% of the image
VEGETATION_DETECTIONS = [
    {"value": "nature--sky", "geometry": encoded(box(0, 0, EXTENT, EXTENT // 4))},
    {"value": "nature--vegetation", "geometry": encoded(box(0, EXTENT // 4, EXTENT, EXTENT // 2))},
    {"value": "construction--flat--road", "geometry": encoded(box(0, EXTENT // 2, EXTENT, EXTENT))},
]
# leftover detections of an old image: a road patch (10%) and a sign. It has a
# "surface" class, but is not a full segmentation
PARTIAL_DETECTIONS = [
    {"value": "construction--flat--road", "geometry": encoded(box(0, 0, EXTENT, EXTENT // 10))},
    {"value": "regulatory--stop--g1", "geometry": encoded(box(0, 0, 100, 100))},
]


def make_images():
    """Per small tile: one segmented image, one partially detected one; 5 segmented ones in the dense tile."""
    images = []
    for big in (BIG_A, BIG_B):
        for small in mercantile.children(big, zoom=SMALL_ZOOM):
            b = mercantile.bounds(small)
            count = {DENSE_TILE: 5, DEEP_TILE: 8}.get(small, 2)
            for n in range(count):
                if small == DEEP_TILE:  # 1e-5 degree steps along the diagonal
                    lon = b.west + 3e-4 + n * 1e-5
                    lat = b.south + 3e-4 + n * 1e-5
                else:
                    lon = b.west + (b.east - b.west) * (n + 1) / (count + 1)
                    lat = b.south + (b.north - b.south) * (n + 1) / (count + 1)
                kind = "sign" if (n == 1 and small not in (DENSE_TILE, DEEP_TILE)) else "veg"
                images.append({
                    "id": f"{kind}-{fcv.tile_key(small)}-{n}",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "computed_geometry": {"type": "Point", "coordinates": [lon + 1e-9, lat]},
                    "computed_altitude": 900.5,
                    "altitude": 901.0,
                    "captured_at": 1700000000000 + n,
                })
    return images


IMAGES = make_images()
SEGMENTED_IDS = {i["id"] for i in IMAGES if i["id"].startswith("veg")}


class Response:
    def __init__(self, payload, status_code=200):
        self.payload, self.status_code = payload, status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            kind = "Client" if self.status_code < 500 else "Server"
            raise requests.exceptions.HTTPError(
                f"{self.status_code} {kind} Error: x for url: https://graph.mapillary.com/images"
                f"?bbox=-49.4012,-25.4291,-49.4007,-25.4287&access_token={TOKEN}",
                response=self,
            )


CENTER = POLYGON.centroid

# the Nominatim results that broke the first real run: the city itself comes
# without a polygon, another administrative area with one
CITY_RELATION = {
    "osm_type": "relation", "osm_id": 42, "category": "boundary", "type": "administrative",
    "name": "Test City", "display_name": "Test City, Somewhere", "importance": 0.7,
    "lat": str(CENTER.y), "lon": str(CENTER.x),
}
OTHER_AREA = {
    "osm_type": "relation", "osm_id": 303895, "category": "boundary", "type": "administrative",
    "name": "Canindé de São Francisco", "display_name": "Canindé de São Francisco, Sergipe", "importance": 0.4,
    "lat": "-9.6", "lon": "-37.8", "geojson": mapping(box(-38.2, -9.8, -37.6, -9.4)),
}


class FakeAPI:
    def __init__(self):
        self.calls = {"nominatim": [], "osmfr": 0, "images": [], "detections": []}
        self.search_results = [OTHER_AREA, CITY_RELATION]
        self.lookup = {"R42": [{**CITY_RELATION, "geojson": mapping(POLYGON)}]}
        self.reverse = {**CITY_RELATION, "geojson": mapping(POLYGON)}
        self.osmfr = None  # polygons.openstreetmap.fr answer (None: HTTP 500)
        # Nominatim details: address hierarchy of the city node (None: HTTP 500)
        self.details = {"address": [
            {"localname": "Test City", "osm_type": "R", "osm_id": 42, "class": "boundary",
             "type": "administrative", "admin_level": 8},
            {"localname": "Região Metropolitana de Test City", "osm_type": "R", "osm_id": 5,
             "class": "boundary", "type": "administrative", "admin_level": 6},
            {"localname": "Test City", "osm_type": "N", "osm_id": 7, "class": "place", "type": "city"},
        ]}
        self.overpass_down_first = False
        # Overpass: administrative relations around a point
        self.overpass = [
            {"type": "relation", "id": 5, "tags": {"name": "Região Metropolitana de Test City", "admin_level": "6"}},
            {"type": "relation", "id": 42, "tags": {"name": "Test City", "admin_level": "8"}},
            {"type": "relation", "id": 1, "tags": {"name": "Somewhere", "admin_level": "2"}},
        ]

    def post(self, url, data=None, timeout=None, headers=None):
        assert url in fcv.OVERPASS_URLS and "is_in(" in data["data"]
        self.calls["nominatim"].append("overpass")
        if self.overpass is None or (self.overpass_down_first and url == fcv.OVERPASS_URLS[0]):
            return Response("error", 504)
        return Response({"elements": self.overpass})

    def __call__(self, url, params=None, timeout=None, headers=None):
        if url.startswith(fcv.NOMINATIM_URL):
            assert headers and "User-Agent" in headers
            endpoint = url.rsplit("/", 1)[-1]
            self.calls["nominatim"].append(endpoint)
            if endpoint == "search":
                assert "polygon_geojson" not in params
                return Response(self.search_results)
            if endpoint == "lookup":
                found = self.lookup.get(params["osm_ids"])
                return Response(found) if found is not None else Response({"error": "down"}, 503)
            if endpoint == "reverse":
                return Response(self.reverse)
            if endpoint == "details":
                return Response(self.details) if self.details else Response({"error": "x"}, 500)
        if url.startswith(fcv.POLYGONS_OSM_FR_URL):
            self.calls["osmfr"] += 1
            return Response(self.osmfr) if self.osmfr else Response("error", 500)
        if url.endswith("/images"):
            self.calls["images"].append(params)
            assert params["access_token"] == TOKEN
            west, south, east, north = map(float, params["bbox"].split(","))
            assert west < east and south < north
            requested = mercantile.tile((west + east) / 2, (south + north) / 2, 18)
            if requested == REFUSED_TILE and (east - west) > 0.0009:
                return Response({"error": {"message": "Please reduce the amount of data you're asking for, then retry your request"}}, 500)
            found = [
                i for i in IMAGES
                if west <= i["geometry"]["coordinates"][0] <= east and south <= i["geometry"]["coordinates"][1] <= north
            ]
            return Response({"data": found[: int(params["limit"])]})
        if url.endswith("/detections"):
            self.calls["detections"].append(url)
            assert params["fields"] == "value,geometry"
            image_id = url.split("/")[-2]
            return Response({"data": VEGETATION_DETECTIONS if image_id.startswith("veg") else PARTIAL_DETECTIONS})
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(requests, "get", fake)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(fcv, "sleep", lambda s: None)
    monkeypatch.setattr(fcv, "STOP_REQUESTED", False)
    return fake


def make_args(data_dir, **overrides):
    args = dict(
        place="Test City, Somewhere", big_zoom=BIG_ZOOM, small_zoom=SMALL_ZOOM, limit=3,
        max_minutes=None, max_big_tiles=None, workers=4, data_dir=str(data_dir), slug=None,
        osm_relation=None, boundary_only=False, min_coverage=80.0, point=None,
    )
    args.update(overrides)
    return argparse.Namespace(**args)


def read_all_tiles(city_dir):
    frames = [gpd.read_parquet(p) for p in sorted((city_dir / "tiles").glob("*.parquet"))]
    return pd.concat(frames, ignore_index=True)


# --- tests --------------------------------------------------------------------

def test_slugify():
    assert fcv.slugify("Curitiba, Paraná, Brazil") == "curitiba-parana-brazil"


def test_boundary_picks_the_named_city_not_another_polygon(tmp_path, api):
    city_dir = tmp_path / "city"
    polygon = fcv.load_or_fetch_boundary("Test City, Somewhere", city_dir)
    assert polygon.equals(POLYGON)  # from the lookup of relation 42, not Canindé's inline polygon
    assert api.calls["nominatim"] == ["search", "lookup"]

    again = fcv.load_or_fetch_boundary("Test City, Somewhere", city_dir)
    assert again.equals(POLYGON) and len(api.calls["nominatim"]) == 2  # reused, no new request
    properties = json.loads((city_dir / "boundary.geojson").read_text())["features"][0]["properties"]
    assert properties["osm_id"] == 42 and properties["place"] == "Test City, Somewhere"


def test_boundary_name_matching_ignores_accents_and_case(api):
    api.search_results = [{**CITY_RELATION, "name": "Tést CITY"}]
    polygon, source = fcv.fetch_boundary("test city")
    assert polygon.equals(POLYGON) and source["osm_id"] == 42


CITY_NODE = {**CITY_RELATION, "osm_type": "node", "osm_id": 7, "category": "place", "type": "city"}


def test_boundary_of_a_city_node_comes_from_its_address_relation(api):
    # what Nominatim really returned for "Curitiba": the place=city node and Canindé's relation
    api.search_results = [CITY_NODE, OTHER_AREA]
    polygon, source = fcv.fetch_boundary("Test City")
    assert polygon.equals(POLYGON) and source["osm_id"] == 42
    assert api.calls["nominatim"] == ["search", "details", "lookup"]


def test_boundary_of_a_city_node_falls_back_to_overpass_mirrors(api):
    api.search_results = [CITY_NODE]
    api.details = None
    api.overpass_down_first = True  # first instance times out, as in the real run
    polygon, source = fcv.fetch_boundary("Test City")
    assert polygon.equals(POLYGON) and source["osm_id"] == 42
    assert api.calls["nominatim"] == ["search", "details", "overpass", "overpass", "lookup"]


def test_boundary_of_a_city_node_falls_back_to_reverse_geocoding(api):
    api.search_results = [CITY_NODE]
    api.details = None
    api.overpass = None  # all Overpass instances down
    polygon, source = fcv.fetch_boundary("Test City")
    assert polygon.equals(POLYGON) and api.calls["nominatim"][-1] == "reverse"

    # a reverse result that is just the node again (as in the real run) is refused
    api.reverse = {**CITY_NODE, "geojson": {"type": "Point", "coordinates": [CENTER.x, CENTER.y]}}
    with pytest.raises(ValueError, match="Could not get a polygon"):
        fcv.fetch_boundary("Test City")

    # and so is one with another name
    api.reverse = {**OTHER_AREA}
    with pytest.raises(ValueError, match="Could not get a polygon"):
        fcv.fetch_boundary("Test City")


def test_boundary_falls_back_to_polygons_openstreetmap_fr(api):
    api.lookup = {}
    api.osmfr = {"type": "GeometryCollection", "geometries": [mapping(POLYGON)]}
    polygon, source = fcv.fetch_boundary("Test City")
    assert polygon.equals(POLYGON) and api.calls["osmfr"] == 1


def test_boundary_errors(api):
    api.search_results = [OTHER_AREA]
    with pytest.raises(ValueError, match="No OpenStreetMap result is named 'Test City'"):
        fcv.fetch_boundary("Test City")

    # a polygon that doesn't contain the city's own location is refused
    api.search_results = [CITY_RELATION]
    api.lookup = {"R42": [{**CITY_RELATION, "geojson": OTHER_AREA["geojson"]}]}
    with pytest.raises(ValueError, match="Could not get a polygon"):
        fcv.fetch_boundary("Test City")


def test_osm_relation_skips_the_search(tmp_path, api):
    polygon = fcv.load_or_fetch_boundary("anything", tmp_path / "city", osm_relation=42)
    assert polygon.equals(POLYGON) and api.calls["nominatim"] == ["lookup"]


def test_cached_boundary_of_another_place_is_refused(tmp_path, api):
    city_dir = tmp_path / "city"
    fcv.load_or_fetch_boundary("Test City", city_dir)
    with pytest.raises(SystemExit, match="Delete"):
        fcv.load_or_fetch_boundary("Test City, Elsewhere", city_dir)
    with pytest.raises(SystemExit):
        fcv.load_or_fetch_boundary("Test City", city_dir, osm_relation=99)


def test_boundary_only(tmp_path, api, capsys):
    assert fcv.run(make_args(tmp_path, boundary_only=True), TOKEN) is None
    city_dir = tmp_path / "test-city-somewhere"
    assert (city_dir / "boundary.geojson").exists()
    assert not (city_dir / "progress.json").exists() and not (city_dir / "tiles").exists()
    assert "2 big tiles at zoom 16" in capsys.readouterr().out
    assert not api.calls["images"]


def test_tiling():
    assert set(fcv.big_tiles_for(POLYGON, BIG_ZOOM)) == {BIG_A, BIG_B}
    small = fcv.small_tiles_for(BIG_A, POLYGON, SMALL_ZOOM)
    assert len(small) == 16 and all(t.z == SMALL_ZOOM for t in small)


def test_full_run(tmp_path, api):
    progress = fcv.run(make_args(tmp_path), TOKEN)
    city_dir = tmp_path / "test-city-somewhere"

    assert {k: v["status"] for k, v in progress["tiles"].items()} == {
        fcv.tile_key(BIG_A): "completed", fcv.tile_key(BIG_B): "completed"
    }
    assert progress["summary"]["images_segmented"] == len(SEGMENTED_IDS)
    assert not list((city_dir / "partial").glob("*.parquet"))

    gdf = read_all_tiles(city_dir)
    assert set(gdf["id"]) == SEGMENTED_IDS  # partially detected images dropped, each image once
    assert len(gdf) == len(SEGMENTED_IDS)
    assert list(gdf.columns) == fcv.COLUMNS + ["geometry"]
    assert gdf.crs == "EPSG:4326"
    assert (gdf["vegetation_percent"] == 25.0).all()
    assert (gdf["h"] == 901.0).all()  # GPS altitude
    assert (gdf["h_computed"] == 900.5).all()
    assert (gdf["segmented_percent"] == 100.0).all()
    assert gdf["captured_at"].dt.year.eq(2023).all()

    # the dense tile (limit=3 < 5 images) and the refused one were split into deeper tiles
    widths = sorted({round(float(p["bbox"].split(",")[2]) - float(p["bbox"].split(",")[0]), 7) for p in api.calls["images"]})
    zoom18 = round(mercantile.bounds(DENSE_TILE).east - mercantile.bounds(DENSE_TILE).west, 7)
    assert widths[-1] == zoom18 and len(widths) >= 2
    refused_requests = [p for p in api.calls["images"] if mercantile.tile(
        *[(a + b) / 2 for a, b in zip(map(float, p["bbox"].split(",")[:2]), map(float, p["bbox"].split(",")[2:]))], 18
    ) == REFUSED_TILE]
    assert len(refused_requests) == 1 + 4  # refused once (not retried), then its 4 children

    # only images inside the city got a detections request, once each
    assert len(api.calls["detections"]) == len(IMAGES)

    # the token never reaches the outputs
    for path in city_dir.rglob("*"):
        if path.is_file():
            assert b"secret" not in path.read_bytes()


def test_resume_gives_the_same_result(tmp_path, api, monkeypatch):
    reference_dir = tmp_path / "reference"
    fcv.run(make_args(reference_dir), TOKEN)
    reference = read_all_tiles(reference_dir / "test-city-somewhere").sort_values("id", ignore_index=True)

    # stop after 20 small tiles (in the middle of the second big tile)
    original = fcv.process_small_tile
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 20:
            monkeypatch.setattr(fcv, "STOP_REQUESTED", True)
        return original(*args, **kwargs)

    monkeypatch.setattr(fcv, "process_small_tile", counting)
    progress = fcv.run(make_args(tmp_path / "resumed"), TOKEN)
    city_dir = tmp_path / "resumed" / "test-city-somewhere"
    second = progress["tiles"][fcv.tile_key(BIG_B)]
    assert progress["tiles"][fcv.tile_key(BIG_A)]["status"] == "completed"
    assert second["status"] == "in_progress" and len(second["small_tiles_done"]) == 4
    assert (city_dir / "partial" / f"{fcv.tile_key(BIG_B)}.parquet").exists()

    monkeypatch.setattr(fcv, "STOP_REQUESTED", False)
    monkeypatch.setattr(fcv, "process_small_tile", original)
    api.calls["detections"].clear()
    progress = fcv.run(make_args(tmp_path / "resumed"), TOKEN)
    assert progress["summary"]["completed"] == 2
    assert len(api.calls["detections"]) == 2 * 12  # only the 12 remaining small tiles were fetched

    resumed = read_all_tiles(city_dir).sort_values("id", ignore_index=True)
    pd.testing.assert_frame_equal(resumed, reference)


def test_time_budget(tmp_path, api, monkeypatch):
    clock = {"t": 0.0}

    def fake_now():
        clock["t"] += 10  # every clock read advances 10 seconds
        return clock["t"]

    monkeypatch.setattr(fcv, "now", fake_now)
    progress = fcv.run(make_args(tmp_path, max_minutes=1), TOKEN)
    assert progress["summary"]["completed"] == 0
    assert progress["summary"]["in_progress"] == 1


def test_failed_small_tile_is_retried_next_run(tmp_path, api, monkeypatch):
    failing = next(iter(mercantile.children(BIG_B, zoom=SMALL_ZOOM)))
    real = fcv.process_small_tile

    def flaky(tile, *args, **kwargs):
        if tile == failing:
            raise requests.exceptions.RequestException(f"boom access_token={TOKEN}")
        return real(tile, *args, **kwargs)

    monkeypatch.setattr(fcv, "process_small_tile", flaky)
    progress = fcv.run(make_args(tmp_path), TOKEN)
    state = progress["tiles"][fcv.tile_key(BIG_B)]
    assert state["status"] == "in_progress" and len(state["small_tiles_done"]) == 15

    monkeypatch.setattr(fcv, "process_small_tile", real)
    progress = fcv.run(make_args(tmp_path), TOKEN)
    assert progress["summary"]["completed"] == 2
    assert set(read_all_tiles(tmp_path / "test-city-somewhere")["id"]) == SEGMENTED_IDS


def test_zoom_mismatch_is_refused(tmp_path, api):
    fcv.run(make_args(tmp_path, max_big_tiles=1), TOKEN)
    with pytest.raises(SystemExit):
        fcv.run(make_args(tmp_path, small_zoom=17), TOKEN)


def test_deep_split_keeps_every_image(tmp_path, api):
    fcv.run(make_args(tmp_path), TOKEN)
    ids = set(read_all_tiles(tmp_path / "test-city-somewhere")["id"])
    deep = {i["id"] for i in IMAGES if f"-{fcv.tile_key(DEEP_TILE)}-" in i["id"]}
    assert len(deep) == 8 and deep <= ids
    widths = [float(p["bbox"].split(",")[2]) - float(p["bbox"].split(",")[0]) for p in api.calls["images"]]
    smallest_zoom18 = mercantile.bounds(DEEP_TILE).east - mercantile.bounds(DEEP_TILE).west
    assert min(widths) < smallest_zoom18 / 2**3  # split beyond 3 extra zoom levels


def test_rate_limit_backoff(tmp_path, api, monkeypatch):
    target = next(i["id"] for i in IMAGES if i["id"].startswith("veg"))
    failures = {"left": 2}
    real = api.__call__
    sleeps = []

    def flaky(url, params=None, timeout=None, headers=None):
        if url.endswith(f"/{target}/detections") and failures["left"]:
            failures["left"] -= 1
            return Response({"error": {"message": "Application request limit reached"}}, 429)
        return real(url, params=params, timeout=timeout, headers=headers)

    monkeypatch.setattr(requests, "get", flaky)
    monkeypatch.setattr(fcv, "sleep", sleeps.append)
    progress = fcv.run(make_args(tmp_path), TOKEN)

    assert failures["left"] == 0 and progress["summary"]["completed"] == 2
    assert target in set(read_all_tiles(tmp_path / "test-city-somewhere")["id"])
    assert len(sleeps) >= 2 and max(sleeps) >= 2 * fcv.RATE_LIMIT_FACTOR  # rate limits wait longer


def test_token_rejection_stops_the_run(tmp_path, api, monkeypatch):
    real = api.__call__

    def rejecting(url, params=None, timeout=None, headers=None):
        if url.endswith("/detections"):
            return Response({"error": {"message": "Invalid OAuth access token - Cannot parse access token"}}, 400)
        return real(url, params=params, timeout=timeout, headers=headers)

    monkeypatch.setattr(requests, "get", rejecting)
    with pytest.raises(SystemExit) as excinfo:
        fcv.run(make_args(tmp_path), TOKEN)
    assert "rejected the token" in str(excinfo.value) and "secret" not in str(excinfo.value)
    progress = json.loads((tmp_path / "test-city-somewhere" / "progress.json").read_text())
    assert progress["summary"]["in_progress"] == 1  # checkpointed before stopping


@pytest.mark.parametrize(
    "message, transient, rate_limited, rejected",
    [
        ("429 Client Error: Too Many Requests for url: x", True, True, False),
        ("Mapillary API error (HTTP 500): An unknown error has occurred", True, False, False),
        ("503 Server Error: Service Unavailable for url: x", True, False, False),
        ("500 Server Error: x (Please reduce the amount of data you're asking for)", False, False, False),
        ("Mapillary API error (HTTP 400): Invalid OAuth access token", False, False, True),
        ("401 Client Error: Unauthorized for url: x", False, False, True),
        # digits of coordinates in the URL are not status codes
        ("404 Client Error: Not Found for url: https://x/images?bbox=-49.4291,-25.5031,-49.401,-25.403", False, False, False),
    ],
)
def test_error_classification(message, transient, rate_limited, rejected):
    error = requests.exceptions.HTTPError(message)
    assert fcv.is_transient(error) == transient
    assert fcv.is_rate_limited(error) == rate_limited
    assert fcv.is_token_rejected(error) == rejected


def test_display_name_is_kept_when_the_polygon_comes_from_the_fallback(api):
    api.lookup = {"R42": [{**CITY_RELATION}]}  # lookup has the names but no polygon
    api.osmfr = {"type": "GeometryCollection", "geometries": [mapping(POLYGON)]}
    polygon, source = fcv.fetch_boundary("Test City")
    assert polygon.equals(POLYGON) and source["display_name"] == "Test City, Somewhere"


def test_big_tiles_are_processed_from_the_center(tmp_path, api, monkeypatch):
    # three big tiles in a row: the middle one is processed first
    left = mercantile.Tile(BIG_A.x - 1, BIG_A.y, BIG_ZOOM)
    polygon = MultiPolygon([fcv.tile_box(t).buffer(-INSET) for t in (left, BIG_A, BIG_B)])
    started = []
    monkeypatch.setattr(fcv, "load_or_fetch_boundary", lambda *a, **k: polygon)
    monkeypatch.setattr(fcv, "small_tiles_for", lambda big, poly, zoom: started.append(fcv.tile_key(big)) or [])
    fcv.run(make_args(tmp_path), TOKEN)
    assert started[0] == fcv.tile_key(BIG_A)


def test_coverage_rule():
    image = {"id": 1, "geometry": {"type": "Point", "coordinates": [0, 0]}, "altitude": None, "captured_at": 0}
    assert fcv.image_row(image, PARTIAL_DETECTIONS, 80) is None
    row = fcv.image_row(image, PARTIAL_DETECTIONS, 5)
    assert row["segmented_percent"] == pytest.approx(100 * (409 + 100 * 100 / 4096) / 4096, abs=1e-3)
    assert row["vegetation_percent"] == 0.0
    assert row["h"] != row["h"]  # NaN when the altitude is missing
    # overlapping polygons never count more than the whole image
    doubled = VEGETATION_DETECTIONS + VEGETATION_DETECTIONS
    assert fcv.image_row(image, doubled, 80)["segmented_percent"] == 100.0


def test_lower_min_coverage_keeps_partial_images(tmp_path, api):
    fcv.run(make_args(tmp_path, min_coverage=5.0), TOKEN)
    ids = set(read_all_tiles(tmp_path / "test-city-somewhere")["id"])
    assert ids == {i["id"] for i in IMAGES}


def test_progress_of_another_schema_or_coverage_is_refused(tmp_path, api):
    fcv.run(make_args(tmp_path, max_big_tiles=1), TOKEN)
    with pytest.raises(SystemExit, match="Delete"):
        fcv.run(make_args(tmp_path, min_coverage=50.0), TOKEN)

    progress_path = tmp_path / "test-city-somewhere" / "progress.json"
    progress = json.loads(progress_path.read_text())
    del progress["schema_version"]  # a progress file of the first version
    progress_path.write_text(json.dumps(progress))
    with pytest.raises(SystemExit, match="schema version 1"):
        fcv.run(make_args(tmp_path), TOKEN)


def test_point_runs_only_its_big_tile(tmp_path, api):
    b = mercantile.bounds(BIG_B)
    point = ((b.south + b.north) / 2, (b.west + b.east) / 2)  # (lat, lon)
    progress = fcv.run(make_args(tmp_path, point=point, slug="test-city-somewhere"), TOKEN)
    assert progress["tiles"][fcv.tile_key(BIG_B)]["status"] == "completed"
    assert progress["tiles"][fcv.tile_key(BIG_A)]["status"] == "not_started"

    # a completed tile is fetched again (and overwritten)
    api.calls["detections"].clear()
    fcv.run(make_args(tmp_path, point=point, slug="test-city-somewhere"), TOKEN)
    in_b = [i for i in IMAGES if mercantile.tile(*i["geometry"]["coordinates"], BIG_ZOOM) == BIG_B]
    assert len(api.calls["detections"]) == len(in_b)
    ids = set(gpd.read_parquet(tmp_path / "test-city-somewhere" / "tiles" / f"{fcv.tile_key(BIG_B)}.parquet")["id"])
    assert ids == {i["id"] for i in in_b if i["id"] in SEGMENTED_IDS}


def test_point_outside_the_city_is_an_error(tmp_path, api):
    with pytest.raises(SystemExit, match="outside the boundary"):
        fcv.run(make_args(tmp_path, point=(0.0, 0.0), slug="test-city-somewhere"), TOKEN)


@pytest.mark.parametrize(
    "argv, message",
    [
        (["Curitiba", "--point", "-25.4", "-49.2"], "requires --slug"),
        (["Curitiba", "--point", "95", "-49.2", "--slug", "curitiba"], "invalid coordinates"),
        (["Curitiba", "--point", "-25.4", "-200", "--slug", "curitiba"], "invalid coordinates"),
    ],
)
def test_point_argument_validation(argv, message, capsys):
    with pytest.raises(SystemExit):
        fcv.main(argv)
    assert message in capsys.readouterr().err
