"""Offline regression for the local-DEM elevation backend.

The real backend was live-validated against a Copernicus GLO-30 tile (Snezka
summit read 1601.4 m vs the known 1603 m; the Spindl loop read gain ~= loss) —
see HANDOFF. That needs `rasterio` + a 30 MB tile + network, none of which can
live in the offline suite. These tests pin the same logic deterministically with
tiny synthetic GeoTIFFs written into `tmp_path`, so the regression travels with
the repo. The whole module skips when the optional `local-dem` extra is absent,
keeping the base suite green.

Synthetic tile layout (EPSG:4326, north-up, 0.1 deg pixels):
  - tile A: origin (lon 15.0, lat 51.0), 4x4, value = 1000 + 10*row + col
  - tile B: origin (lon 15.4, lat 51.0), 4x4, value = 2000 + 10*row + col  (east neighbour)
The pixel-(r,c) CENTRE is lon = west + (c+0.5)*res, lat = north - (r+0.5)*res,
and the provider does nearest-cell sampling, so a centre reads back its cell value.
"""
from __future__ import annotations

import pytest

rasterio = pytest.importorskip("rasterio")  # skip module without the local-dem extra
np = pytest.importorskip("numpy")  # rasterio's transitive dep; guard it too so a
# [dev]-only install (no local-dem extra) skips this module instead of erroring on
# collection — numpy is not a declared dependency, it only arrives via rasterio.
from rasterio.transform import from_origin  # noqa: E402

from hike_finder.elevation.base import ElevationError  # noqa: E402
from hike_finder.elevation.local_dem import LocalDemElevationProvider  # noqa: E402

RES = 0.1
NODATA = -9999.0


def _write_tile(path, west: float, north: float, data: np.ndarray) -> None:
    transform = from_origin(west, north, RES, RES)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=NODATA,
    ) as dst:
        dst.write(data.astype("float32"), 1)


def _centre(west: float, north: float, row: int, col: int):
    """(lat, lon) of the centre of pixel (row, col) in a tile at (west, north)."""
    return (north - (row + 0.5) * RES, west + (col + 0.5) * RES)


def _grid(base: float):
    return np.array([[base + 10 * r + c for c in range(4)] for r in range(4)], dtype="float32")


@pytest.fixture
def two_tiles(tmp_path):
    """Two adjacent tiles (A west, B east); A[0,0] is nodata. Returns the dir."""
    a = _grid(1000.0)
    a[0, 0] = NODATA
    _write_tile(tmp_path / "a_tile.tif", 15.0, 51.0, a)
    _write_tile(tmp_path / "b_tile.tif", 15.4, 51.0, _grid(2000.0))
    return str(tmp_path)


def test_known_cell_values_round_trip(two_tiles):
    prov = LocalDemElevationProvider(two_tiles)
    # A few interior cells of tile A (avoid the nodata corner).
    assert prov.lookup([_centre(15.0, 51.0, 1, 2)]) == [1012.0]
    assert prov.lookup([_centre(15.0, 51.0, 3, 3)]) == [1033.0]


def test_merge_seam_picks_the_right_tile(two_tiles):
    # A point in tile B's footprint must read B's values, proving the in-memory
    # mosaic stitches the two tiles and the affine maps each point to its tile.
    prov = LocalDemElevationProvider(two_tiles)
    assert prov.lookup([_centre(15.4, 51.0, 1, 2)]) == [2012.0]
    # One call spanning both tiles preserves order and length across the seam.
    assert prov.lookup(
        [_centre(15.0, 51.0, 0, 1), _centre(15.4, 51.0, 2, 0)]
    ) == [1001.0, 2020.0]


def test_nodata_cell_raises_without_fill(two_tiles):
    prov = LocalDemElevationProvider(two_tiles)
    with pytest.raises(ElevationError):
        prov.lookup([_centre(15.0, 51.0, 0, 0)])  # the seeded nodata cell


def test_nodata_cell_returns_fill_when_configured(two_tiles):
    prov = LocalDemElevationProvider(two_tiles, nodata_fill=-1.0)
    assert prov.lookup([_centre(15.0, 51.0, 0, 0)]) == [-1.0]


def test_out_of_coverage_raises_without_fill(two_tiles):
    prov = LocalDemElevationProvider(two_tiles)
    with pytest.raises(ElevationError):
        prov.lookup([(60.0, 20.0)])  # far outside both tiles


def test_out_of_coverage_returns_fill_when_configured(two_tiles):
    prov = LocalDemElevationProvider(two_tiles, nodata_fill=0.0)
    assert prov.lookup([(60.0, 20.0)]) == [0.0]


def test_empty_dir_raises(tmp_path):
    with pytest.raises(ElevationError, match="no .tif"):
        LocalDemElevationProvider(str(tmp_path))
