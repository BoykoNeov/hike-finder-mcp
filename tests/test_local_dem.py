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


def _write_tile(
    path,
    west: float,
    north: float,
    data: np.ndarray,
    *,
    res: float = RES,
    crs: str = "EPSG:4326",
    nodata: float | None = NODATA,
) -> None:
    transform = from_origin(west, north, res, res)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
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


# --- VRT mosaic specifics (the in-memory merge -> GDAL VRT change) ---


def test_single_tile_still_works(tmp_path):
    # Scaling to many tiles is the point, but the one-tile region must still work
    # (the VRT then wraps a single source).
    _write_tile(tmp_path / "only.tif", 15.0, 51.0, _grid(1000.0))
    prov = LocalDemElevationProvider(str(tmp_path))
    assert prov.lookup([_centre(15.0, 51.0, 1, 2)]) == [1012.0]


def test_overlapping_tiles_no_seam_and_top_wins(tmp_path):
    """Two tiles overlapping by one column must mosaic without a phantom nodata
    seam, with the later-listed tile painting over the overlap (gdalbuildvrt's
    rule). An off-by-one in the dst-offset rounding would surface here as a 1-px
    nodata stripe or a value from the wrong tile."""
    # A at lon 15.0 (cols 15.0..15.4); B at lon 15.3 (cols 15.3..15.7).
    # Union is 7 cols wide; col index 3 (lon ~15.35) is the shared overlap, where
    # B (sorted after A -> on top) must win.
    _write_tile(tmp_path / "a_tile.tif", 15.0, 51.0, _grid(1000.0))
    _write_tile(tmp_path / "b_tile.tif", 15.3, 51.0, _grid(2000.0))
    prov = LocalDemElevationProvider(str(tmp_path))

    lat = 51.0 - 1.5 * RES  # row 1 of both tiles
    lons = [15.0 + (c + 0.5) * RES for c in range(7)]  # 7 union-column centres
    vals = prov.lookup([(lat, lon) for lon in lons])

    # cols 0-2 = A row1 (1010,1011,1012); col 3 = overlap -> B row1 col0 (2010);
    # cols 4-6 = B row1 (2011,2012,2013). No NODATA anywhere across the seam.
    assert vals == [1010.0, 1011.0, 1012.0, 2010.0, 2011.0, 2012.0, 2013.0]
    assert NODATA not in vals


def test_mixed_crs_raises(tmp_path):
    _write_tile(tmp_path / "a_tile.tif", 15.0, 51.0, _grid(1000.0))
    _write_tile(tmp_path / "b_tile.tif", 15.4, 51.0, _grid(2000.0), crs="EPSG:3857")
    with pytest.raises(ElevationError, match="mixed CRS"):
        LocalDemElevationProvider(str(tmp_path))


def test_mixed_resolution_raises(tmp_path):
    _write_tile(tmp_path / "a_tile.tif", 15.0, 51.0, _grid(1000.0))
    _write_tile(tmp_path / "b_tile.tif", 15.4, 51.0, _grid(2000.0), res=0.2)
    with pytest.raises(ElevationError, match="mixed resolution"):
        LocalDemElevationProvider(str(tmp_path))


def test_tiles_without_nodata_off_coverage_still_raises(tmp_path):
    # Copernicus-like tiles report nodata=None, so an off-mosaic point sampled
    # from the VRT reads back 0.0 (a valid elevation). The extent bounds-check,
    # not sample(), must catch it.
    _write_tile(tmp_path / "a_tile.tif", 15.0, 51.0, _grid(1000.0), nodata=None)
    prov = LocalDemElevationProvider(str(tmp_path))
    assert prov.lookup([_centre(15.0, 51.0, 1, 2)]) == [1012.0]  # in-coverage ok
    with pytest.raises(ElevationError):
        prov.lookup([(60.0, 20.0)])  # far off mosaic -> not 0.0


def test_mixed_batch_preserves_order_and_scatters_fill(two_tiles):
    # The one new mechanism the VRT rewrite introduced: lookup() bounds-checks
    # every point, samples ONLY the in-bounds ones, then scatters results back by
    # original index. A single call mixing all three categories (in-bounds A,
    # off-mosaic, seeded-nodata, in-bounds B) must keep order and length exact.
    prov = LocalDemElevationProvider(two_tiles, nodata_fill=-1.0)
    pts = [
        _centre(15.0, 51.0, 1, 2),  # in-bounds tile A -> 1012
        (60.0, 20.0),               # off-mosaic -> fill
        _centre(15.0, 51.0, 0, 0),  # seeded nodata cell -> fill
        _centre(15.4, 51.0, 1, 2),  # in-bounds tile B -> 2012
    ]
    assert prov.lookup(pts) == [1012.0, -1.0, -1.0, 2012.0]


def test_user_supplied_vrt_is_used(two_tiles):
    # A .vrt in the directory wins over auto-building from the .tif tiles. Build a
    # valid one with the same generator and confirm the provider samples through it.
    from hike_finder.elevation.local_dem import _build_vrt_doc
    import glob as _glob
    import os as _os

    tiles = sorted(_glob.glob(_os.path.join(two_tiles, "*.tif")))
    with open(_os.path.join(two_tiles, "mosaic.vrt"), "w") as f:
        f.write(_build_vrt_doc(tiles))
    prov = LocalDemElevationProvider(two_tiles)
    assert prov.lookup([_centre(15.4, 51.0, 1, 2)]) == [2012.0]
