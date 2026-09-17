# Canopeo Drone

Green canopy cover for drone and satellite orthomosaics, as a desktop
application. Load an 8-bit RGB GeoTIFF, see the Canopeo
classification draped over satellite imagery, tune the thresholds, draw
areas or load plot boundaries, and export masks and per-area results.

Built with [guile](https://github.com/andpatrig/guile); the processing engine
is plain `rasterio` + `numpy` + `geopandas`.

Project page: https://soilwater.github.io/canopeo-drone/

## Who needs what

* **End users** run the packaged `.exe` (see Packaging). It bundles Python,
  guile, and every library, so nothing needs to be installed. Windows 10/11
  already has the WebView2 runtime it uses.
* **Running from source** needs Python 3.11+ and the packages in
  `app/requirements.txt` (which installs `guile >= 0.8.7` from its repo).
* **Building the installer** needs `guile >= 0.8.7` (see Packaging).

## Repo layout

* `app/main.py` — the guile UI (toolbar, sidebar, map).
* `app/engine.py` — all processing; no UI dependencies, importable from scripts.
* `app/tileserver.py` — optional on-demand tile server (see below).
* `app/build.py`, `app/requirements.txt` — packaging.
* `icons/`, `assets/` — app icon and the KSU / OSU logos shown in About.
* `LICENSE.txt` — PolyForm Noncommercial 1.0.0.

## Run from source

```bash
python app/main.py            # add --dev for hot reload
```

## Inputs

Canopeo Drone takes **georeferenced, 8-bit RGB GeoTIFF orthomosaics** only.
Canopeo was developed for standard 8-bit RGB images, so the app enforces that
format instead of converting for the user:

* A file without a coordinate system, or a plain photo (JPEG, PNG, camera
  RAW), is rejected at load with a note pointing to Canopeo Drag&Drop — the
  companion tool for ordinary photos.
* A GeoTIFF that is not `uint8` with at least 3 bands (16-bit, float
  reflectance, single band) is rejected with a note to export 8-bit RGB from
  the photogrammetry software.

Extra bands are fine (RGBA, or RGB + NIR): red, green, and blue come from the
file's color tags, else bands 1/2/3. Pixel values are classified exactly as
stored; nothing is rescaled.

## How the numbers are computed

* **Whole-field cover** streams the full-resolution raster in row strips
  across worker threads — never loaded whole, so multi-GB orthomosaics are
  fine. Strips are capped at ~16 MP so memory does not grow with image width. The map shows a fast overview preview while it runs;
  the badge switches to the exact value when done.
* **Nodata** (alpha band, nodata value, internal mask, all-black pixels) is
  excluded from both numerator and denominator.
* **Areas** are one list, whether drawn on the map (rectangle, polygon, circle
  — via guile's `drawn=` layer) or loaded from a GeoJSON of plot boundaries.
  Each is reprojected to the raster CRS and masked at full resolution; circles
  are buffered in meters. Imported shapes are single outer rings: multi-part
  features keep their largest part and holes are ignored (the app warns when
  this happens); Z coordinates are accepted. Areas show a neon outline with a cover pill, can be
  edited or deleted with the map toolbar (covers recompute automatically), and
  export as CSV / GeoJSON. Both exports record the thresholds each area was
  computed with.
* **Ground units**: pixel size and areas are converted to meters from the
  CRS's linear unit, so State Plane (feet) orthomosaics report correct cm/px
  and hectares. Geographic (lat/lon) rasters report cover but no areas.
* **Canopeo rule** (Patrignani & Ochsner, 2015): a pixel is canopy when
  `R/G < rg`, `B/G < bg`, and `2G − R − B > exg` (defaults 0.95 / 0.95 / 20).
  Raise *excess green* to reject the green tint JPEG compression adds.

## Controls

* Thresholds are sliders (R/G and B/G 0.85–1.15, excess green 0–50) with a
  Reset button. Dragging a threshold updates the map overlay live and shows a
  quick preview cover from a downscaled image; press **Recompute** for the
  exact full-resolution value (a deliberate step, since that pass is the
  expensive one on large files). Mask blend and color are display-only and
  never trigger a recompute.
* *Mask blend* mixes the mask color into the image (0 = plain image);
  *Show overlay* hides the classified layer to compare with the basemap.
* About / Guidelines / License live in the top toolbar; the license text is
  read from `LICENSE.txt`.

The **tile server** (`tileserver.py`) renders each 256 px map tile on demand —
read from the GeoTIFF, classified with the current thresholds, returned as PNG
(stdlib HTTP + rasterio, ~10 ms per tile) — so the overlay stays sharp at zoom
28 (~0.05 cm/px) on gigapixel files. It is optional: delete the file and the
`TILESERVER` block in `main.py` to fall back to a static preview overlay.

## Packaging

```bash
python app/build.py             # dist/CanopeoDrone/  (folder build, onedir)
python app/build.py --console   # keep a console to see tracebacks
```

`build.py` wraps `gui.package()` (PyInstaller). guile (>= 0.8.7) handles the
generic hard parts — bundling only the native WebView2 backend, raising the
recursion limit, and putting conda's `Library\bin` on PATH during the build.
`build.py` adds the geospatial specifics: the GDAL / PROJ data directories, a
full collection of `rasterio` / `pyproj` / `pyogrio` (compiled submodules
PyInstaller cannot see statically), the icon, license, and logos, and excludes for the heavy optional stacks (TensorFlow,
dask, numba, OpenCV, …) that pandas/geopandas extras would otherwise pull in from a full environment.
Building from a clean venv (`app/requirements.txt`) makes those excludes
unnecessary.

At startup a frozen build points `GDAL_DATA` / `PROJ_LIB` at the bundled data
and registers every bundled `*.libs` DLL folder (see the top of `main.py`).

Verify a build without opening a window:

```bash
dist\CanopeoDrone\CanopeoDrone.exe --smoke smoke.txt
```

which exercises GDAL, PROJ, shapely, pyogrio, the input-format checks, the
tile server, logo, and license, and writes `RESULT: OK` (exit code 0) to `smoke.txt`.

The `onedir` folder build (~270 MB) is the one to ship; wrap it with an
installer such as Inno Setup. Target machines need Windows 10/11 with the
WebView2 runtime (preinstalled on current systems).

## Reference

Patrignani, A., & Ochsner, T. E. (2015). Canopeo: A powerful new tool for
measuring fractional green canopy cover. *Agronomy Journal*, 107(6), 2312–2320.
