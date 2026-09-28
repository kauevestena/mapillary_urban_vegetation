# scripts

- `fetch_city_vegetation.py`: fetches the location, height and vegetation
  percent of every segmented Mapillary image of a city, tile by tile, into
  `data/<city_slug>/tiles/*.parquet`. Resumable; see the main README and
  `python scripts/fetch_city_vegetation.py --help`.
