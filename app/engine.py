"""
engine.py — Canopeo Drone processing engine (UI-agnostic).

Green canopy cover (Patrignani & Ochsner, 2015) for drone / satellite
imagery. Designed around three facts:

  * Orthomosaics can be multi-GB, so every full-resolution pass streams
    the raster in row strips and never loads it whole.
  * Thresholds must behave the same everywhere in the image, so one global
    2-98 percentile stretch (computed once from an overview) maps any bit
    depth to 8-bit before classification. Per-tile stretching would move
    the decision boundary across stitch seams.
  * Stitched borders / nodata must not count. Validity comes from the
    dataset mask (alpha / nodata / internal mask) plus an all-zero test.

A Session caches the cheap things (metadata, scaling, a display overview
already reprojected to WGS84) so the UI can re-preview instantly; the
expensive full-resolution numbers are separate calls meant for a
background task.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import rasterio
import rasterio.mask
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

RASTER_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
RAW_EXTS = (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".rw2", ".orf", ".pef")

# Zero-based (R, G, B) positions per known multispectral sensor (by band count).
SENSOR_PRESETS = {
    8: (5, 3, 1),   # PlanetScope 8-band SR: Red=6, Green=4, Blue=2
    5: (2, 1, 0),   # MicaSense 5-band: Red=3, Green=2, Blue=1
}


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


# ── Band mapping & scaling ──────────────────────────────────────────────────

def detect_rgb_indices(ds) -> tuple:
    ci = [c.name for c in ds.colorinterp]
    if "red" in ci and "green" in ci and "blue" in ci:
        return ci.index("red"), ci.index("green"), ci.index("blue")
    if ds.count in SENSOR_PRESETS:
        return SENSOR_PRESETS[ds.count]
    if ds.count >= 3:
        return (0, 1, 2)
    return (0, 0, 0)


@dataclass
class Scaling:
    lo: np.ndarray
    hi: np.ndarray
    passthrough: bool = False

    def apply(self, chw: np.ndarray) -> np.ndarray:
        if self.passthrough:
            return chw.astype(np.uint8)
        out = np.empty(chw.shape, dtype=np.uint8)
        for c in range(3):
            span = max(float(self.hi[c] - self.lo[c]), 1e-6)
            v = (chw[c].astype(np.float32) - self.lo[c]) * (255.0 / span)
            out[c] = np.clip(v, 0, 255).astype(np.uint8)
        return out


def _validity(ds, chw, window=None, out_shape=None) -> np.ndarray:
    """dataset mask (alpha / nodata / mask band) AND not-all-zero."""
    kw = {}
    if window is not None:
        kw["window"] = window
    if out_shape is not None:
        kw["out_shape"] = out_shape
    dm = ds.dataset_mask(**kw) > 0
    return dm & ~np.all(chw == 0, axis=0)


def compute_scaling(ds, rgb_idx, max_dim=1024) -> Scaling:
    if ds.dtypes[0] == "uint8":
        return Scaling(np.zeros(3), np.full(3, 255.0), passthrough=True)
    sc = min(1.0, max_dim / max(ds.width, ds.height))
    oh, ow = max(1, int(ds.height * sc)), max(1, int(ds.width * sc))
    bands = tuple(i + 1 for i in rgb_idx)
    ov = ds.read(bands, out_shape=(3, oh, ow)).astype(np.float32)
    valid = _validity(ds, ov, out_shape=(oh, ow))
    lo = np.zeros(3, np.float32)
    hi = np.ones(3, np.float32)
    for c in range(3):
        vals = ov[c][valid]
        if vals.size:
            lo[c], hi[c] = np.percentile(vals, (2, 98))
        if hi[c] <= lo[c]:
            hi[c] = lo[c] + 1.0
    return Scaling(lo, hi)


# ── Session ─────────────────────────────────────────────────────────────────

@dataclass
class Session:
    path: str
    name: str
    georeferenced: bool
    crs: Optional[str]
    width: int
    height: int
    bands: int
    dtype: str
    rgb_idx: tuple
    scaling: Scaling
    file_mb: float
    gsd: Optional[str]                    # ground sample distance text
    projected: bool                       # CRS in meters (areas possible)
    px_area_m2: Optional[float]
    ov_rgb: np.ndarray                    # (H,W,3) uint8 display overview
    ov_valid: np.ndarray                  # (H,W) bool
    bounds: Optional[tuple]               # ((s,w),(n,e)) WGS84
    raw_rgb: Optional[np.ndarray] = None  # full (H,W,3) uint8 for RAW files
    notes: list = field(default_factory=list)

    @property
    def center(self):
        if not self.bounds:
            return None
        (s, w), (n, e) = self.bounds
        return ((s + n) / 2, (w + e) / 2)


def is_raw(path) -> bool:
    return os.path.splitext(path)[1].lower() in RAW_EXTS


def _gsd_text(ds) -> Optional[str]:
    if ds.crs is None:
        return None
    rx, ry = ds.res
    if ds.crs.is_projected:
        g = (abs(rx) + abs(ry)) / 2
    else:
        lat = (ds.bounds.top + ds.bounds.bottom) / 2
        gx = abs(rx) * 111_320 * np.cos(np.radians(lat))
        gy = abs(ry) * 110_540
        g = (gx + gy) / 2
    return f"{g * 100:.2f} cm/px" if g < 1 else f"{g:.2f} m/px"


def open_session(path: str, max_dim: int = 1400) -> Session:
    """Open a GeoTIFF / PNG / JPEG (or RAW if rawpy is installed)."""
    name = os.path.basename(path)
    file_mb = os.path.getsize(path) / 1e6

    if is_raw(path):
        try:
            import rawpy  # optional; deliberately not a hard dependency
        except ImportError:
            raise RuntimeError(
                "RAW camera files need the optional 'rawpy' package "
                "(pip install rawpy). GeoTIFF, PNG and JPEG work without it.")
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(no_auto_bright=True, output_bps=8)
        h, w = rgb.shape[:2]
        step = max(1, int(np.ceil(max(w, h) / max_dim)))
        ov = np.ascontiguousarray(rgb[::step, ::step])
        return Session(path, name, False, None, w, h, 3, "uint8", (0, 1, 2),
                       Scaling(np.zeros(3), np.full(3, 255.0), True), file_mb,
                       None, False, None, ov, np.ones(ov.shape[:2], bool),
                       None, raw_rgb=rgb,
                       notes=["RAW photo decoded; not georeferenced."])

    with rasterio.open(path) as ds:
        rgb_idx = detect_rgb_indices(ds)
        scaling = compute_scaling(ds, rgb_idx)
        bands = tuple(i + 1 for i in rgb_idx)
        geo = ds.crs is not None
        notes = []
        if ds.count < 3:
            notes.append("Fewer than 3 bands: canopy classification needs RGB.")
        if not geo:
            notes.append("Not georeferenced: whole-image cover only, no map or areas.")

        if geo:
            with WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.nearest) as vrt:
                sc = min(1.0, max_dim / max(vrt.width, vrt.height))
                oh, ow = max(1, int(vrt.height * sc)), max(1, int(vrt.width * sc))
                chw = vrt.read(bands, out_shape=(3, oh, ow))
                valid = _validity(vrt, chw, out_shape=(oh, ow))
                b = vrt.bounds
                bounds = ((b.bottom, b.left), (b.top, b.right))
        else:
            sc = min(1.0, max_dim / max(ds.width, ds.height))
            oh, ow = max(1, int(ds.height * sc)), max(1, int(ds.width * sc))
            chw = ds.read(bands, out_shape=(3, oh, ow))
            valid = _validity(ds, chw, out_shape=(oh, ow))
            bounds = None

        ov_rgb = np.ascontiguousarray(np.transpose(scaling.apply(chw), (1, 2, 0)))
        projected = bool(geo and ds.crs.is_projected)
        px_area = abs(ds.res[0] * ds.res[1]) if projected else None
        return Session(path, name, geo, ds.crs.to_string() if geo else None,
                       ds.width, ds.height, ds.count, ds.dtypes[0], rgb_idx,
                       scaling, file_mb, _gsd_text(ds), projected, px_area,
                       ov_rgb, valid, bounds, notes=notes)


# ── Fast preview (overview) ─────────────────────────────────────────────────

def preview(s: Session, p: Params) -> tuple:
    """Return (PNG bytes with transparent invalid pixels, preview cover %)."""
    from PIL import Image
    rgb = s.ov_rgb
    m = canopeo_mask(rgb[..., 0], rgb[..., 1], rgb[..., 2], p) & s.ov_valid
    valid_n = int(s.ov_valid.sum())
    cover = (int(m.sum()) / valid_n * 100.0) if valid_n else 0.0
    blended = blend_rgb(rgb, m, p)
    rgba = np.dstack([blended, (s.ov_valid * 255).astype(np.uint8)])
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=3)
    return buf.getvalue(), cover


# ── Full-resolution cover (streaming) ───────────────────────────────────────

def _iter_strips(ds, strip_rows=1024):
    for row0 in range(0, ds.height, strip_rows):
        h = min(strip_rows, ds.height - row0)
        yield Window(0, row0, ds.width, h)


def full_cover(s: Session, p: Params, progress=None) -> dict:
    """Whole-image cover at full resolution. progress(fraction) optional."""
    if s.raw_rgb is not None:
        rgb = s.raw_rgb
        m = canopeo_mask(rgb[..., 0], rgb[..., 1], rgb[..., 2], p)
        g, v = int(m.sum()), int(m.size)
        return {"cover": g / v * 100.0 if v else 0.0, "green_px": g, "valid_px": v}

    bands = tuple(i + 1 for i in s.rgb_idx)
    green = valid = 0
    with rasterio.open(s.path) as ds:
        n = max(1, -(-ds.height // 1024))
        for k, win in enumerate(_iter_strips(ds)):
            chw = ds.read(bands, window=win)
            vmask = _validity(ds, chw, window=win)
            rgb8 = s.scaling.apply(chw)
            m = canopeo_mask(rgb8[0], rgb8[1], rgb8[2], p) & vmask
            green += int(m.sum())
            valid += int(vmask.sum())
            if progress:
                progress((k + 1) / n)
    out = {"cover": green / valid * 100.0 if valid else 0.0,
           "green_px": green, "valid_px": valid}
    if s.px_area_m2:
        out["green_m2"] = green * s.px_area_m2
        out["valid_m2"] = valid * s.px_area_m2
    return out


# ── Per-polygon (zonal) cover ───────────────────────────────────────────────

def zonal_cover(s: Session, geojson_path: str, p: Params) -> tuple:
    """Return (FeatureCollection in WGS84 with canopy_cover_pct, rows)."""
    import geopandas as gpd
    if not s.georeferenced:
        raise RuntimeError("Per-plot cover needs a georeferenced raster.")
    gdf = gpd.read_file(geojson_path)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    bands = [i + 1 for i in s.rgb_idx]
    rows = []
    with rasterio.open(s.path) as ds:
        nd = ds.nodata if ds.nodata is not None else 0
        g = gdf.to_crs(ds.crs)
        for i, geom in enumerate(g.geometry):
            props = {k: v for k, v in gdf.iloc[i].items() if k != "geometry"}
            pid = props.get("plot_id", props.get("id", props.get("name", i + 1)))
            try:
                out, _ = rasterio.mask.mask(ds, [geom], crop=True, indexes=bands,
                                            filled=True, nodata=nd)
                vmask = ~np.all(out == nd, axis=0) & ~np.all(out == 0, axis=0)
                rgb8 = s.scaling.apply(out)
                m = canopeo_mask(rgb8[0], rgb8[1], rgb8[2], p) & vmask
                v, gpx = int(vmask.sum()), int(m.sum())
                cover = round(gpx / v * 100.0, 2) if v else None
            except ValueError:            # polygon does not overlap the raster
                v, gpx, cover = 0, 0, None
            row = {"plot": str(pid), "cover_pct": cover,
                   "valid_px": v, "green_px": gpx}
            if s.px_area_m2 and v:
                row["area_m2"] = round(v * s.px_area_m2, 1)
                row["green_m2"] = round(gpx * s.px_area_m2, 1)
            rows.append(row)
    gdf["canopy_cover_pct"] = [r["cover_pct"] for r in rows]
    fc = json.loads(gdf.to_crs(4326).to_json())
    return fc, rows


# ── Exports ─────────────────────────────────────────────────────────────────

def save_mask(s: Session, p: Params, out_path: str) -> str:
    """Binary canopy mask: GeoTIFF (1=canopy, 0=other, 255=nodata) or PNG."""
    from PIL import Image
    if s.raw_rgb is not None or not s.georeferenced:
        if s.raw_rgb is not None:
            rgb = s.raw_rgb
            m = canopeo_mask(rgb[..., 0], rgb[..., 1], rgb[..., 2], p)
        else:
            bands = tuple(i + 1 for i in s.rgb_idx)
            with rasterio.open(s.path) as ds:
                chw = ds.read(bands)
                vmask = _validity(ds, chw)
            rgb8 = s.scaling.apply(chw)
            m = canopeo_mask(rgb8[0], rgb8[1], rgb8[2], p) & vmask
        if not out_path.lower().endswith(".png"):
            out_path += ".png"
        Image.fromarray((m * 255).astype(np.uint8), "L").save(out_path)
        return out_path

    if not out_path.lower().endswith((".tif", ".tiff")):
        out_path += ".tif"
    bands = tuple(i + 1 for i in s.rgb_idx)
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
                rgb8 = s.scaling.apply(chw)
                m = canopeo_mask(rgb8[0], rgb8[1], rgb8[2], p) & vmask
                out = np.where(vmask, m.astype(np.uint8), 255).astype(np.uint8)
                dst.write(out, 1, window=win)
    return out_path


def save_rows_csv(rows: list, out_path: str) -> str:
    import csv
    if not out_path.lower().endswith(".csv"):
        out_path += ".csv"
    keys = ["plot", "cover_pct", "valid_px", "green_px", "area_m2", "green_m2"]
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


def import_geojson_shapes(path: str) -> list:
    """GeoJSON file -> list of shapes (WGS84 polygons) with a 'name'."""
    import geopandas as gpd
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    shapes = []
    for i, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiPolygon":
            geom = max(geom.geoms, key=lambda g: g.area)
        if geom.geom_type != "Polygon":
            continue
        props = {k: v for k, v in row.items() if k != "geometry"}
        name = props.get("plot_id", props.get("id", props.get("name", f"plot {i + 1}")))
        coords = [[float(y), float(x)] for x, y in geom.exterior.coords]
        shapes.append({"type": "polygon", "coords": coords, "name": str(name)})
    return shapes


def areas_cover(s: Session, shapes: list, p: Params) -> list:
    """Cover for each shape (WGS84) at full resolution. Returns list of dicts."""
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    if not s.georeferenced:
        raise RuntimeError("Areas need a georeferenced raster.")
    bands = [i + 1 for i in s.rgb_idx]
    out = []
    with rasterio.open(s.path) as ds:
        nd = ds.nodata if ds.nodata is not None else 0
        fwd = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        for sh in shapes:
            try:
                geom = shp_transform(fwd, shape_geometry_wgs84(sh))
                arr, _ = rasterio.mask.mask(ds, [geom], crop=True, indexes=bands,
                                            filled=True, nodata=nd)
                vmask = ~np.all(arr == nd, axis=0) & ~np.all(arr == 0, axis=0)
                rgb8 = s.scaling.apply(arr)
                m = canopeo_mask(rgb8[0], rgb8[1], rgb8[2], p) & vmask
                v, g = int(vmask.sum()), int(m.sum())
                cover = round(g / v * 100.0, 2) if v else None
            except ValueError:                 # outside the raster
                v, g, cover = 0, 0, None
            row = {"cover_pct": cover, "valid_px": v, "green_px": g}
            if s.px_area_m2 and v:
                row["area_m2"] = round(v * s.px_area_m2, 1)
                row["green_m2"] = round(g * s.px_area_m2, 1)
            out.append(row)
    return out


def areas_to_geojson(areas: list) -> dict:
    """FeatureCollection (WGS84) of area shapes with their results."""
    from shapely.geometry import mapping
    feats = []
    for a in areas:
        props = {"name": a.get("name"), "canopy_cover_pct": a.get("cover_pct"),
                 "area_m2": a.get("area_m2"), "valid_px": a.get("valid_px"),
                 "source": a.get("source", "drawn")}
        feats.append({"type": "Feature", "properties": props,
                      "geometry": mapping(shape_geometry_wgs84(a))})
    return {"type": "FeatureCollection", "features": feats}
