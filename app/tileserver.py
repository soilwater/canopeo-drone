"""
tileserver.py — on-demand classified XYZ tiles for the Canopeo Drone map.

Self-contained and optional: main.py works without it (it falls back to the
static ImageOverlay preview). Delete this file and the `TILESERVER` block in
main.py to remove the feature entirely.

How it works
------------
A tiny HTTP server (stdlib only) runs on 127.0.0.1 in a daemon thread. Each
request for  /tiles/{z}/{x}/{y}.png  reads *just that tile's window* from the
GeoTIFF through a Web-Mercator WarpedVRT, runs the Canopeo classification
with the current thresholds, blends the mask color, and returns a 256 px
RGBA PNG (transparent outside the mosaic). Nothing is pre-tiled and the full
raster is never loaded, so gigapixel orthomosaics stay sharp at zoom 28.

Threshold changes bump a version number that is part of the tile URL, so
Leaflet simply refetches the tiles in view.

Usage
-----
    srv = CanopeoTileServer()               # starts listening
    srv.set_source(session, params)         # after engine.open_session()
    srv.set_params(params)                  # on every threshold change
    gui.TileOverlay(srv.url, max_zoom=28, bounds=session.bounds)
"""

from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds

import engine as E

TILE = 256
R = 6378137.0
ORIGIN = math.pi * R                    # half the Web-Mercator world width
_EMPTY_PNG = None                       # lazily built transparent tile


def tile_bounds_3857(z: int, x: int, y: int):
    """(minx, miny, maxx, maxy) of an XYZ tile in EPSG:3857 meters."""
    size = 2 * ORIGIN / (2 ** z)
    minx = -ORIGIN + x * size
    maxy = ORIGIN - y * size
    return minx, maxy - size, minx + size, maxy


def _empty_png() -> bytes:
    global _EMPTY_PNG
    if _EMPTY_PNG is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0)).save(buf, "PNG")
        _EMPTY_PNG = buf.getvalue()
    return _EMPTY_PNG


class _Source:
    """One open raster + its Web-Mercator VRT, guarded by a lock."""

    def __init__(self, session: E.Session):
        self.session = session
        self.ds = rasterio.open(session.path)
        self.vrt = WarpedVRT(self.ds, crs="EPSG:3857",
                             resampling=Resampling.bilinear)
        self.bands = tuple(i + 1 for i in session.rgb_idx)
        b = self.vrt.bounds
        self.bounds = (b.left, b.bottom, b.right, b.top)
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            try:
                self.vrt.close()
                self.ds.close()
            except Exception:
                pass

    def render(self, z, x, y, params: E.Params) -> bytes:
        minx, miny, maxx, maxy = tile_bounds_3857(z, x, y)
        bl, bb, br, bt = self.bounds
        if maxx <= bl or minx >= br or maxy <= bb or miny >= bt:
            return _empty_png()

        # WarpedVRT forbids boundless reads, so clip the tile to the mosaic
        # extent, read that part, and paste it into a blank 256 px tile.
        res = (maxx - minx) / TILE                  # meters per tile pixel
        ix0, ix1 = max(minx, bl), min(maxx, br)
        iy0, iy1 = max(miny, bb), min(maxy, bt)
        c0 = int(math.floor((ix0 - minx) / res))
        c1 = int(math.ceil((ix1 - minx) / res))
        r0 = int(math.floor((maxy - iy1) / res))
        r1 = int(math.ceil((maxy - iy0) / res))
        c0, r0 = max(c0, 0), max(r0, 0)
        c1, r1 = min(c1, TILE), min(r1, TILE)
        w, h = c1 - c0, r1 - r0
        if w <= 0 or h <= 0:
            return _empty_png()
        # bounds of exactly the pixels we will fill (keeps alignment exact)
        px0, px1 = minx + c0 * res, minx + c1 * res
        py1, py0 = maxy - r0 * res, maxy - r1 * res
        win = from_bounds(px0, py0, px1, py1, self.vrt.transform)
        # At very deep zooms a tile covers less than one source pixel; GDAL
        # needs a window of at least 1x1, so widen it (alignment error is
        # then below the source pixel size, i.e. invisible).
        if win.width < 1 or win.height < 1:
            from rasterio.windows import Window
            win = Window(math.floor(win.col_off), math.floor(win.row_off),
                         max(1, math.ceil(win.width)), max(1, math.ceil(win.height)))

        chw = np.zeros((3, TILE, TILE), dtype=self.vrt.dtypes[0])
        dm = np.zeros((TILE, TILE), dtype=np.uint8)
        with self.lock:
            part = self.vrt.read(self.bands, window=win, out_shape=(3, h, w),
                                 resampling=Resampling.bilinear)
            part_m = self.vrt.dataset_mask(window=win, out_shape=(h, w))
        chw[:, r0:r1, c0:c1] = part
        dm[r0:r1, c0:c1] = part_m
        valid = (dm > 0) & ~np.all(chw == 0, axis=0)
        if not valid.any():
            return _empty_png()

        rgb8 = self.session.scaling.apply(chw)
        rgb = np.ascontiguousarray(np.transpose(rgb8, (1, 2, 0)))
        mask = E.canopeo_mask(rgb8[0], rgb8[1], rgb8[2], params) & valid
        blended = E.blend_rgb(rgb, mask, params)
        rgba = np.dstack([blended, (valid * 255).astype(np.uint8)])

        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=1)
        return buf.getvalue()


class CanopeoTileServer:
    def __init__(self, cache_tiles: int = 600):
        self._source: _Source | None = None
        self._params = E.Params()
        self._version = 0
        self._cache: OrderedDict = OrderedDict()
        self._cache_max = cache_tiles
        self._clock = threading.Lock()

        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):        # keep the console quiet
                pass

            def do_GET(self):
                try:
                    path = self.path.split("?", 1)[0]
                    parts = path.strip("/").split("/")
                    if len(parts) != 4 or parts[0] != "tiles":
                        self.send_error(404)
                        return
                    z, x = int(parts[1]), int(parts[2])
                    y = int(parts[3].split(".")[0])
                    png = server._tile(z, x, y)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(png)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(png)
                except (BrokenPipeError, ConnectionAbortedError):
                    pass
                except Exception as exc:       # never take the app down
                    try:
                        self.send_error(500, str(exc)[:200])
                    except Exception:
                        pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="canopeo-tiles").start()

    # ── public API ──────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._source is not None

    @property
    def url(self) -> str:
        """XYZ template for gui.TileOverlay; changes whenever params change."""
        return (f"http://127.0.0.1:{self.port}/tiles/{{z}}/{{x}}/{{y}}.png"
                f"?v={self._version}")

    def set_source(self, session: E.Session, params: E.Params):
        old, self._source = self._source, None
        if old is not None:
            old.close()
        if session is not None and session.georeferenced and session.raw_rgb is None:
            self._source = _Source(session)
        self.set_params(params)

    def set_params(self, params: E.Params):
        self._params = params
        self._version += 1
        with self._clock:
            self._cache.clear()

    def clear(self):
        self.set_source(None, self._params)

    def stop(self):
        self.clear()
        self._httpd.shutdown()

    # ── internals ───────────────────────────────────────────────────────────

    def _tile(self, z, x, y) -> bytes:
        src, params, ver = self._source, self._params, self._version
        if src is None:
            return _empty_png()
        key = (ver, z, x, y)
        with self._clock:
            png = self._cache.get(key)
            if png is not None:
                self._cache.move_to_end(key)
                return png
        png = src.render(z, x, y, params)
        with self._clock:
            self._cache[key] = png
            while len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)
        return png
