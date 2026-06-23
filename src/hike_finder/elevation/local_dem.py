"""Local DEM elevation: sample SRTM/ASTER GeoTIFF tiles on disk.

Accurate and fast (no rate limits), but you download the DEM tiles once for
your region. SRTM 1-arc-second (~30 m) tiles are free from USGS EarthExplorer
or derived mirrors; store them as GeoTIFF in `dem_dir`.

Uses rasterio's sampling, which handles tile CRS/affine transforms. For points
spanning multiple tiles, rasterio.merge or a VRT over the tile directory is the
clean approach — see HANDOFF.md for the VRT TODO.

NOTE: requires `rasterio` (heavy, GDAL-backed). Not exercised in the build
sandbox; wire up and test on your machine.
"""
from __future__ import annotations

import glob
import os

from .base import Coord, ElevationError, ElevationProvider

try:
    import rasterio  # type: ignore
    from rasterio.merge import merge  # type: ignore

    _HAVE_RASTERIO = True
except Exception:  # pragma: no cover - optional heavy dep
    _HAVE_RASTERIO = False


class LocalDemElevationProvider(ElevationProvider):
    def __init__(self, dem_dir: str, nodata_fill: float | None = None):
        if not _HAVE_RASTERIO:
            raise ElevationError(
                "rasterio is required for LocalDemElevationProvider; "
                "install with: pip install rasterio"
            )
        self.dem_dir = dem_dir
        self.nodata_fill = nodata_fill
        tiles = sorted(glob.glob(os.path.join(dem_dir, "*.tif")))
        if not tiles:
            raise ElevationError(f"no .tif DEM tiles found in {dem_dir}")
        # Build one merged in-memory mosaic. For very large regions, prefer a
        # GDAL VRT instead of an in-memory merge (see HANDOFF.md).
        srcs = [rasterio.open(t) for t in tiles]
        self._mosaic, self._transform = merge(srcs)
        self._band = self._mosaic[0]
        self._nodata = srcs[0].nodata
        for s in srcs:
            s.close()

    def lookup(self, points: list[Coord]) -> list[float]:
        import rasterio.transform as rt  # type: ignore

        out: list[float] = []
        rows, cols = self._band.shape
        for lat, lon in points:
            row, col = rt.rowcol(self._transform, lon, lat)
            if 0 <= row < rows and 0 <= col < cols:
                val = float(self._band[row, col])
                if self._nodata is not None and val == self._nodata:
                    val = self._handle_nodata()
                out.append(val)
            else:
                out.append(self._handle_nodata())
        return out

    def _handle_nodata(self) -> float:
        if self.nodata_fill is not None:
            return self.nodata_fill
        raise ElevationError("point falls on DEM nodata / outside tile coverage")
