from opentopodata import backend
import rasterio
import pytest
import numpy as np
from unittest.mock import patch

from opentopodata import config


ETOPO1_GEOTIFF_PATH = "tests/data/datasets/test-etopo1-resampled-1deg/ETOPO1_Ice_g_geotiff.resampled-1deg.tif"
ETOPO1_DATASET_NAME = "test-dataset"
ETOPO1_RESAMPLED_DATASET_NAME = "etopo1deg"
SRTM_DATASET_NAME = "srtm90subset"
SRTM_UTM_DATASET_NAME = "srtm90utm"
EU_DEM_DATASET_NAME = "eudemsubset"
EU_DEM_NO_EPSG_DATASET_NAME = "eudemnoepsg"
NO_FILL_VALUE_CONFIG_PATH = "tests/data/configs/no-fill-value.yaml"
TEST_CONFIG_PATH = "tests/data/configs/test-config.yaml"
NODATA_DATASET_PATH = "tests/data/datasets/test-nodata/nodata.geotiff"
NODATA_DATASET_NAME = "nodata"
EUDEM_TILE_PATH = "tests/data/datasets/test-eu-dem-subset/N2000000E3000000.TIF"


@pytest.fixture
def patch_config():
    with patch("opentopodata.config.CONFIG_PATH", TEST_CONFIG_PATH):
        yield


def test_noop():
    x = "Test"
    assert backend._noop(x) == x


class TestValidatePointsLieWithinRaster:
    bounds = rasterio.coords.BoundingBox(-101, -51, 101, 51)  # L,B,R,T
    res = (2, 2)

    def test_valid_points_on_boundary(self):
        lats = np.array([0, -50, 50])
        lons = np.array([0, -100, 100])
        assert [] == backend._validate_points_lie_within_raster(
            lons, lats, lats, lons, self.bounds, self.res
        )

    def test_invalid_bottom(self):
        lats = np.array([0, self.bounds.bottom])
        lons = np.array([0, 0])
        assert [1] == backend._validate_points_lie_within_raster(
            lons, lats, lats, lons, self.bounds, self.res
        )

    def test_invalid_top(self):
        lats = np.array([self.bounds.top])
        lons = np.array([0])
        assert [0] == backend._validate_points_lie_within_raster(
            lons, lats, lats, lons, self.bounds, self.res
        )

    def test_invalid_left(self):
        lats = np.array([0])
        lons = np.array([self.bounds.left])
        assert [0] == backend._validate_points_lie_within_raster(
            lons, lats, lats, lons, self.bounds, self.res
        )

    def test_invalid_right(self):
        lats = np.array([0])
        lons = np.array([self.bounds.right])
        assert [0] == backend._validate_points_lie_within_raster(
            lons, lats, lats, lons, self.bounds, self.res
        )

    def test_invalid_xy_valid_latlons(self):
        # Only x/y should be used for testing, latlon should be independent.
        x = np.array([self.bounds.right])
        y = np.array([0])
        lats = y
        lons = np.array([0])
        assert [0] == backend._validate_points_lie_within_raster(
            x, y, lats, lons, self.bounds, self.res
        )

    def test_valid_xy_invalid_latlons(self):
        xs = np.array([0, -100, 100])
        ys = np.array([0, -50, 50])
        lats = np.array([1000, 1000, -1000])
        lons = np.array([1000, 1000, -1000])
        assert [] == backend._validate_points_lie_within_raster(
            xs, ys, lats, lons, self.bounds, self.res
        )

    def test_floating_point_trickery(self):
        # Values taken from ASTER dataset which was causing issues.
        res = (0.000277777777777778, 0.000277777777777778)
        bounds = rasterio.coords.BoundingBox(
            149.999861111111, -33.00013888888888, 151.00013888888876, -31.9998611111111
        )

        xs = np.array([150])
        ys = np.array([-33])
        assert [] == backend._validate_points_lie_within_raster(
            xs, ys, [], [], bounds, res
        )


class TestGetElevationFromPath:
    with rasterio.open(ETOPO1_GEOTIFF_PATH) as f:
        geotiff_z = f.read(1)

    def test_all_interpolation_methods(self):
        lats = [0.5]
        lons = [0.4]
        for method in backend.INTERPOLATION_METHODS.keys():
            backend._get_elevation_from_path(lats, lons, ETOPO1_GEOTIFF_PATH, method)

    def test_read_ul_corner(self):
        lats = [90]
        lons = [-180]
        z = backend._get_elevation_from_path(
            lats, lons, ETOPO1_GEOTIFF_PATH, "bilinear"
        )
        assert z[0] == self.geotiff_z[0, 0]

    def test_read_lr_corner(self):
        lats = [-90]
        lons = [180]
        z = backend._get_elevation_from_path(
            lats, lons, ETOPO1_GEOTIFF_PATH, "bilinear"
        )
        assert z[0] == self.geotiff_z[-1, -1]

    def test_nearest_interpolation(self):
        lats = [89.51]
        lons = [-179.51]
        z = backend._get_elevation_from_path(lats, lons, ETOPO1_GEOTIFF_PATH, "nearest")
        assert z[0] == self.geotiff_z[0, 0]

    def _interp_bilinear(self, x, y, z):
        return (
            z[0][0] * (1 - x) * (1 - y)
            + z[1][0] * x * (1 - y)
            + z[0][1] * (1 - x) * y
            + z[1][1] * x * y
        )

    def test_bilinear_interpolation(self):
        lats = [89.6]
        lons = [-179.7]
        z = backend._get_elevation_from_path(
            lats, lons, ETOPO1_GEOTIFF_PATH, "bilinear"
        )
        assert pytest.approx(z[0]) == self._interp_bilinear(
            0.4, 0.3, self.geotiff_z[:2, :2]
        )

    def test_none_outside_dataset(self):
        lats = [0, 0, -90.1, 90.1]
        lons = [-180.1, 180.1, 0, 0]
        for lat, lon in zip(lats, lons):
            z = backend._get_elevation_from_path(
                [lat], [lon], ETOPO1_GEOTIFF_PATH, interpolation="lanczos"
            )[0]
            assert z is None

    def test_valid_read_from_dataset_with_nans(self):
        """
        Array looks like
            [[   2,    1,    0],
             [   3, 9999, 9999],
             [   4, 9999, 9999]
        with NODATA=9999 set on the GEOTIFF, bounds from 0-2 lat and lon.
        """
        lat = 0
        lon = 0
        z = backend._get_elevation_from_path(
            [lat], [lon], NODATA_DATASET_PATH, "bilinear"
        )
        assert z[0] == 4

    def test_nodata_read_from_dataset_with_nans(self):
        lat = 0
        lon = 2
        z = backend._get_elevation_from_path(
            [lat], [lon], NODATA_DATASET_PATH, "bilinear"
        )
        assert np.isnan(z[0])

    def test_valid_nearest_next_to_nodata(self):
        lat = 1
        lon = 0.49
        z = backend._get_elevation_from_path(
            [lat], [lon], NODATA_DATASET_PATH, "nearest"
        )
        assert z[0] == 3

    def test_invalid_nearest_next_to_nodata(self):
        lat = 1
        lon = 0.51
        z = backend._get_elevation_from_path(
            [lat], [lon], NODATA_DATASET_PATH, "nearest"
        )
        assert np.isnan(z[0])

    def test_valid_bilinear_next_to_nodata(self):
        lat = 2
        lon = 0.5
        z = backend._get_elevation_from_path(
            [lat], [lon], NODATA_DATASET_PATH, "bilinear"
        )
        assert z[0] == 1.5

    def test_invalid_bilinear_next_to_nodata(self):
        lat = 1
        lon = 0.5
        z = backend._get_elevation_from_path(
            [lat], [lon], NODATA_DATASET_PATH, "bilinear"
        )
        assert np.isnan(z[0])

    def test_invalid_cubic_nodata(self):
        lat = 0
        lon = 2
        z = backend._get_elevation_from_path([lat], [lon], NODATA_DATASET_PATH, "cubic")
        assert np.isnan(z[0])

    def test_nodata_is_nan(self):
        # EU-dem has NODATA over water.
        lat = [44.969186]
        lon = [-3.152424]
        z = backend._get_elevation_from_path(
            [lat], [lon], EUDEM_TILE_PATH, interpolation="nearest"
        )
        assert np.isnan(z)


class TestGetElevationForSingleDataset:
    def test_single_file_dataset(self):
        lats = [0.1, -9]
        lons = [-50.5, 12.11]
        interpolation = "cubic"
        dataset = config.load_datasets()[ETOPO1_DATASET_NAME]
        elevations_by_dataset = backend._get_elevation_for_single_dataset(
            lats, lons, dataset, interpolation
        )
        elevations_by_path = backend._get_elevation_from_path(
            lats, lons, ETOPO1_GEOTIFF_PATH, interpolation
        )
        assert elevations_by_dataset == elevations_by_path

    def test_oob(self, patch_config):
        lats = [1.5, -0.5, 0.5, 0.5]
        lons = [10.5, 11.5, 9.5, 12.5]
        dataset = config.load_datasets()[SRTM_DATASET_NAME]
        z = backend._get_elevation_for_single_dataset(lats, lons, dataset)
        assert all([x is None for x in z])

    def test_srtm_tiles(self, patch_config):
        lats = [0.1, 0.9]
        lons = [10.5, 11.5]
        dataset = config.load_datasets()[SRTM_DATASET_NAME]
        z = backend._get_elevation_for_single_dataset(lats, lons, dataset)
        assert all(z)
        assert all(np.isfinite(z))

    def test_utm(self, patch_config):
        lats = [0.2, 0.8, 0.6]
        lons = [10.2, 10.8, 11.5]

        dataset = config.load_datasets()[SRTM_DATASET_NAME]
        z = backend._get_elevation_for_single_dataset(lats, lons, dataset)

        dataset_utm = config.load_datasets()[SRTM_UTM_DATASET_NAME]
        z_utm = backend._get_elevation_for_single_dataset(lats, lons, dataset_utm)

        assert np.allclose(z, z_utm)

    def test_out_of_srtm_bounds(self, patch_config):
        lats = [70]
        lons = [10.5]
        dataset = config.load_datasets()[SRTM_DATASET_NAME]
        z = backend._get_elevation_for_single_dataset(lats, lons, dataset)[0]
        assert z is None

    def test_out_of_srtm_bounds_utm(self, patch_config):
        lats = [70]
        lons = [10.5]
        dataset = config.load_datasets()[SRTM_DATASET_NAME]
        z = backend._get_elevation_for_single_dataset(lats, lons, dataset)[0]
        assert z is None

    def test_alternate_tiled_dataset(self, patch_config):
        lats = [47.625765]
        lons = [9.418759]
        dataset = config.load_datasets()[EU_DEM_DATASET_NAME]
        z = backend._get_elevation_for_single_dataset(lats, lons, dataset)
        assert np.isfinite(z)

    def test_dataset_crs_format_equivalence(self, patch_config):
        lats = [43.597009, 45.534601]
        lons = [1.455697, 10.698698]

        dataset_epsg = config.load_datasets()[EU_DEM_DATASET_NAME]
        z_epsg = backend._get_elevation_for_single_dataset(lats, lons, dataset_epsg)

        dataset_wkt = config.load_datasets()[EU_DEM_NO_EPSG_DATASET_NAME]
        z_wkt = backend._get_elevation_for_single_dataset(lats, lons, dataset_wkt)

        assert np.allclose(z_epsg, z_wkt)


class TestGetElevation:
    def test_mutli_datasets(self, patch_config):
        lats = [47.625765, 0.1, 70, 1]
        lons = [9.418759, 10.5, 150, 1]
        datasets = [
            config.load_datasets()[NODATA_DATASET_NAME],
            config.load_datasets()[EU_DEM_DATASET_NAME],
            config.load_datasets()[SRTM_DATASET_NAME],
            config.load_datasets()[ETOPO1_RESAMPLED_DATASET_NAME],
        ]
        z, dataset_names = backend.get_elevation(lats, lons, datasets)
        assert all(np.isfinite(z))
        assert dataset_names == [
            EU_DEM_DATASET_NAME,
            SRTM_DATASET_NAME,
            ETOPO1_RESAMPLED_DATASET_NAME,
            ETOPO1_RESAMPLED_DATASET_NAME,
        ]

    def test_multi_dataset_mask(self, patch_config):
        lats = [47.625765, 0.1, 70]
        lons = [9.418759, 10.5, 150]
        datasets = [
            config.load_datasets()[ETOPO1_RESAMPLED_DATASET_NAME],
            config.load_datasets()[EU_DEM_DATASET_NAME],
            config.load_datasets()[SRTM_DATASET_NAME],
        ]
        z, dataset_names = backend.get_elevation(lats, lons, datasets)
        assert all(np.isfinite(z))
        assert dataset_names == [ETOPO1_RESAMPLED_DATASET_NAME] * len(lats)

    def test_single_dataset(self, patch_config):
        lats = [0.1, 0.9]
        lons = [10.5, 11.5]
        dataset = config.load_datasets()[SRTM_DATASET_NAME]
        z, names = backend.get_elevation(lats, lons, [dataset])
        assert all(z)
        assert all(np.isfinite(z))
        assert names == [SRTM_DATASET_NAME] * len(lats)


class TestBoundlessReadFillValue:
    """Issue 1 regression: Copernicus GLO-30 COGs ship with nodata=None.
    Without fill_value=np.nan, a boundless read past the tile extent
    silently returns 0 for the padded pixels (rasterio #2916), and those
    zeros then drag bilinear/cubic interpolation kernels toward 0 along
    integer-degree tile seams. _get_elevation_from_path now passes
    fill_value=np.nan so seam padding becomes NaN instead."""

    @staticmethod
    def _write_tif(path, data, nodata=None):
        height, width = data.shape
        transform = rasterio.transform.from_bounds(
            west=0, south=0, east=1, north=1, width=width, height=height
        )
        kwargs = dict(
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype=data.dtype,
            crs="EPSG:4326",
            transform=transform,
        )
        if nodata is not None:
            kwargs["nodata"] = nodata
        with rasterio.open(path, "w", **kwargs) as dst:
            dst.write(data, 1)

    def test_seam_padding_is_nan_not_zero(self, tmp_path):
        # 4x4 tile with nodata=None. Read a 5x5 window starting one row
        # and column outside the extent; the surrounding ring is the
        # boundless padding that used to leak as 0.
        path = str(tmp_path / "no_nodata.tif")
        data = np.full((4, 4), 1500, dtype=np.int16)
        self._write_tif(path, data, nodata=None)

        # Sanity: the on-disk file has no nodata declared.
        with rasterio.open(path) as raw:
            assert raw.nodata is None

        lru = backend._RasterioLRU(maxsize=4)
        f = lru.open(path)

        window = rasterio.windows.Window(-1, -1, 6, 6)
        arr = f.read(
            1,
            window=window,
            boundless=True,
            masked=True,
            out_dtype=float,
            fill_value=np.nan,
        )
        arr = np.ma.filled(arr, np.nan)

        # Centre 4x4 is the real data.
        assert (arr[1:5, 1:5] == 1500).all()
        # Outer ring is boundless padding — must be NaN, not 0.
        assert np.isnan(arr[0, :]).all()
        assert np.isnan(arr[5, :]).all()
        assert np.isnan(arr[:, 0]).all()
        assert np.isnan(arr[:, 5]).all()

    def test_inside_tile_bilinear_unchanged(self, tmp_path):
        # End-to-end via _get_elevation_from_path: a point well inside
        # the tile must still return the clean elevation, i.e. the
        # fill_value=np.nan kwarg has not perturbed in-tile reads.
        path = str(tmp_path / "no_nodata_inside.tif")
        data = np.full((4, 4), 1500, dtype=np.int16)
        self._write_tif(path, data, nodata=None)

        z = backend._get_elevation_from_path([0.5], [0.5], path, "bilinear")
        assert z[0] == 1500
