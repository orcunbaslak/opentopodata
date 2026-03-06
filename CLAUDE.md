# OpenTopoData - Optimized Elevation API

## Overview

Self-hosted elevation API serving Copernicus GLO-30 DEM (30m global resolution) via a forked and optimized version of [opentopodata](https://github.com/ajnisbet/opentopodata).

- **Public URL**: https://terrain.pvx.ai
- **Local URL**: http://localhost:5000
- **Source repo**: /opt/opentopodata
- **Data directory**: /var/opentopodata/data/copernicus30m/ (26,450 tiles, ~549 GiB)
- **Config**: /var/opentopodata/config.yaml
- **Docker image**: `opentopodata:optimized`

## Quick Reference

### Start the server
```bash
docker run --rm -d --name opentopodata \
  -v /var/opentopodata/data:/app/data:ro \
  -v /var/opentopodata/config.yaml:/app/config.yaml:ro \
  -p 5000:5000 opentopodata:optimized
```

### Rebuild after code changes
```bash
cd /opt/opentopodata && docker build -t opentopodata:optimized -f docker/Dockerfile .
docker rm -f opentopodata
# Then run the start command above
```

### Test
```bash
curl "https://terrain.pvx.ai/v1/copernicus30m?locations=39.9334,32.8597&interpolation=nearest"
```

### API endpoints
- `GET /v1/copernicus30m?locations=lat,lon|lat,lon` — primary dataset
- `GET /v1/srtm30m?locations=lat,lon` — alias to copernicus30m
- `GET /v1/eudem25m?locations=lat,lon` — alias to copernicus30m
- `GET /health` — health check
- `GET /datasets` — list available datasets
- `POST /v1/copernicus30m` with body `locations=...` — for large batch queries

### Query parameters
- `locations` — pipe-delimited `lat,lon` pairs or Google polyline
- `interpolation` — `nearest` (fast, recommended), `bilinear` (smoother), `cubic`
- `nodata_value` — `null` (default), `nan`, or integer
- `format` — `json` (default) or `geojson`
- `samples` — integer, for path sampling between waypoints

## Dataset

- **Source**: Copernicus GLO-30 DEM from `s3://copernicus-dem-30m/` (public, no credentials)
- **Format**: Cloud Optimized GeoTIFF, DEFLATE compression, PREDICTOR=3, 3600x3600 pixels per tile
- **Coverage**: Global, 1-degree tiles
- **Naming**: Renamed from `Copernicus_DSM_COG_10_N39_00_E032_00_DEM.tif` to `N39E032.tif` (SRTM-style, required by opentopodata tile lookup)
- **No VRT needed**: opentopodata resolves tiles by filename convention for tiled datasets

### Re-downloading data (if needed)
```bash
aws s3 sync s3://copernicus-dem-30m/ /var/opentopodata/data/copernicus30m/ \
  --no-sign-request --only-show-errors

# Flatten from subdirectories
find /var/opentopodata/data/copernicus30m/ -mindepth 2 -name '*_DEM.tif' \
  -exec mv -t /var/opentopodata/data/copernicus30m/ {} +

# Rename to SRTM-style
cd /var/opentopodata/data/copernicus30m
for f in Copernicus_DSM_COG_10_*_DEM.tif; do
    newname=$(echo "$f" | sed -E 's/Copernicus_DSM_COG_10_([NS])([0-9]+)_00_([EW])([0-9]+)_00_DEM\.tif/\1\2\3\4.tif/')
    mv "$f" "$newname"
done

# Remove empty subdirectories
find /var/opentopodata/data/copernicus30m/ -mindepth 1 -type d -empty -delete
```

## Config (/var/opentopodata/config.yaml)

```yaml
max_locations_per_request: 5000
access_control_allow_origin: "*"
datasets:
- name: copernicus30m
  path: data/copernicus30m
- name: srtm30m
  path: data/copernicus30m
- name: eudem25m
  path: data/copernicus30m
```

- `max_locations_per_request: 5000` — GET is limited by URL length (~200 points), POST can handle the full 5000
- `srtm30m` and `eudem25m` are aliases pointing to the same copernicus30m data for backward compatibility

## Optimizations Applied

All changes are in `/opt/opentopodata/` and baked into the `opentopodata:optimized` Docker image.

### 1. Batch `f.sample()` for nearest interpolation (backend.py)
**Before**: Per-point `f.read(window=1x1)` loop — one disk I/O per point per tile.
**After**: Single `f.sample()` call reads all points in a tile at once.
**Impact**: 25-200x faster for nearest interpolation queries.

### 2. Rasterio LRU file handle cache (backend.py)
**Before**: `rasterio.open(path)` + close on every request, even for the same tile.
**After**: `_RasterioLRU` cache keeps 256 file handles open; LRU eviction closes least recently used.
**Impact**: Eliminates repeated header parsing and GDAL driver init. Thread-safe.

### 3. Single-dataset early exit (backend.py)
**Before**: All requests went through multi-dataset logic with `_Point` objects and bounds filtering.
**After**: Uncommented fast path — single dataset skips straight to `_get_elevation_for_single_dataset()`.
**Impact**: Less overhead per request for our primary use case (one dataset).

### 4. HTTP caching headers (docker/nginx.conf)
**Added**: `Cache-Control: public, max-age=86400` — clients and CDNs can cache responses for 24h.
**Also**: Added `application/geo+json` to gzip types.

### 5. Memcached 64MB → 256MB (docker/supervisord.conf)
**Before**: 64MB cache for tile path lookups.
**After**: 256MB — better coverage for 26,450 tile lookups.

### 6. Faster NaN fill (opentopodata/utils.py)
**Before**: `safe_is_nan()` with try/except per element.
**After**: Short-circuits when `value is None` (common case); uses direct `isinstance + math.isnan`.

### 7. Fixed bare except (opentopodata/api.py)
**Before**: `except:` catching all exceptions including SystemExit.
**After**: `except (ValueError, TypeError):` — only catches JSON parsing errors.
**Also**: Fixed `ConfigError` → `config.ConfigError` reference.

### 8. Dockerfile fix (docker/Dockerfile)
**Before**: `rm root/wheels/*` (missing leading slash, didn't clean up).
**After**: `rm -rf /root/wheels` — properly removes builder wheels from final image.

### 9. Debug prints removed (backend.py)
Removed `print(f"{xs=}")`, `print(f"{ys=}")`, `print(f"{tmp=}")` from production code.

## Benchmark Results (nearest interpolation)

| Scenario | Baseline | Optimized | Speedup |
|----------|----------|-----------|---------|
| 1 point, same tile | 196ms | 8ms | ~25x |
| 10 points, same tile | 2587ms | 13ms | ~200x |
| 5 points, 5 tiles | 1076ms | 17ms | ~63x |
| 50 points, multi-tile | 2438ms | 32ms | ~76x |
| 1000 points (POST) | 18645ms | 4161ms | 4.5x |
| Throughput (c=50) | 49 req/s | 1189 req/s | ~24x |

Bilinear interpolation sees ~5-10% improvement (LRU cache only; still uses per-point reads).

## File Layout

```
/opt/opentopodata/              # Source repo (build from here)
├── opentopodata/
│   ├── api.py                  # Flask HTTP layer
│   ├── backend.py              # Elevation reads, LRU cache, batch sampling
│   ├── config.py               # Dataset discovery, tile lookup
│   └── utils.py                # Coord reprojection, NaN fill
├── docker/
│   ├── Dockerfile              # Multi-stage build
│   ├── nginx.conf              # Reverse proxy, caching headers
│   ├── supervisord.conf        # Process manager (uwsgi, nginx, memcached)
│   ├── uwsgi.ini               # App server config
│   ├── run.sh                  # Entry point
│   ├── warm_cache.py           # Pre-populate memcache on startup
│   └── config_watcher.py       # Hot-reload config on file change
├── CLAUDE.md                   # This file
└── ...

/var/opentopodata/
├── config.yaml                 # Runtime config (mounted into container)
└── data/
    └── copernicus30m/          # 26,450 .tif tiles (~549 GiB)
        ├── N00E006.tif
        ├── N00E009.tif
        └── ...
```

## Architecture

```
Client → nginx (:5000) → uWSGI (unix socket) → Flask app
                                                  ├── api.py (request parsing, validation)
                                                  ├── backend.py (elevation reads via rasterio)
                                                  │   └── _RasterioLRU (cached file handles)
                                                  └── config.py (tile path resolution)
         memcached (unix socket) ← caches config + dataset metadata
         supervisor manages all processes
```

## Notes

- The original `opentopodata:local` image is the unmodified upstream build. `opentopodata:optimized` has all our changes.
- uWSGI worker count is auto-set from `nproc --all`. Override by setting `N_UWSGI_THREADS` env var.
- GDAL environment variables set in Dockerfile: `GDAL_DISABLE_READDIR_ON_OPEN=TRUE`, `GDAL_NUM_THREADS=ALL_CPUS`, `GDAL_CACHEMAX=512`.
- Tiles are already Cloud Optimized GeoTIFF with DEFLATE — no recompression needed per opentopodata performance guide.
