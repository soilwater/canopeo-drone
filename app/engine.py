"""
engine.py — Canopeo Drone processing engine (UI-agnostic).

Green canopy cover (Patrignani & Ochsner, 2015) for drone / satellite
imagery. Designed around three facts:

  * Orthomosaics can be multi-GB, so every full-resolution pass streams
    the raster in row strips and never loads it whole.
  * Canopeo was developed for standard 8-bit RGB images, so that is the only
    pixel format accepted. Pixel values are classified exactly as stored;
    nothing is rescaled or converted, and other formats are rejected at load.
  * Stitched borders / nodata must not count. Validity comes from the
    dataset mask (alpha / nodata / internal mask) plus an all-zero test.

A Session caches the cheap things (metadata, a display overview
already reprojected to WGS84) so the UI can re-preview instantly; the
expensive full-resolution numbers are separate calls meant for a
background task.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import rasterio
import rasterio.mask
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from rasterio.windows import transform as _window_transform

GEOTIFF_EXTS = (".tif", ".tiff")

# Canopeo Drone is for georeferenced orthomosaics only. Plain photos belong in
# Canopeo Drag&Drop, so anything without coordinates is rejected at load.
NOT_GEOTIFF_MSG = (
    "Canopeo Drone works with georeferenced GeoTIFF orthomosaics (.tif). "
    "For regular photos (JPEG, PNG, camera RAW), use Canopeo Drag&Drop.")
NO_CRS_MSG = (
    "This GeoTIFF has no coordinate system, so it cannot be placed on the map. "
    "Canopeo Drone needs a georeferenced orthomosaic; for plain images use "
    "Canopeo Drag&Drop.")
NOT_RGB8_MSG = (
    "Canopeo Drone needs a standard 8-bit RGB orthomosaic (uint8, at least "
    "3 bands). This file has {bands} band(s) of type {dtype}. Export the "
    "orthomosaic from your photogrammetry software as 8-bit RGB.")


@dataclass
class Params:
    rg: float = 0.95        # R/G threshold
    bg: float = 0.95        # B/G threshold
    exg: float = 20.0       # Excess Green threshold (8-bit units)
    blend: float = 0.55     # 0 = original image, 1 = solid mask color
    color: str = "#00ff00"  # mask color


# ── Classification ──────────────────────────────────────────────────────────

def canopeo_mask(R, G, B, p: Params) -> np.ndarray:
    R = R.astype(np.float32)
    G = G.astype(np.float32)
    B = B.astype(np.float32)
    G_safe = np.where(G == 0, 1.0, G)
    return (R / G_safe < p.rg) & (B / G_safe < p.bg) & ((2.0 * G - R - B) > p.exg)


def _hex_rgb(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def blend_rgb(rgb: np.ndarray, mask: np.ndarray, p: Params) -> np.ndarray:
    """rgb (H,W,3) uint8 -> blended (H,W,3) uint8 (canopy tinted, rest dimmed)."""
    out = rgb.astype(np.float32)
    col = _hex_rgb(p.color)
    out[mask] = (1 - p.blend) * out[mask] + p.blend * col
    out[~mask] *= (1 - p.blend * 0.35)
    return np.clip(out, 0, 255).astype(np.uint8)


# ── Band mapping & validity ─────────────────────────────────────────────────

def detect_rgb_indices(ds) -> tuple:
    """Zero-based (R, G, B) band positions: the file's color tags when
    present, else bands 1/2/3. Callers guarantee ds.count >= 3."""
    ci = [c.name for c in ds.colorinterp]
    if "red" in ci and "green" in ci and "blue" in ci:
        return ci.index("red"), ci.index("green"), ci.index("blue")
    return (0, 1, 2)


def _validity(ds, chw, window=None, out_shape=None) -> np.ndarray:
    """dataset mask (alpha / nodata / mask band) AND not-all-zero."""
    kw = {}
    if window is not None:
        kw["window"] = window
    if out_shape is not None:
        kw["out_shape"] = out_shape
    dm = ds.dataset_mask(**kw) > 0
    return dm & ~np.all(chw == 0, axis=0)


# ── Session ─────────────────────────────────────────────────────────────────

@dataclass
class Session:
    path: str
    name: str
    crs: str
    width: int
    height: int
    bands: int
    dtype: str
    rgb_idx: tuple
    file_mb: float
    gsd: str                              # ground sample distance text
    projected: bool                       # projected CRS (areas possible)
    px_area_m2: Optional[float]           # None for geographic CRSs
    ov_rgb: np.ndarray                    # (H,W,3) uint8 display overview
    ov_valid: np.ndarray                  # (H,W) bool
    bounds: tuple                         # ((s,w),(n,e)) WGS84
    # Analysis boundary (AOI) — set via set_aoi(); None means the whole image.
    aoi: object = None                    # WGS84 shapely geometry
    aoi_crs: object = None                # the same geometry in the raster CRS
    ov_aoi: object = None                 # overview-resolution bool mask

    @property
    def center(self):
        (s, w), (n, e) = self.bounds
        return ((s + n) / 2, (w + e) / 2)


def _unit_m(crs) -> float:
    """Meters per CRS linear unit (1.0 for meters, 0.3048... for feet)."""
    return float(crs.linear_units_factor[1])


def _gsd_text(ds) -> str:
    rx, ry = ds.res
    if ds.crs.is_projected:
        g = (abs(rx) + abs(ry)) / 2 * _unit_m(ds.crs)
    else:
        lat = (ds.bounds.top + ds.bounds.bottom) / 2
        gx = abs(rx) * 111_320 * np.cos(np.radians(lat))
        gy = abs(ry) * 110_540
        g = (gx + gy) / 2
    return f"{g * 100:.2f} cm/px" if g < 1 else f"{g:.2f} m/px"


# ── Analysis boundary (AOI) ──────────────────────────────────────────────────
# One boundary per session limits every cover calculation to an area of
# interest, so drone imagery of roads / neighbouring fields is excluded. It is
# non-destructive: nothing is written, it is just an extra validity mask that
# every path (full pass, tiles, preview, areas, mask export) honours.

def _reproject_geom(geom_wgs84, crs):
    """Reproject a WGS84 shapely geometry into `crs`."""
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    return shp_transform(fwd, geom_wgs84)


def _overview_aoi_mask(s, geom_wgs84):
    """Bool mask (True inside the AOI) at the display-overview resolution."""
    (south, west), (north, east) = s.bounds
    oh, ow = s.ov_valid.shape
    t = from_bounds(west, south, east, north, ow, oh)
    return geometry_mask([geom_wgs84], out_shape=(oh, ow), transform=t,
                         invert=True)


def set_aoi(s: "Session", shape) -> None:
    """Set (a drawn-shape dict) or clear (None) the analysis boundary."""
    if not shape:
        s.aoi = s.aoi_crs = s.ov_aoi = None
        return
    geom = shape_geometry_wgs84(shape)
    s.aoi = geom
    s.aoi_crs = _reproject_geom(geom, s.crs)
    s.ov_aoi = _overview_aoi_mask(s, geom)


def aoi_geojson(shape) -> dict:
    """FeatureCollection (WGS84) to draw the boundary outline on the map."""
    from shapely.geometry import mapping
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": mapping(shape_geometry_wgs84(shape))}]}


def _win_aoi_mask(aoi_crs, win, transform):
    """Bool mask (True inside the AOI) for one read window."""
    return geometry_mask([aoi_crs], out_shape=(int(win.height), int(win.width)),
                         transform=_window_transform(win, transform), invert=True)


def _aoi_row_range(ds, aoi_crs):
    """[row0, row1) the AOI bounding box spans, so strips clear of it are
    skipped entirely (the boundary's whole point is trimming edges)."""
    minx, miny, maxx, maxy = aoi_crs.bounds
    r_top, _ = ds.index(minx, maxy)
    r_bot, _ = ds.index(minx, miny)
    r0, r1 = sorted((int(r_top), int(r_bot)))
    return max(0, r0), min(ds.height, r1 + 1)


def open_session(path: str, max_dim: int = 1400) -> Session:
    """Open a georeferenced 8-bit RGB GeoTIFF orthomosaic. Rejects anything else."""
    name = os.path.basename(path)
    if os.path.splitext(path)[1].lower() not in GEOTIFF_EXTS:
        raise RuntimeError(NOT_GEOTIFF_MSG)
    file_mb = os.path.getsize(path) / 1e6

    with rasterio.open(path) as ds:
        if ds.crs is None:
            raise RuntimeError(NO_CRS_MSG)
        if ds.count < 3 or any(dt != "uint8" for dt in ds.dtypes):
            raise RuntimeError(NOT_RGB8_MSG.format(
                bands=ds.count, dtype="/".join(sorted(set(ds.dtypes)))))
        rgb_idx = detect_rgb_indices(ds)
        bands = tuple(i + 1 for i in rgb_idx)

        with WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.nearest) as vrt:
            sc = min(1.0, max_dim / max(vrt.width, vrt.height))
            oh, ow = max(1, int(vrt.height * sc)), max(1, int(vrt.width * sc))
            chw = vrt.read(bands, out_shape=(3, oh, ow))
            valid = _validity(vrt, chw, out_shape=(oh, ow))
            b = vrt.bounds
            bounds = ((b.bottom, b.left), (b.top, b.right))

        ov_rgb = np.ascontiguousarray(np.transpose(chw, (1, 2, 0)))
        projected = bool(ds.crs.is_projected)
        px_area = (abs(ds.res[0] * ds.res[1]) * _unit_m(ds.crs) ** 2
                   if projected else None)
        return Session(path, name, ds.crs.to_string(),
                       ds.width, ds.height, ds.count, ds.dtypes[0], rgb_idx,
                       file_mb, _gsd_text(ds), projected, px_area,
                       ov_rgb, valid, bounds)



def set_rgb_idx(s: Session, rgb_idx) -> None:
    """Change which bands are R/G/B and rebuild the cached display overview.
    full_cover / tiles / areas read s.rgb_idx directly, so they pick the change
    up on their next run; only the overview (ov_rgb / ov_valid) is cached and is
    re-read here at the overview's existing size."""
    s.rgb_idx = tuple(int(i) for i in rgb_idx)
    bands = tuple(i + 1 for i in s.rgb_idx)
    oh, ow = s.ov_valid.shape
    with rasterio.open(s.path) as ds:
        with WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.nearest) as vrt:
            chw = vrt.read(bands, out_shape=(3, oh, ow))
            valid = _validity(vrt, chw, out_shape=(oh, ow))
    s.ov_rgb = np.ascontiguousarray(np.transpose(chw, (1, 2, 0)))
    s.ov_valid = valid


# ── Fast preview (overview) ─────────────────────────────────────────────────

def preview(s: Session, p: Params) -> tuple:
    """Return (PNG bytes with transparent invalid pixels, preview cover %)."""
    from PIL import Image
    rgb = s.ov_rgb
    valid = s.ov_valid if s.ov_aoi is None else (s.ov_valid & s.ov_aoi)
    m = canopeo_mask(rgb[..., 0], rgb[..., 1], rgb[..., 2], p) & valid
    valid_n = int(valid.sum())
    cover = (int(m.sum()) / valid_n * 100.0) if valid_n else 0.0
    blended = blend_rgb(rgb, m, p)
    rgba = np.dstack([blended, (valid * 255).astype(np.uint8)])
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=3)
    return buf.getvalue(), cover


# ── Full-resolution cover (streaming) ───────────────────────────────────────

STRIP_PIXELS = 16_000_000     # per-strip budget; bounds memory on wide mosaics


def _iter_strips(ds, strip_pixels=STRIP_PIXELS):
    """Full-width row strips of about strip_pixels each (at most 1024 rows), so
    peak memory per worker does not grow with the width of the mosaic."""
    strip_rows = max(1, min(1024, strip_pixels // max(1, ds.width)))
    for row0 in range(0, ds.height, strip_rows):
        h = min(strip_rows, ds.height - row0)
        yield Window(0, row0, ds.width, h)


def _count_strip(ds, win, bands, p, aoi=None):
    """(green, valid) pixel counts for one row strip."""
    chw = ds.read(bands, window=win)
    vmask = _validity(ds, chw, window=win)
    if aoi is not None:
        vmask &= _win_aoi_mask(aoi, win, ds.transform)
    m = canopeo_mask(chw[0], chw[1], chw[2], p) & vmask
    return int(m.sum()), int(vmask.sum())


def _cover_worker(path, wins, bands, p, tick, aoi=None):
    """Process a subset of strips on its own dataset handle. GDAL datasets are
    not thread-safe to share, so each worker opens the file independently; the
    reads (GDAL) and numpy work then run in parallel across cores."""
    green = valid = 0
    with rasterio.open(path) as ds:
        for win in wins:
            g, v = _count_strip(ds, win, bands, p, aoi)
            green += g
            valid += v
            tick()
    return green, valid


def full_cover(s: Session, p: Params, progress=None, workers: int = None) -> dict:
    """Whole-image cover at full resolution. progress(fraction) optional.

    The raster is read in row strips spread across worker threads. GDAL
    releases the GIL during reads and numpy during the vectorized
    classification, so on a multi-core machine the decompress + classify work
    overlaps and the pass runs roughly N× faster (bounded by disk/decode)."""
    import concurrent.futures as cf
    import threading

    bands = tuple(i + 1 for i in s.rgb_idx)
    aoi = s.aoi_crs
    with rasterio.open(s.path) as ds:
        wins = list(_iter_strips(ds))
        if aoi is not None:
            r0, r1 = _aoi_row_range(ds, aoi)
            wins = [w for w in wins
                    if w.row_off < r1 and w.row_off + w.height > r0]
    n = max(1, len(wins))
    if workers is None:
        workers = min(os.cpu_count() or 4, 8)
    workers = max(1, min(workers, n))

    lock = threading.Lock()
    seen = [0]

    def tick():
        if progress:
            with lock:
                seen[0] += 1
                progress(seen[0] / n)

    green = valid = 0
    if workers == 1:
        with rasterio.open(s.path) as ds:
            for win in wins:
                g, v = _count_strip(ds, win, bands, p, aoi)
                green += g
                valid += v
                tick()
    else:
        # round-robin so the shorter tail strip is spread, not piled on one worker
        chunks = [wins[i::workers] for i in range(workers)]
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_cover_worker, s.path, ch, bands, p, tick, aoi)
                    for ch in chunks if ch]
            for fut in cf.as_completed(futs):
                g, v = fut.result()
                green += g
                valid += v

    out = {"cover": green / valid * 100.0 if valid else 0.0,
           "green_px": green, "valid_px": valid}
    if s.px_area_m2:
        out["green_m2"] = green * s.px_area_m2
        out["valid_m2"] = valid * s.px_area_m2
    return out


# ── Exports ─────────────────────────────────────────────────────────────────

def save_mask(s: Session, p: Params, out_path: str) -> str:
    """Binary canopy mask as a GeoTIFF (1=canopy, 0=other, 255=nodata)."""
    if not out_path.lower().endswith((".tif", ".tiff")):
        out_path += ".tif"
    if os.path.exists(out_path) and os.path.samefile(out_path, s.path):
        raise RuntimeError("The mask cannot be saved over the orthomosaic "
                           "itself. Choose a different file name.")
    bands = tuple(i + 1 for i in s.rgb_idx)
    aoi = s.aoi_crs
    with rasterio.open(s.path) as ds:
        profile = {"driver": "GTiff", "height": ds.height, "width": ds.width,
                   "count": 1, "dtype": "uint8", "crs": ds.crs,
                   "transform": ds.transform, "nodata": 255,
                   "compress": "lzw", "tiled": True,
                   "blockxsize": 512, "blockysize": 512}
        with rasterio.open(out_path, "w", **profile) as dst:
            for win in _iter_strips(ds):
                chw = ds.read(bands, window=win)
                vmask = _validity(ds, chw, window=win)
                if aoi is not None:
                    vmask &= _win_aoi_mask(aoi, win, ds.transform)
                m = canopeo_mask(chw[0], chw[1], chw[2], p) & vmask
                out = np.where(vmask, m.astype(np.uint8), 255).astype(np.uint8)
                dst.write(out, 1, window=win)
    return out_path


def save_rows_csv(rows: list, out_path: str) -> str:
    import csv
    if not out_path.lower().endswith(".csv"):
        out_path += ".csv"
    keys = ["plot", "cover_pct", "valid_px", "green_px", "area_m2", "green_m2",
            "rg_threshold", "bg_threshold", "exg_threshold"]
    keys = [k for k in keys if any(k in r for r in rows)]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return out_path


def save_geojson(fc: dict, out_path: str) -> str:
    if not out_path.lower().endswith((".geojson", ".json")):
        out_path += ".geojson"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    return out_path


# ── Areas: drawn shapes and file plots in one model ─────────────────────────
# A "shape" is what guile's draw tools deliver:
#   {"type": "polygon"|"rectangle", "coords": [[lat, lon], ...]}
#   {"type": "circle", "coords": {"lat", "lng", "radius"}}   (radius in meters)

def shape_geometry_wgs84(shape: dict):
    """shapely geometry (EPSG:4326) for a drawn shape."""
    from shapely.geometry import Polygon, Point
    from shapely.ops import transform as shp_transform
    from pyproj import CRS, Transformer
    t, c = shape["type"], shape["coords"]
    if t == "circle":
        lat, lon, r = float(c["lat"]), float(c["lng"]), float(c["radius"])
        aeqd = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +units=m")
        back = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True).transform
        return shp_transform(back, Point(0, 0).buffer(r, 64))
    ring = [(float(p[1]), float(p[0])) for p in c]        # lat,lon -> x,y
    if len(ring) < 3:
        raise ValueError("polygon needs at least 3 points")
    return Polygon(ring)


def import_geojson_shapes(path: str) -> tuple:
    """GeoJSON file -> (shapes, warnings). Shapes are WGS84 polygons with a
    'name'. A shape is a single outer ring, so multi-part features keep only
    their largest part and interior holes are dropped; warnings says so."""
    import geopandas as gpd
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    shapes = []
    n_multi = n_holes = n_skipped = 0
    for i, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiPolygon":
            n_multi += len(geom.geoms) > 1
            geom = max(geom.geoms, key=lambda g: g.area)
        if geom.geom_type != "Polygon":
            n_skipped += 1
            continue
        n_holes += len(geom.interiors) > 0
        props = {k: v for k, v in row.items() if k != "geometry"}
        name = props.get("plot_id", props.get("id", props.get("name", f"plot {i + 1}")))
        # *_ swallows an optional Z coordinate (common in survey / KML exports)
        coords = [[float(y), float(x)] for x, y, *_ in geom.exterior.coords]
        shapes.append({"type": "polygon", "coords": coords, "name": str(name)})
    warnings = []
    if n_multi:
        warnings.append(f"{n_multi} multi-part feature(s): only the largest "
                        "part of each was kept.")
    if n_holes:
        warnings.append(f"{n_holes} polygon(s) have holes: the holes are "
                        "ignored, so their interior is counted.")
    if n_skipped:
        warnings.append(f"{n_skipped} non-polygon feature(s) skipped.")
    return shapes, warnings


def areas_cover(s: Session, shapes: list, p: Params) -> list:
    """Cover for each shape (WGS84) at full resolution. Returns list of dicts.
    Each result carries the thresholds it was computed with, so exports stay
    self-describing even if the sliders move afterwards."""
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    bands = [i + 1 for i in s.rgb_idx]
    out = []
    with rasterio.open(s.path) as ds:
        nd = ds.nodata if ds.nodata is not None else 0
        fwd = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        for sh in shapes:
            try:
                geom = shp_transform(fwd, shape_geometry_wgs84(sh))
                if s.aoi_crs is not None:
                    geom = geom.intersection(s.aoi_crs)
                    if geom.is_empty:
                        raise ValueError    # area lies outside the boundary
                arr, _ = rasterio.mask.mask(ds, [geom], crop=True, indexes=bands,
                                            filled=True, nodata=nd)
                vmask = ~np.all(arr == nd, axis=0) & ~np.all(arr == 0, axis=0)
                m = canopeo_mask(arr[0], arr[1], arr[2], p) & vmask
                v, g = int(vmask.sum()), int(m.sum())
                cover = round(g / v * 100.0, 2) if v else None
            except ValueError:                 # outside the raster
                v, g, cover = 0, 0, None
            row = {"cover_pct": cover, "valid_px": v, "green_px": g,
                   "rg_threshold": p.rg, "bg_threshold": p.bg,
                   "exg_threshold": p.exg}
            if s.px_area_m2:        # always set, so a re-run clears old values
                row["area_m2"] = round(v * s.px_area_m2, 1) if v else None
                row["green_m2"] = round(g * s.px_area_m2, 1) if v else None
            out.append(row)
    return out


def areas_to_geojson(areas: list) -> dict:
    """FeatureCollection (WGS84) of area shapes with their results."""
    from shapely.geometry import mapping
    feats = []
    for a in areas:
        props = {"name": a.get("name"), "canopy_cover_pct": a.get("cover_pct"),
                 "area_m2": a.get("area_m2"), "green_m2": a.get("green_m2"),
                 "valid_px": a.get("valid_px"), "green_px": a.get("green_px"),
                 "rg_threshold": a.get("rg_threshold"),
                 "bg_threshold": a.get("bg_threshold"),
                 "exg_threshold": a.get("exg_threshold"),
                 "source": a.get("source", "drawn")}
        feats.append({"type": "Feature", "properties": props,
                      "geometry": mapping(shape_geometry_wgs84(a))})
    return {"type": "FeatureCollection", "features": feats}
