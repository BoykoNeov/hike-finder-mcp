"""Local DEM elevation: sample SRTM/ASTER/Copernicus GeoTIFF tiles on disk.

Accurate and fast (no rate limits); you download the DEM tiles for your region
once and store them as GeoTIFF in `dem_dir`. SRTM/ASTER 1-arc-second (~30 m) and
Copernicus GLO-30 tiles are freely available.

Multi-tile regions are mosaicked through a GDAL **VRT** — a lightweight XML
"virtual raster" that references the tiles in place — which is then point-sampled.
Only the pixels under each query point are read, so memory stays flat no matter
how large the region. (The previous implementation called `rasterio.merge` to
build the whole mosaic in memory, which doesn't scale past a tile or two.)

We construct the VRT XML directly from the tiles' georeferencing rather than
shelling out to `gdalbuildvrt`: rasterio doesn't wrap GDAL's VRT builder, and
neither that CLI nor the `osgeo` Python bindings ship with the `local-dem`
(rasterio) extra. The result is the same virtual raster `gdalbuildvrt` would emit
for a directory of homogeneous single-band tiles. If you'd rather build your own
(e.g. mixed resolutions needing resampling), drop a `*.vrt` into `dem_dir` and it
is used as-is.

The VRT also fixes a nodata bug in the old merge, which read nodata from the
first tile only (`srcs[0].nodata`): a void/ocean pixel in any other tile then
leaked a raw value. Each VRT source now declares its own nodata, masked against a
single band nodata value, so voids in any tile resolve correctly.

NOTE: requires `rasterio` (heavy, GDAL-backed); install the `local-dem` extra.
"""
from __future__ import annotations

import glob
import math
import os
import xml.etree.ElementTree as ET

from .base import Coord, ElevationError, ElevationProvider

try:
    import rasterio  # type: ignore

    _HAVE_RASTERIO = True
except Exception:  # pragma: no cover - optional heavy dep
    _HAVE_RASTERIO = False


# rasterio dtype string -> GDAL VRT dataType attribute.
_GDAL_DTYPE = {
    "uint8": "Byte",
    "int8": "Int8",
    "uint16": "UInt16",
    "int16": "Int16",
    "uint32": "UInt32",
    "int32": "Int32",
    "float32": "Float32",
    "float64": "Float64",
}


def _fmt(v: float) -> str:
    # float() coercion strips any numpy scalar so the XML never contains
    # 'np.float32(...)'; repr() gives the shortest round-tripping form.
    return repr(float(v))


def _build_vrt_doc(tiles: list[str]) -> str:
    """Return GDAL VRT XML mosaicking `tiles`, built from their georeferencing.

    Equivalent to `gdalbuildvrt` over a directory of homogeneous single-band DEM
    tiles. Assumes a common CRS and pixel resolution on a shared grid (true for a
    single DEM product); raises :class:`ElevationError` on mixed CRS/resolution
    so the caller is told to supply their own ``.vrt`` instead of getting a
    silently misregistered mosaic.
    """
    metas = []
    for t in tiles:
        with rasterio.open(t) as ds:
            metas.append(
                {
                    "path": os.path.abspath(t),
                    "w": ds.width,
                    "h": ds.height,
                    "bounds": ds.bounds,
                    "res": ds.res,
                    "crs": ds.crs,
                    "dtype": ds.dtypes[0],
                    "nodata": ds.nodata,
                    "block": ds.block_shapes[0],  # (rows, cols)
                }
            )

    crs0 = metas[0]["crs"]
    xres, yres = metas[0]["res"]
    for m in metas[1:]:
        if m["crs"] != crs0:
            raise ElevationError(
                "DEM tiles have mixed CRS; mosaic them yourself with gdalbuildvrt "
                "and place the .vrt in the DEM directory"
            )
        if not (
            math.isclose(m["res"][0], xres, rel_tol=1e-6)
            and math.isclose(m["res"][1], yres, rel_tol=1e-6)
        ):
            raise ElevationError(
                "DEM tiles have mixed resolution; mosaic them yourself with "
                "gdalbuildvrt and place the .vrt in the DEM directory"
            )

    minx = min(m["bounds"].left for m in metas)
    maxx = max(m["bounds"].right for m in metas)
    miny = min(m["bounds"].bottom for m in metas)
    maxy = max(m["bounds"].top for m in metas)
    width = int(round((maxx - minx) / xres))
    height = int(round((maxy - miny) / yres))

    gdtype = _GDAL_DTYPE.get(metas[0]["dtype"], "Float32")
    # One band nodata for the mosaic; each source masks its own sentinel against
    # it (see below), so heterogeneous source nodata values all resolve to this
    # single value that lookup() then compares against. None if no tile sets one.
    src_nodatas = [m["nodata"] for m in metas if m["nodata"] is not None]
    band_nodata = src_nodatas[0] if src_nodatas else None

    root = ET.Element("VRTDataset", rasterXSize=str(width), rasterYSize=str(height))
    ET.SubElement(root, "SRS").text = crs0.to_wkt()
    # Affine: north-up, origin at (minx, maxy), pixel (xres, -yres).
    ET.SubElement(root, "GeoTransform").text = ", ".join(
        _fmt(v) for v in (minx, xres, 0.0, maxy, 0.0, -yres)
    )
    band = ET.SubElement(root, "VRTRasterBand", dataType=gdtype, band="1")
    if band_nodata is not None:
        ET.SubElement(band, "NoDataValue").text = _fmt(band_nodata)
    for m in metas:
        dxoff = int(round((m["bounds"].left - minx) / xres))
        dyoff = int(round((maxy - m["bounds"].top) / yres))
        # ComplexSource (vs SimpleSource) so the per-source <NODATA> below is
        # honoured. SourceProperties lets GDAL defer opening the tile until a
        # window is actually read — key to staying memory-flat over a big region.
        src = ET.SubElement(band, "ComplexSource")
        ET.SubElement(src, "SourceFilename", relativeToVRT="0").text = m["path"]
        ET.SubElement(src, "SourceBand").text = "1"
        ET.SubElement(
            src,
            "SourceProperties",
            RasterXSize=str(m["w"]),
            RasterYSize=str(m["h"]),
            DataType=_GDAL_DTYPE.get(m["dtype"], "Float32"),
            BlockXSize=str(m["block"][1]),
            BlockYSize=str(m["block"][0]),
        )
        ET.SubElement(src, "SrcRect", xOff="0", yOff="0", xSize=str(m["w"]), ySize=str(m["h"]))
        ET.SubElement(
            src, "DstRect", xOff=str(dxoff), yOff=str(dyoff), xSize=str(m["w"]), ySize=str(m["h"])
        )
        if m["nodata"] is not None:
            ET.SubElement(src, "NODATA").text = _fmt(m["nodata"])
    return ET.tostring(root, encoding="unicode")


class LocalDemElevationProvider(ElevationProvider):
    def __init__(self, dem_dir: str, nodata_fill: float | None = None):
        if not _HAVE_RASTERIO:
            raise ElevationError(
                "rasterio is required for LocalDemElevationProvider; "
                "install with: pip install rasterio"
            )
        self.dem_dir = dem_dir
        self.nodata_fill = nodata_fill
        # A user-supplied .vrt wins: a power user can mosaic with gdalbuildvrt and
        # control resampling/overlap themselves. Otherwise build one over the
        # GeoTIFF tiles. `self._source` is whatever rasterio.open() accepts — a
        # path to the .vrt, or the generated VRT XML document inline.
        vrts = sorted(glob.glob(os.path.join(dem_dir, "*.vrt")))
        if vrts:
            self._source: str = os.path.abspath(vrts[0])
        else:
            tiles = sorted(glob.glob(os.path.join(dem_dir, "*.tif")))
            if not tiles:
                raise ElevationError(f"no .tif DEM tiles (or .vrt) found in {dem_dir}")
            self._source = _build_vrt_doc(tiles)
        # Fail fast if the source can't be opened (malformed .vrt, missing tile,
        # unreadable dir) rather than at first lookup.
        with rasterio.open(self._source):
            pass

    def lookup(self, points: list[Coord]) -> list[float]:
        out: list[float] = [0.0] * len(points)
        # Re-open per call: opening a VRT only parses the XML (tiles open lazily
        # per window), so this is cheap, and it avoids holding file handles for
        # the provider's lifetime. lookup() is called batched, not per-point.
        with rasterio.open(self._source) as src:
            rows, cols = src.height, src.width
            nodata = src.nodata
            # Decide coverage with our own extent check, NOT sample(): a point off
            # the mosaic, sampled from a DEM whose tiles declare no nodata, comes
            # back as 0.0 — a valid sea-level reading, indistinguishable from real
            # data. So bounds-check every point, sample only the in-bounds ones,
            # then scatter results back by original index (order/length preserved).
            in_bounds: list[tuple[int, tuple[float, float]]] = []
            for i, (lat, lon) in enumerate(points):
                row, col = src.index(lon, lat)
                if 0 <= row < rows and 0 <= col < cols:
                    in_bounds.append((i, (lon, lat)))
                else:
                    out[i] = self._handle_nodata()
            if in_bounds:
                xy = [c for _, c in in_bounds]
                for (i, _), sampled in zip(in_bounds, src.sample(xy)):
                    val = float(sampled[0])
                    if nodata is not None and val == nodata:
                        val = self._handle_nodata()
                    out[i] = val
        return out

    def _handle_nodata(self) -> float:
        if self.nodata_fill is not None:
            return self.nodata_fill
        raise ElevationError("point falls on DEM nodata / outside tile coverage")
