import collections
import threading

from rasterio.enums import Resampling
import numpy as np
import rasterio

from opentopodata import utils

# Only a subset of rasterio's supported methods are currently activated. In
# the future I might do interpolation in backend.py instead if relying on
# gdal, and I don't want to commit to supporting an interpolation method that
# would be a pain to do in python.
INTERPOLATION_METHODS = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    # 'cubic_spline': Resampling.cubic_spline,
    # 'lanczos': Resampling.lanczos,
}


class _RasterioLRU:
    """LRU cache for open rasterio file handles.

    Keeps up to maxsize dataset handles open to avoid repeated open/close
    overhead (header parsing, GDAL driver init). Evicted handles are closed.
    """

    def __init__(self, maxsize=128):
        self._cache = collections.OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def open(self, path):
        with self._lock:
            if path in self._cache:
                self._cache.move_to_end(path)
                return self._cache[path]
        # Open outside the lock to avoid blocking other threads during I/O.
        f = rasterio.open(path)
        with self._lock:
            # Another thread may have opened the same path while we were
            # waiting. If so, close our duplicate and use theirs.
            if path in self._cache:
                f.close()
                self._cache.move_to_end(path)
                return self._cache[path]
            self._cache[path] = f
            while len(self._cache) > self._maxsize:
                _, evicted = self._cache.popitem(last=False)
                evicted.close()
            return f


_RASTERIO_CACHE = _RasterioLRU(maxsize=256)


class InputError(ValueError):
    """Invalid input data.

    The error message should be safe to pass back to the client.
    """


def _noop(x):
    return x


def _validate_points_lie_within_raster(xs, ys, lats, lons, bounds, res):
    """Check that querying the dataset won't throw an error.

    Args:
        xs, ys: Lists/arrays of x/y coordinates, in projection of file.
        lats, lons: Lists/arrays of lat/lon coordinates. Only used for error message.
        bounds: rastio BoundingBox object.
        res: Tuple of (x_res, y_res) resolutions.

    Raises:
        InputError: if one of the points lies outside bounds.
    """
    oob_indices = set()

    # Get actual extent. When storing point data in a pixel-based raster
    # format, the true extent is the centre of the outer pixels, but GDAL
    # reports the extent as the outer edge of the outer pixels. So need to
    # adjust by half the pixel width.
    #
    # Also add an epsilon to account for
    # floating point precision issues: better to validate an invalid point
    # which will error out on the reading anyway, than to invalidate a valid
    # point.
    atol = 1e-8
    x_min = min(bounds.left, bounds.right) + abs(res[0]) / 2 - atol
    x_max = max(bounds.left, bounds.right) - abs(res[0]) / 2 + atol
    y_min = min(bounds.top, bounds.bottom) + abs(res[1]) / 2 - atol
    y_max = max(bounds.top, bounds.bottom) - abs(res[1]) / 2 + atol

    # Check bounds.
    x_in_bounds = (xs >= x_min) & (xs <= x_max)
    y_in_bounds = (ys >= y_min) & (ys <= y_max)

    # Found out of bounds.
    oob_indices.update(np.nonzero(~x_in_bounds)[0])
    oob_indices.update(np.nonzero(~y_in_bounds)[0])
    return sorted(oob_indices)


def _batch_bilinear(data, rows, cols):
    """Bilinear interpolation for arrays of fractional row/col coordinates.

    Args:
        data: 2D numpy array (the raster window).
        rows, cols: 1D arrays of fractional pixel coordinates relative to data.

    Returns:
        List of interpolated float values (NaN where input is NaN).
    """
    h, w = data.shape
    r0 = np.floor(rows).astype(int).clip(0, h - 2)
    c0 = np.floor(cols).astype(int).clip(0, w - 2)
    dr = rows - r0
    dc = cols - c0

    # Four corners.
    v00 = data[r0, c0]
    v01 = data[r0, c0 + 1]
    v10 = data[r0 + 1, c0]
    v11 = data[r0 + 1, c0 + 1]

    z = (v00 * (1 - dr) * (1 - dc) +
         v01 * (1 - dr) * dc +
         v10 * dr * (1 - dc) +
         v11 * dr * dc)

    return [float(v) for v in z]


def _batch_cubic(data, rows, cols):
    """Cubic (Catmull-Rom) interpolation for arrays of fractional row/col coordinates.

    Args:
        data: 2D numpy array (the raster window).
        rows, cols: 1D arrays of fractional pixel coordinates relative to data.

    Returns:
        List of interpolated float values.
    """
    h, w = data.shape
    r0 = np.floor(rows).astype(int)
    c0 = np.floor(cols).astype(int)
    dr = rows - r0
    dc = cols - c0

    results = np.empty(len(rows), dtype=float)
    for idx in range(len(rows)):
        # 4x4 neighborhood centered on the pixel.
        rr = r0[idx]
        cc = c0[idx]
        row_indices = np.clip([rr - 1, rr, rr + 1, rr + 2], 0, h - 1)
        col_indices = np.clip([cc - 1, cc, cc + 1, cc + 2], 0, w - 1)
        patch = data[np.ix_(row_indices, col_indices)]

        # Catmull-Rom weights.
        t = dr[idx]
        wr = _cubic_weights(t)
        t = dc[idx]
        wc = _cubic_weights(t)

        results[idx] = wr @ patch @ wc

    return [float(v) for v in results]


def _cubic_weights(t):
    """Catmull-Rom spline weights for offset t in [0, 1]."""
    t2 = t * t
    t3 = t2 * t
    w0 = -0.5 * t3 + t2 - 0.5 * t
    w1 = 1.5 * t3 - 2.5 * t2 + 1.0
    w2 = -1.5 * t3 + 2.0 * t2 + 0.5 * t
    w3 = 0.5 * t3 - 0.5 * t2
    return np.array([w0, w1, w2, w3])


def _get_elevation_from_path(lats, lons, path, interpolation):
    """Read values at locations in a raster.

    Args:
        lats, lons: Arrays of latitudes/longitudes.
        path: GDAL supported raster location.
        interpolation: method name string.

    Returns:
        z_all: List of elevations, same length as lats/lons.
    """
    z_all = []
    interpolation = INTERPOLATION_METHODS.get(interpolation)
    lons = np.asarray(lons)
    lats = np.asarray(lats)

    try:
        f = _RASTERIO_CACHE.open(path)

        if f.crs is None:
            msg = "Dataset has no coordinate reference system."
            msg += f" Check the file '{path}' is a geo raster."
            msg += " Otherwise you'll have to add the crs manually with a tool like gdaltranslate."
            raise InputError(msg)

        try:
            if f.crs.is_epsg_code:
                xs, ys = utils.reproject_latlons(lats, lons, epsg=f.crs.to_epsg())
            else:
                xs, ys = utils.reproject_latlons(lats, lons, wkt=f.crs.to_wkt())
        except ValueError:
            raise InputError("Unable to transform latlons to dataset projection.")

        # Check bounds.
        oob_indices = _validate_points_lie_within_raster(
            xs, ys, lats, lons, f.bounds, f.res
        )
        rows, cols = tuple(f.index(xs.tolist(), ys.tolist(), op=_noop))

        # Different versions of rasterio may or may not collapse single
        # f.index() lookups into scalars. We want to always have an
        # array.
        rows = np.atleast_1d(rows)
        cols = np.atleast_1d(cols)

        # Use batch sampling for nearest interpolation (much faster).
        if interpolation == Resampling.nearest:
            xy_coords = list(zip(xs, ys))
            sampled = list(f.sample(xy_coords, indexes=1, masked=True))
            for i, val in enumerate(sampled):
                if i in oob_indices:
                    z_all.append(None)
                else:
                    z = float(val[0]) if val[0] is not np.ma.masked else np.nan
                    z_all.append(z)
        else:
            # Batch read for bilinear/cubic: read bounding window once,
            # then interpolate all points in numpy.
            oob_set = set(oob_indices)
            valid_mask = np.array([i not in oob_set for i in range(len(rows))])

            if not np.any(valid_mask):
                z_all = [None] * len(rows)
            else:
                # Convert pixel indices to fractional row/col (center-based).
                frows = rows[valid_mask] - 0.5
                fcols = cols[valid_mask] - 0.5
                frows = frows.clip(0, f.height - 1)
                fcols = fcols.clip(0, f.width - 1)

                # Determine padding for interpolation kernel.
                pad = 1 if interpolation == Resampling.bilinear else 2

                # Bounding window over all valid points, with padding.
                r_min = max(int(np.floor(frows.min())) - pad, 0)
                r_max = min(int(np.ceil(frows.max())) + pad + 1, f.height)
                c_min = max(int(np.floor(fcols.min())) - pad, 0)
                c_max = min(int(np.ceil(fcols.max())) + pad + 1, f.width)

                window = rasterio.windows.Window(c_min, r_min, c_max - c_min, r_max - r_min)
                data = f.read(indexes=1, window=window, out_dtype=float, boundless=True, masked=True)
                data = np.ma.filled(data, np.nan)

                # Coordinates relative to the window origin.
                lr = frows - r_min
                lc = fcols - c_min

                if interpolation == Resampling.bilinear:
                    valid_z = _batch_bilinear(data, lr, lc)
                else:
                    valid_z = _batch_cubic(data, lr, lc)

                # Merge back with oob points.
                z_all = [None] * len(rows)
                vi = 0
                for i in range(len(rows)):
                    if i in oob_set:
                        z_all[i] = None
                    else:
                        z_all[i] = valid_z[vi]
                        vi += 1

    # Depending on the file format, when rasterio finds an invalid projection
    # of file, it might load it with a None crs, or it might throw an error.
    except rasterio.RasterioIOError as e:
        if "not recognized as a supported file format" in str(e):
            msg = f"Dataset file '{path}' not recognised as a geo raster."
            msg += " Check that the file has projection information with gdalsrsinfo,"
            msg += " and that the file is not corrupt."
            raise InputError(msg)
        raise e

    return z_all


def _get_elevation_for_single_dataset(
    lats, lons, dataset, interpolation="nearest", nodata_value=None
):
    """Read elevations from a dataset.

    A dataset may consist of multiple files, so need to determine which
    locations lies in which file, then loop over the files.

    Args:
        lats, lons: Arrays of latitudes/longitudes.
        dataset: config.Dataset object.
        interpolation: method name string.

    Returns:
        elevations: List of elevations, same length as lats/lons.
    """

    # Which paths we need results from.
    lats = np.array(lats)
    lons = np.array(lons)
    paths = dataset.location_paths(lats, lons)

    # Store mapping of tile path to point so we can merge back together later.
    elevations_by_path = {}
    path_to_point_index = collections.defaultdict(list)
    for i, path in enumerate(paths):
        path_to_point_index[path].append(i)

    # Batch results by path.
    for path, indices in path_to_point_index.items():
        if path is None:
            elevations_by_path[None] = [None] * len(indices)
            continue
        batch_lats = lats[path_to_point_index[path]]
        batch_lons = lons[path_to_point_index[path]]
        elevations_by_path[path] = _get_elevation_from_path(
            batch_lats, batch_lons, path, interpolation
        )

    # Put the results back again.
    elevations = [None] * len(paths)
    for path, path_elevations in elevations_by_path.items():
        for i_path, i_original in enumerate(path_to_point_index[path]):
            elevations[i_original] = path_elevations[i_path]

    elevations = utils.fill_na(elevations, nodata_value)
    return elevations


class _Point:
    def __init__(self, lat, lon, index):
        self.lat = lat
        self.lon = lon
        self.index = index
        self.elevation = None
        self.dataset_name = None


def get_elevation(lats, lons, datasets, interpolation="nearest", nodata_value=None):
    """Read first non-null elevation from multiple datasets.


    Args:
        lats, lons: Arrays of latitudes/longitudes.
        dataset: config.Dataset object.
        interpolation: method name string.

    Returns:
        elevations: List of elevations, same length as lats/lons.
    """

    # Early exit for single dataset.
    if len(datasets) == 1:
        elevations = _get_elevation_for_single_dataset(
            lats, lons, datasets[0], interpolation, nodata_value
        )
        dataset_names = [datasets[0].name] * len(lats)
        return elevations, dataset_names

    # Check
    points = [_Point(lat, lon, idx) for idx, (lat, lon) in enumerate(zip(lats, lons))]
    for dataset in datasets:
        # Only check points that have no point yet. Can exit early if
        # there's no unqueried points.
        dataset_points = [p for p in points if p.elevation is None]
        if not dataset_points:
            break

        # Only check points within the dataset bounds.
        dataset_points = [
            p for p in dataset_points if p.lat >= dataset.wgs84_bounds.bottom
        ]
        dataset_points = [
            p for p in dataset_points if p.lat <= dataset.wgs84_bounds.top
        ]
        dataset_points = [
            p for p in dataset_points if p.lon >= dataset.wgs84_bounds.left
        ]
        dataset_points = [
            p for p in dataset_points if p.lon <= dataset.wgs84_bounds.right
        ]
        if not dataset_points:
            continue

        # Get locations.
        elevations = _get_elevation_for_single_dataset(
            [p.lat for p in dataset_points],
            [p.lon for p in dataset_points],
            dataset,
            interpolation,
            nodata_value,
        )

        # Save.
        for point, elevation in zip(dataset_points, elevations):
            points[point.index].elevation = elevation
            points[point.index].dataset_name = dataset.name

    # Return elevations.
    fallback_dataset_name = datasets[-1].name
    dataset_names = [p.dataset_name or fallback_dataset_name for p in points]
    elevations = [p.elevation for p in points]
    return elevations, dataset_names
