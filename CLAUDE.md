# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Fork of [ajnisbet/opentopodata](https://github.com/ajnisbet/opentopodata) — a self-hosted REST API for elevation data. This fork serves Copernicus GLO-30 DEM (30m global resolution) with significant performance optimizations for high-throughput serving.

- **Local URL**: http://localhost:5000
- **Data**: /var/opentopodata/data/copernicus30m/ (26,450 tiles, ~549 GiB)
- **Config**: /var/opentopodata/config.yaml

## Build & Test Commands

```bash
# Build optimized image (what we run in production)
docker build -t opentopodata:optimized -f docker/Dockerfile .
docker compose up -d --force-recreate

# Build upstream-style (tagged with VERSION file)
make build

# Run tests (builds image first, runs pytest in container)
make test

# Run a single test file
docker build -t opentopodata:optimized -f docker/Dockerfile . && \
docker run --rm -e DISABLE_MEMCACHE=1 opentopodata:optimized \
  python -m pytest tests/test_backend.py -v --timeout=10

# Format check / format code
make black-check
make black                  # black --target-version py311

# Local dev without Docker (no memcache, no nginx)
FLASK_APP=opentopodata/api.py FLASK_DEBUG=1 flask run --port 5000

# Quick smoke test
curl "http://localhost:5000/v1/copernicus30m?locations=47.0,11.0&interpolation=nearest"
```

## Architecture

```
Client → nginx (:5000) → uWSGI (unix socket) → Flask app (api.py)
                                                  ├── backend.py  (elevation reads via rasterio)
                                                  │   └── _RasterioLRU (LRU cache of open file handles)
                                                  ├── config.py   (dataset discovery, tile path lookup)
                                                  └── utils.py    (CRS transforms, NaN handling)
         memcached (unix socket) ← caches dataset metadata
         supervisor manages: uwsgi, nginx, memcached, warm_cache, watch_config
```

### Request flow
1. `api.py` parses `locations` (pipe-delimited lat,lon or polyline), `interpolation`, `nodata_value`, `format`
2. `backend.get_elevation()` dispatches to `_get_elevation_for_single_dataset()` (fast path for 1 dataset) or multi-dataset logic
3. Points are grouped by tile path via `config.TiledDataset.location_paths()` (SRTM-style filename lookup: `N47E011.tif`)
4. `_get_elevation_from_path()` reads elevations — batch `f.sample()` for nearest, single-window numpy for bilinear/cubic
5. Boundary nudge (0.0005°) + adjacent-tile fallback handles integer-degree tile gaps

### Dataset types (config.py)
- **TiledDataset**: SRTM-style filenames (`N50W121.tif`), O(1) tile lookup via `decimal_base_floor`. This is what we use.
- **SingleFileDataset**: One raster file covers everything.
- **MultiDataset**: Composite with fallback priority (child datasets).

### Caching layers
1. **memcached** (256MB): Dataset metadata, tile path lookups
2. **Module-level dict** (`_SIMPLE_CACHE`): Config, version, datasets (avoids deserialization)
3. **_RasterioLRU** (256 handles): Open rasterio file handles (avoids repeated GDAL init)
4. **_TRANSFORMER_CACHE**: pyproj CRS transformers

## Key Optimizations (vs upstream)

| What | Where | Impact |
|------|-------|--------|
| Batch `f.sample()` for nearest | backend.py | 25-200x faster |
| Rasterio LRU file handle cache | backend.py | Eliminates repeated open/close |
| Single-dataset early exit | backend.py | Less overhead per request |
| Batch window read for bilinear/cubic | backend.py | Single I/O per tile |
| Boundary nudge + adjacent tile fallback | backend.py | Fixes null at integer-degree coords |
| HTTP cache headers (24h) | docker/nginx.conf | CDN/client caching |
| Memcached 256MB (was 64MB) | docker/supervisord.conf | Better tile lookup coverage |

## API Endpoints

- `GET/POST /v1/copernicus30m?locations=lat,lon|lat,lon` — primary dataset
- `GET /v1/srtm30m`, `GET /v1/eudem25m` — aliases to copernicus30m
- `GET /health` — health check
- `GET /datasets` — list available datasets

Query params: `interpolation` (nearest|bilinear|cubic), `nodata_value` (null|nan|int), `format` (json|geojson), `samples` (int, path sampling)

## Copernicus DEM Data Setup

The dataset is Copernicus GLO-30 DEM — 30m resolution, global coverage, distributed as Cloud Optimized GeoTIFFs on a public S3 bucket (no credentials required).

### Prerequisites
- AWS CLI (`apt install awscli` or `pip install awscli`)
- ~549 GiB free disk space

### 1. Download tiles from S3

```bash
# Public bucket, no credentials needed (--no-sign-request)
aws s3 sync s3://copernicus-dem-30m/ /var/opentopodata/data/copernicus30m/ \
  --no-sign-request --only-show-errors
```

This downloads ~26,450 tiles into per-tile subdirectories like:
```
copernicus30m/Copernicus_DSM_COG_10_N47_00_E011_00_DEM/Copernicus_DSM_COG_10_N47_00_E011_00_DEM.tif
```

### 2. Flatten directory structure

Move all .tif files up to the top-level directory:

```bash
find /var/opentopodata/data/copernicus30m/ -mindepth 2 -name '*_DEM.tif' \
  -exec mv -t /var/opentopodata/data/copernicus30m/ {} +

# Clean up empty subdirectories
find /var/opentopodata/data/copernicus30m/ -mindepth 1 -type d -empty -delete
```

### 3. Rename to SRTM-style filenames

OpenTopoData's `TiledDataset` resolves tiles by SRTM naming convention (`N47E011.tif`). The Copernicus names must be converted:

```bash
cd /var/opentopodata/data/copernicus30m
for f in Copernicus_DSM_COG_10_*_DEM.tif; do
    newname=$(echo "$f" | sed -E 's/Copernicus_DSM_COG_10_([NS])([0-9]+)_00_([EW])([0-9]+)_00_DEM\.tif/\1\2\3\4.tif/')
    mv "$f" "$newname"
done
```

Example: `Copernicus_DSM_COG_10_N47_00_E011_00_DEM.tif` → `N47E011.tif`

### 4. Configure the dataset

Create `/var/opentopodata/config.yaml`:

```yaml
max_locations_per_request: 5000
access_control_allow_origin: "*"
datasets:
- name: copernicus30m
  path: data/copernicus30m
- name: srtm30m
  path: data/copernicus30m    # alias for backward compat
- name: eudem25m
  path: data/copernicus30m    # alias for backward compat
```

### 5. Build and run

```bash
cd /opt/opentopodata
docker build -t opentopodata:optimized -f docker/Dockerfile .
docker compose up -d
```

### 6. Verify

```bash
curl "http://localhost:5000/v1/copernicus30m?locations=47.0,11.0&interpolation=nearest"
# Should return ~1731m (Austrian Alps near Brenner Pass)
```

### Data format notes
- Tiles are already Cloud Optimized GeoTIFF with DEFLATE compression — no recompression needed
- Each tile is 3600x3600 pixels covering 1°×1°
- Pixel centers are offset from integer degree boundaries by half a pixel (~0.000139°), which is why the boundary nudge logic exists in backend.py

## Non-Obvious Conventions

- `DISABLE_MEMCACHE=1` env var disables memcached for testing
- `N_UWSGI_THREADS` env var overrides auto-detected worker count from `nproc`
- GDAL env vars set in Dockerfile: `GDAL_DISABLE_READDIR_ON_OPEN=TRUE`, `GDAL_NUM_THREADS=ALL_CPUS`, `GDAL_CACHEMAX=512`
- Config supports `filename_tile_size` as Decimal (not float) to avoid precision issues
- Copernicus tile naming: renamed from `Copernicus_DSM_COG_10_N39_00_E032_00_DEM.tif` to `N39E032.tif`
- POST body supports form data, JSON, or query args; GET only uses query args
- The `_noop` lambda in backend.py replaces rasterio's default `math.floor` in `f.index()` to get raw pixel coords
