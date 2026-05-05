# Copernicus Tile-Seam Fix and EPSG Coordinate Input — Design

**Date:** 2026-05-05
**Branch:** `feat/epsg-input-and-seam-fix`
**Origin:** PVX repo PR #785 review — both issues identified there are symptom-fixes
on the C# client side; the root causes belong in this API.

---

## Problem statement

Two distinct issues are currently being worked around in the C# client
(`pvxai/pvx#785`). Both can be eliminated permanently in this API.

### Issue 1 — Vertical strips of `elevation = 0` at Copernicus tile seams

- **Symptom:** Terrain imports return columns of `0 m` along integer-degree tile
  boundaries. The C# side ships `FillSuspectZeroCells`, a heuristic that
  rewrites zero cells from neighbour means.
- **Root cause:** rasterio
  [#2916](https://github.com/rasterio/rasterio/issues/2916). Copernicus GLO-30
  COG headers ship with `nodata=None`. When `f.read(boundless=True, masked=True)`
  reads a window that crosses a tile edge, the out-of-tile pixels are filled
  with `0` and **cannot be masked** — there is no nodata value to compare
  against. `f.sample()` (used in the nearest fast path) has the same problem.
  Those zeros leak into the bilinear/cubic kernel and into nearest results.
- **Why fix in API:** The C# heuristic is unsafe — it can fabricate elevation
  for transport holes (`IsFull = false` cells), and it can overwrite real
  coastal/water `0 m` pixels. Reviewer @orcunbaslak flagged both as merge
  blockers in PR #785.

### Issue 2 — UTM coordinate drift in client/server round-trips

- **Symptom:** UTM coordinates returned by the client's response parser drift
  by sub-metre to several metres relative to what was sent. The C# side ships
  `CorrectDriftedPoints` to snap drifted points back to the originals when
  drift exceeds 100 m.
- **Root cause:** The client converts `UTMPoint → WGS84 → API → WGS84 → UTM`
  on every batch, with `float` (32-bit) precision in
  `CoordinateHelper.UTMToWGS84` and `WGS84ToUTM`. Losses accumulate, especially
  near UTM zone boundaries.
- **Why fix in API:** The drift is entirely a client-side double round-trip.
  The API already echoes the lat/lon it receives (`api.py:574`). The fix is
  to let clients communicate in their native CRS — the API's `pyproj`
  dependency can do the conversion in one direction with double precision,
  and echo the original projected coordinates verbatim. This eliminates the
  client round-trip entirely.

---

## Goals

1. **Issue 1:** Make `f.sample()` / `f.read(boundless=True, masked=True)` mask
   out-of-tile pixels correctly without changing the on-disk tiles.
2. **Issue 2:** Allow clients to send and receive coordinates in any EPSG-coded
   CRS, with the API performing the conversion internally.
3. **Backward compatibility:** Existing `?locations=lat,lon|…` requests must
   produce byte-identical responses (modulo the seam-fix correctness gain).
4. **No new dependencies** — `pyproj` and `rasterio` are already in the runtime.

## Non-goals

- New dataset registration or config-format changes.
- Polyline + CRS combination (polyline encoding is WGS84-specific; CRS only
  applies to `lat,lon|…` style locations).
- Bulk performance optimisation beyond what's already in place — `pyproj`
  transformers are already cached via `_TRANSFORMER_CACHE`.
- Any change to `backend.get_elevation()` interpolation logic.

---

## Approaches considered

### Issue 1 — three options

- **A. Override nodata on file open** *(recommended)* — In
  `_RasterioLRU.open()`, call `f.nodata = -32767` immediately after
  `rasterio.open(path)` if the file declares no nodata. Single point of
  control; both `f.sample()` and boundless reads benefit. Trade-off: relies
  on a documented Copernicus value (`-32767` per the
  [STEP forum thread](https://forum.step.esa.int/t/error-in-copernicus-dem-model-descriptor/36521)).
  Must NOT use `0` here — see rasterio
  [#3245](https://github.com/rasterio/rasterio/issues/3245) — it would mask
  real sea-level pixels.
- **B. Pass `boundless_fill_value=np.nan` on each read** — More invasive: needs
  separate logic for `f.sample()` (which has no equivalent kwarg) and
  `f.read()`. Avoids global handle state mutation but doubles the surface
  area of the fix.
- **C. Build-time tile buffering** — Physically buffer each tile by 1 pixel
  from neighbours offline (per upstream's
  [buffering-tiles note](https://github.com/ajnisbet/opentopodata/blob/master/docs/notes/buffering-tiles.md)).
  Heaviest option; requires re-processing 549 GiB of tiles.

**Decision:** A. ~5 lines in one place, deterministic, uses the documented
nodata value, restores the masking guarantee both sample paths rely on.

### Issue 2 — three options

- **A. UTM-only parameters** (`?utm_zone=33&hemisphere=N&locations=…`) — Simple
  but solves only one projection family. UPS, Lambert, national grids would
  still force clients into round-trips.
- **B. Generic EPSG parameter** (`?crs=EPSG:NNNN&locations=…`) *(recommended)* —
  `pyproj.CRS.from_user_input` already accepts every EPSG code. Single code
  path covers UTM (the immediate need), UPS, national grids, and any future
  projection. Validation cost: ~10 lines.
- **C. Separate endpoint** (`/v1/<dataset>/utm` or `/v2/<dataset>`) — Splits
  the contract cleanly but doubles maintenance for `interpolation`, `format`,
  `samples`, etc.

**Decision:** B. Generic EPSG with a single optional `crs` parameter. Existing
calls without `crs` keep their contract byte-identical.

---

## Architecture

```
                    ┌───────────────────────────────────────────────┐
                    │  api.py — request handling layer              │
                    │                                               │
   request ──►──────┤   _parse_crs(crs_str)            (NEW)        │
                    │       │                                       │
                    │       ▼                                       │
                    │   _parse_locations(loc, max_n, crs)  (EXTEND) │
                    │       │  if crs: pyproj transform x,y → lat/lon
                    │       ▼                                       │
                    │   backend.get_elevation(lats, lons, …)        │
                    │       │                                       │
                    │       ▼                                       │
                    │   build response: echo original (x,y,crs)     │
                    │   when crs was provided, else (lat, lng)      │
                    └───────────────────────────────────────────────┘
                                       │
                                       ▼
                    ┌───────────────────────────────────────────────┐
                    │  backend.py — elevation read layer            │
                    │                                               │
                    │   _RasterioLRU.open(path)         (PATCH)     │
                    │       └─► f = rasterio.open(path)             │
                    │           if f.nodata is None:                │
                    │               f.nodata = -32767               │
                    │                                               │
                    │   _get_elevation_from_path(...)   (UNCHANGED) │
                    │       f.sample(...) / f.read(...)             │
                    │       — now correctly masks seam pixels       │
                    └───────────────────────────────────────────────┘
```

### Components

#### Issue 1 — `backend.py` patch

In `_RasterioLRU.open()`, after `f = rasterio.open(path)` and **before** the
handle is inserted into the LRU cache (i.e. while still outside the lock, on
the same thread that opened it). Setting nodata before any other thread can
observe the cached handle keeps the override race-free without holding the
lock during the rasterio call.

```python
f = rasterio.open(path)
# Copernicus GLO-30 COGs ship with nodata=None, which causes boundless reads
# and f.sample() to silently return 0 for out-of-tile pixels (rasterio #2916).
# -32767 is Copernicus's documented nodata; do NOT use 0 (rasterio #3245
# would then mask real sea-level pixels).
if f.nodata is None:
    f.nodata = -32767
with self._lock:
    ...  # existing cache insertion logic
```

That is the entire production-code change for Issue 1.

#### Issue 2 — `api.py` extensions

Three small additions:

1. **`_parse_crs(crs_str)`** — accepts `"EPSG:NNNN"` (case-insensitive),
   validates via `pyproj.CRS.from_user_input`, returns the parsed CRS object.
   Raises `ClientError` on invalid input with a friendly message.

2. **`_parse_locations(locations, max_n, crs=None)`** — extends the existing
   parser:
   - When `crs is None`: behaviour unchanged (parse as `lat,lon`, return
     `(lats, lons, None)`).
   - When `crs is not None`: parse as `x,y`, look up a cached
     `pyproj.Transformer` from `crs → EPSG:4326`, transform, return
     `(lats, lons, (xs, ys, crs_str))`. The third tuple element carries the
     original projected coordinates so the response builder can echo them.

3. **Response build** in the `get_elevation` route:
   - Existing branch unchanged (`{"lat": lat, "lng": lon}`).
   - When `original_xy` is non-`None`, use `{"x": x, "y": y, "crs": crs_str}`
     and echo the values the client sent.

`utils.py` already has `_TRANSFORMER_CACHE` — we reuse it. No new caches.

---

## Data flow

### New `crs`-bearing request (full path)

```
Client posts:
  POST /v1/copernicus30m
  {"locations":"669875,5219140|669900,5219140",
   "crs":"EPSG:32633",
   "interpolation":"bilinear"}

           │
           ▼
api.py: _parse_crs("EPSG:32633")              → CRS object
api.py: _parse_locations(...)                  → lats=[47.123, 47.123],
                                                  lons=[11.456, 11.457],
                                                  original=([669875, 669900],
                                                            [5219140, 5219140],
                                                           "EPSG:32633")
           │
           ▼
backend.get_elevation(lats, lons, …)           → elevations=[1731.0, 1731.4]
   └─ _RasterioLRU.open(N47E011.tif)
        with nodata = -32767 (Issue 1 fix)     → seam pixels mask correctly
           │
           ▼
api.py response build (crs branch):
  {"results":[
     {"elevation":1731.0,
      "dataset":"copernicus30m",
      "location":{"x":669875,"y":5219140,"crs":"EPSG:32633"}},
     {"elevation":1731.4,
      "dataset":"copernicus30m",
      "location":{"x":669900,"y":5219140,"crs":"EPSG:32633"}}],
   "status":"OK"}
```

### Legacy WGS84 request (unchanged)

Identical to the current behaviour. Both code paths share the same backend
call and only diverge in `_parse_locations`'s pre-processing and the response
formatter's branch.

---

## API contract

### Existing (preserved verbatim)

| Param | Where | Default | Notes |
|---|---|---|---|
| `locations` | query/body | required | `lat,lon` pairs delimited by `\|`, or polyline |
| `interpolation` | query/body | `nearest` | unchanged |
| `nodata_value` | query/body | `null` | unchanged |
| `format` | query/body | `json` | unchanged |
| `samples` | query/body | none | unchanged |

Response when `crs` is absent — byte-identical to current:

```json
{"status":"OK","results":[{"elevation":1731,"dataset":"copernicus30m",
                           "location":{"lat":47.123,"lng":11.456}}]}
```

### New (added)

| Param | Where | Default | Notes |
|---|---|---|---|
| `crs` | query/body | `null` | `"EPSG:NNNN"` string. When present, `locations` is parsed as `x,y` pairs in this CRS. Mutually exclusive with polyline-encoded locations. |

Response when `crs` is present:

```json
{"status":"OK","results":[{"elevation":1731,"dataset":"copernicus30m",
                           "location":{"x":669875,"y":5219140,"crs":"EPSG:32633"}}]}
```

The `x` and `y` values in `location` are echoed verbatim from the request — no
round-trip transformation.

### GeoJSON format with `crs`

GeoJSON's `Point` geometry is canonically WGS84 lon/lat; that is the format
contract. When `crs` is provided alongside `format=geojson`, the geometry
remains in WGS84 (so the GeoJSON stays valid), but the `properties` block
carries `x`, `y`, and `crs` for the original projected coordinates:

```json
{"type":"Feature",
 "geometry":{"type":"Point","coordinates":[11.456,47.123,1731]},
 "properties":{"dataset":"copernicus30m","x":669875,"y":5219140,
               "crs":"EPSG:32633"}}
```

---

## Error handling

| Condition | Status | Message |
|---|---|---|
| `crs` not parseable as EPSG / unknown EPSG code | 400 INVALID_REQUEST | `"Invalid CRS '<value>'. Provide an EPSG code like 'EPSG:32633'."` |
| `crs` provided alongside polyline-encoded `locations` | 400 INVALID_REQUEST | `"Polyline-encoded locations require WGS84; do not pass 'crs' with polyline."` |
| Coordinate outside the CRS's valid area (pyproj returns `inf`) | 400 INVALID_REQUEST | `"Location <i> (x=<x>, y=<y>) is outside the area of use for <crs>."` |
| `locations` fails to parse as numeric pairs | 400 INVALID_REQUEST | existing message reused |

All errors follow the existing `ClientError → 400` pattern (`api.py:580-581`).
No new exception types are introduced.

---

## Testing

### Issue 1 — `tests/test_backend.py`

A small fixture is needed: a 8×8 GeoTIFF whose header has `nodata=None`,
mimicking Copernicus. Built once with `gdal_translate -a_nodata none …`
from an existing test tile and committed under
`tests/data/datasets/test-copernicus-seam/`.

- `test_copernicus_open_sets_nodata_when_missing` — open via `_RasterioLRU`,
  assert `f.nodata == -32767`.
- `test_seam_pixel_returns_nan_not_zero` — sample a coordinate just outside
  the tile extent (boundless), assert NaN (or `None` after `fill_na`),
  not `0.0`. This is the actual seam regression test.

### Issue 2 — `tests/test_api.py`

- `test_crs_utm_north_request_succeeds` — `EPSG:32633`, posts UTM coords
  inside the configured test dataset, asserts elevation matches the
  WGS84-equivalent request to within float tolerance, and asserts
  `location.x` / `location.y` equal the input verbatim.
- `test_crs_utm_south_request_succeeds` — `EPSG:32735`, same pattern in the
  southern hemisphere.
- `test_crs_invalid_epsg_returns_400` — `crs=EPSG:99999`, assert 400 with
  the documented error message.
- `test_crs_with_polyline_locations_returns_400` — `crs=EPSG:32633` plus
  polyline-encoded `locations`, assert 400.
- `test_no_crs_response_byte_compatible_with_legacy` — request without `crs`,
  assert response shape contains `location.lat`/`location.lng` and **not**
  `location.x`/`location.y`/`location.crs`.
- `test_crs_geojson_format_keeps_wgs84_geometry` — `format=geojson`, assert
  geometry is WGS84 lon/lat and properties carry `x`, `y`, `crs`.

### Tests we deliberately do not add

- Performance/load tests for the transformer cache — already covered by
  the existing `_TRANSFORMER_CACHE` infrastructure.
- A test for every UTM zone — pyproj is the contract, not us.

---

## Files touched

```
opentopodata/backend.py               (+5 lines  — Issue 1)
opentopodata/api.py                   (+~80 lines — Issue 2)
docs/api.md                           (~+30 lines — `crs` parameter docs)
tests/test_backend.py                 (+~40 lines — Issue 1 regression)
tests/test_api.py                     (+~120 lines — Issue 2 cases)
tests/data/datasets/test-copernicus-seam/   (1 new GeoTIFF fixture)
```

No changes to: `config.py`, `utils.py` (reused as-is), Docker/nginx/uWSGI
configs, `CLAUDE.md`.

---

## Risk

| Risk | Likelihood | Mitigation |
|---|---|---|
| `f.nodata = -32767` masks legitimate `-32767` pixels in non-Copernicus datasets that also lack nodata | Low | The override only applies when `f.nodata is None`. Datasets that explicitly declare `nodata` are unaffected. Document the behaviour in `CLAUDE.md`. |
| pyproj transformer init for an unfamiliar EPSG code is slow on first use | Low | Already cached via `_TRANSFORMER_CACHE`. First-call cost ~5-20 ms; subsequent calls sub-ms. |
| Client passes `crs` with polyline locations | Low | Validated explicitly with a 400 response. |
| Existing WGS84 callers see any behavioural change | Negligible | The `crs is None` branch is byte-identical to the current code; existing tests must continue to pass without modification. |

---

## Out of scope (explicit deferrals)

- Configuring per-dataset default CRS in `config.yaml`.
- Server-side caching of elevation results keyed on `(crs, x, y)`.
- Returning the projected coordinates of the *sampled pixel centre* rather
  than the input — the client wants its inputs echoed, not pixel snapping.
