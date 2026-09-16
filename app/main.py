"""
Canopeo Drone — green canopy cover for drone & satellite imagery.

Desktop app built with guile (>= 0.8.7). Sidebar holds the controls; the
map with the classified orthomosaic draped over satellite imagery is the
main view. Areas of interest (drawn on the map or loaded from GeoJSON)
get their own canopy cover.

Run:
    python main.py            # or  python main.py --dev  for hot reload
"""

import base64
import math
import os
import sys

import guile as gui

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Frozen build (PyInstaller): bundled data lives next to the executable /
# in the unpack dir; source checkout: one level above app/.
FROZEN = getattr(sys, "frozen", False)
ROOT_DIR = getattr(sys, "_MEIPASS", APP_DIR) if FROZEN else os.path.dirname(APP_DIR)
if FROZEN:
    # GDAL / PROJ look for their data through these variables; the build
    # script bundles the directories under gdal_data/ and proj_data/.
    for _var, _sub in (("GDAL_DATA", "gdal_data"), ("PROJ_LIB", "proj_data"),
                       ("PROJ_DATA", "proj_data")):
        _d = os.path.join(ROOT_DIR, _sub)
        if os.path.isdir(_d):
            os.environ.setdefault(_var, _d)
    # Wheels keep their DLLs in <pkg>.libs folders and register them with
    # os.add_dll_directory relative to the source tree; in a frozen build
    # pyogrio's lookup misses, so register every bundled .libs dir up front.
    for _name in sorted(os.listdir(ROOT_DIR)):
        _d = os.path.join(ROOT_DIR, _name)
        if _name.endswith(".libs") and os.path.isdir(_d):
            os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(_d)
sys.path.insert(0, APP_DIR)
import engine as E  # noqa: E402

VERSION = "0.3.0"
HOMEPAGE = "https://soilwater.github.io/canopeo-drone/"

# ── TILESERVER (optional) ───────────────────────────────────────────────────
# On-demand classified tiles so the overlay stays sharp at any zoom. Remove
# tileserver.py and this block to fall back to the static preview overlay.
try:
    import tileserver as _ts
    TILESERVER = _ts.CanopeoTileServer()
except Exception as _exc:          # missing file or failed to start
    print("[canopeo] tile server unavailable:", _exc)
    TILESERVER = None

# ── Defaults ────────────────────────────────────────────────────────────────
DEF_RG, DEF_BG, DEF_EXG, DEF_BLEND = 0.95, 0.95, 20.0, 55.0

# ── State ───────────────────────────────────────────────────────────────────
sess        = gui.state(None)        # engine.Session
busy_load   = gui.state(False)
busy_full   = gui.state(False)
busy_areas  = gui.state(False)

rg          = gui.state(DEF_RG)
bg          = gui.state(DEF_BG)
exg         = gui.state(DEF_EXG)
blend       = gui.state(DEF_BLEND)   # 0-100
color       = gui.state("#00ff00")
show_ovl    = gui.state(True)

preview_png = gui.state(None)        # bytes (fallback overlay / JPEG view)
preview_cc  = gui.state(None)        # % from the display overview
full        = gui.state(None)        # dict from engine.full_cover
full_prog   = gui.state(0.0)
full_pending = gui.state(False)      # thresholds changed while computing
stale       = gui.state(False)       # classification changed since last Recompute
tile_url    = gui.state("")          # current tile template (changes with params)

# Areas: drawn shapes and file plots in one list. Each entry is what guile's
# draw tools deliver ({id, type, coords}) plus name/source/results.
areas       = gui.state([])
sel_area    = gui.state(None)        # id of the selected area
areas_pending = gui.state(False)
_area_seq   = [0]

tiles       = gui.state("none")      # default: no basemap (drone > basemap res)
view        = gui.state({"center": (39.19, -96.58), "zoom": 5})
img_pick    = gui.state("")          # kept empty so the button label stays fixed
plots_pick  = gui.state("")

show_about  = gui.state(False)
show_license = gui.state(False)
show_guide  = gui.state(False)

COLORS = [("#00ff00", "Green"), ("#ffffff", "White"), ("#f6ff00", "Yellow"),
          ("#00ffff", "Cyan"), ("#ff007f", "Magenta")]
TILES = [("none", "None"), ("satellite", "Satellite")]

NEON = "#39ff14"
AREA_STYLE = {"color": NEON, "weight": 3, "opacity": 1.0,
              "fill_color": NEON, "fill_opacity": 0.06}
AREA_SELECTED = {"color": "#ffffff", "weight": 4, "opacity": 1.0,
                 "fill_color": NEON, "fill_opacity": 0.18}

# Zoom ceiling. Web-Mercator zoom 24 is ~0.7 cm/px at mid latitudes, coarser
# than a 0.2 cm/px drone mosaic, so allow 28 (~0.05 cm/px). Basemaps stop at
# their native level (maxNativeZoom) and are upscaled beyond it; the
# classified overlay is rendered on demand at every level.
MAX_ZOOM = 28
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/"


def _tile(url, native, attribution, **extra):
    opts = {"attribution": attribution, "maxNativeZoom": native,
            "maxZoom": MAX_ZOOM, **extra}
    return {"url": url, "options": opts}


# Satellite imagery from Esri's arcgisonline CDN, which permits app use.
# (OpenStreetMap / OpenTopoMap are volunteer-run servers that return HTTP 403
# to apps, so they are not used.) "none" is a 1x1 transparent tile: no basemap
# imagery, but the map still pans, zooms, and places the overlay by coordinate.
_TRANSPARENT = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
                "FcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")

TILE_LAYERS = {
    "satellite": [_tile(_ESRI + "World_Imagery/MapServer/tile/{z}/{y}/{x}",
                        19, "Tiles © Esri")],
    "none": [{"url": _TRANSPARENT,
              "options": {"attribution": "", "maxNativeZoom": MAX_ZOOM,
                          "maxZoom": MAX_ZOOM}}],
}


def params() -> E.Params:
    return E.Params(rg=rg.value, bg=bg.value, exg=exg.value,
                    blend=blend.value / 100.0, color=color.value)


def zoom_for(bounds, px_w=900, px_h=760) -> int:
    (s, w), (n, e) = bounds
    lon_span = max(e - w, 1e-6)
    lat_span = max(n - s, 1e-6)
    zx = math.log2(360.0 * px_w / (256.0 * lon_span))
    zy = math.log2(170.0 * px_h / (256.0 * lat_span))
    return int(max(2, min(22, math.floor(min(zx, zy)))))


def _data_uri(relpath: str, mime: str) -> str:
    """Read a bundled image and return a data: URI (empty string if missing)."""
    p = os.path.join(ROOT_DIR, relpath)
    if not os.path.isfile(p):
        return ""
    with open(p, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


LOGO_SRC = _data_uri(os.path.join("icons", "icon-256.png"), "image/png")
KSU_SRC = _data_uri(os.path.join("assets", "kstate_logo.jpg"), "image/jpeg")
OSU_SRC = _data_uri(os.path.join("assets", "osu_logo.png"), "image/png")


# ── Callbacks: image & thresholds ───────────────────────────────────────────

def refresh_preview():
    s = sess.value
    if s is None:
        return
    png, cc = E.preview(s, params())
    preview_png.set(png)
    preview_cc.set(cc)


def run_full():
    """Full-resolution cover in the background; re-queues if params change."""
    s = sess.value
    if s is None:
        return
    if busy_full.value:
        full_pending.set(True)
        return
    p = params()
    full_prog.set(0.0)
    full_pending.set(False)

    def work():
        return E.full_cover(s, p, progress=full_prog.set)

    def done(res):
        if sess.value is s:
            full.set(res)
        if full_pending.value:
            run_full()

    gui.task(work, on_done=done, busy=busy_full)


def _tiles_update(new_source=False):
    if TILESERVER is None:
        return
    s = sess.value
    try:
        if new_source:
            TILESERVER.set_source(s, params())
        else:
            TILESERVER.set_params(params())
        tile_url.set(TILESERVER.url if TILESERVER.active else "")
    except Exception as exc:
        tile_url.set("")
        gui.notify(f"Tile server error: {exc}", variant="warning")


def _refresh_display():
    """Cheap update: preview overview + map overlay tiles. No full-res pass."""
    refresh_preview()
    _tiles_update()


def set_display_param(state, value):
    """Blend / color: appearance only — never recompute the cover."""
    state.set(value)
    _refresh_display()


def set_class_param(state, value):
    """R/G, B/G, ExG: change the classification. Update the live preview and
    overlay and mark the exact cover stale, but wait for Recompute."""
    state.set(value)
    _refresh_display()
    stale.set(True)


def recompute():
    """Full-resolution cover + per-area covers for the current thresholds."""
    stale.set(False)
    run_full()
    run_areas()


def reset_defaults():
    rg.set(DEF_RG)
    bg.set(DEF_BG)
    exg.set(DEF_EXG)
    _refresh_display()
    recompute()


def load_image(path):
    if not path:
        return
    img_pick.set("")                 # keep the button label fixed

    def work():
        return E.open_session(path)

    def done(s):
        sess.set(s)
        full.set(None)
        full_pending.set(False)
        stale.set(False)
        sel_area.set(None)
        refresh_preview()
        _tiles_update(new_source=True)
        if s.bounds:
            view.set({"center": s.center, "zoom": zoom_for(s.bounds)})
        for n in s.notes:
            gui.notify(n, variant="warning", duration=6)
        run_full()
        run_areas()                  # re-apply existing areas to the new image

    def fail(exc):
        gui.notify(str(exc), variant="danger", duration=8)

    gui.task(work, on_done=done, on_error=fail, busy=busy_load)


# ── Callbacks: areas ────────────────────────────────────────────────────────

def _new_area_id() -> str:
    _area_seq[0] += 1
    return f"a{_area_seq[0]}"


def run_areas():
    """Cover for every area in the background; re-queues on change."""
    s = sess.value
    snapshot = list(areas.value)
    if s is None or not s.georeferenced or not snapshot:
        return
    if busy_areas.value:
        areas_pending.set(True)
        return
    areas_pending.set(False)
    p = params()
    shapes = [{"type": a["type"], "coords": a["coords"]} for a in snapshot]
    ids = [a["id"] for a in snapshot]

    def work():
        return E.areas_cover(s, shapes, p)

    def done(results):
        by_id = dict(zip(ids, results))

        def merge(cur):
            return [dict(a, **by_id[a["id"]]) if a["id"] in by_id else a
                    for a in cur]
        areas.update(merge)
        if areas_pending.value:
            run_areas()

    gui.task(work, on_done=done, busy=busy_areas)


def on_shape(shape_type, coords):
    n = sum(1 for a in areas.value if a.get("source") == "drawn") + 1
    entry = {"id": _new_area_id(), "type": shape_type, "coords": coords,
             "name": f"Area {n}", "source": "drawn", "cover_pct": None}
    areas.update(lambda a: a + [entry])
    sel_area.set(entry["id"])
    run_areas()


def on_shape_edit(shape_id, shape_type, coords):
    areas.update(lambda a: [dict(x, type=shape_type, coords=coords, cover_pct=None)
                            if x["id"] == shape_id else x for x in a])
    run_areas()


def on_shape_delete(shape_id):
    areas.update(lambda a: [x for x in a if x["id"] != shape_id])
    if sel_area.value == shape_id:
        sel_area.set(None)


def on_shape_click(shape_id):
    sel_area.set(None if sel_area.value == shape_id else shape_id)


def delete_area(shape_id):
    on_shape_delete(shape_id)


def clear_areas():
    areas.set([])
    sel_area.set(None)


def load_plots(path):
    if not path:
        return
    plots_pick.set("")
    try:
        shapes = E.import_geojson_shapes(path)
    except Exception as exc:
        gui.notify(f"Could not read GeoJSON: {exc}", variant="danger", duration=8)
        return
    if not shapes:
        gui.notify("No polygons found in that file.", variant="warning")
        return
    new = [dict(sh, id=_new_area_id(), source="file", cover_pct=None)
           for sh in shapes]
    areas.update(lambda a: a + new)
    gui.notify(f"{len(new)} plots loaded from {os.path.basename(path)}")
    run_areas()


def on_move(center, zoom):
    view.set({"center": tuple(center), "zoom": int(zoom)})


# ── Callbacks: export ───────────────────────────────────────────────────────

def export_mask(path):
    if not path or sess.value is None:
        return
    s, p = sess.value, params()

    def done(out):
        gui.notify(f"Mask saved: {os.path.basename(out)}")

    gui.task(lambda: E.save_mask(s, p, path), on_done=done,
             on_error=lambda e: gui.notify(str(e), variant="danger"))


def _area_rows():
    return [{"plot": a.get("name"), "cover_pct": a.get("cover_pct"),
             "valid_px": a.get("valid_px", 0), "green_px": a.get("green_px", 0),
             **({"area_m2": a["area_m2"]} if "area_m2" in a else {}),
             **({"green_m2": a["green_m2"]} if "green_m2" in a else {})}
            for a in areas.value]


def export_csv(path):
    if not path or not areas.value:
        return
    out = E.save_rows_csv(_area_rows(), path)
    gui.notify(f"CSV saved: {os.path.basename(out)}")


def export_geojson(path):
    if not path or not areas.value:
        return
    out = E.save_geojson(E.areas_to_geojson(areas.value), path)
    gui.notify(f"GeoJSON saved: {os.path.basename(out)}")


def _license_text() -> str:
    for p in (os.path.join(APP_DIR, "LICENSE.txt"),
              os.path.join(ROOT_DIR, "LICENSE.txt")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read()
    return "LICENSE.txt not found."


# ── UI pieces ───────────────────────────────────────────────────────────────

TOOLBAR_H = 44
BODY_H = f"calc(100vh - {TOOLBAR_H}px)"
SIDEBAR_CSS = ("width:330px;flex-shrink:0;border-right:1px solid var(--border);"
               f"background:var(--surface);overflow-y:auto;height:{BODY_H}")
TOOLBAR_CSS = (f"height:{TOOLBAR_H}px;flex-shrink:0;border-bottom:1px solid "
               "var(--border);background:var(--surface)")
SECTION_CSS = "text-transform:uppercase;letter-spacing:.06em"
# square map corners; let the map fill its column instead of a fixed height
PAGE_CSS = ("<style>.guile-map{border-radius:0 !important;height:100% !important}"
            ".guile-map-canvas{height:100% !important}</style>")


def cover_readout():
    """Top result block (shown on every tab): the loading progress while the
    full-resolution cover is computing, then the final canopy-cover value."""
    s = sess.value
    if s is None:
        return
    f = full.value
    # Initial load: no exact value yet — show a prominent loading card in the
    # slot the cover banner will occupy, so the flow reads load → analyzing → value.
    if f is None and busy_full.value:
        pct = int(full_prog.value * 100)
        with gui.card(gap=6, padding=10, style="background:var(--surface-2)",
                      key="cc-loading"):
            gui.text("Analyzing full resolution…", bold=True, size="sm",
                     key="cc-load-title")
            # Slightly taller, darker track so the bar reads as a bar at 0%.
            gui.progress(pct, key="cc-progress",
                         style="height:8px;background:rgba(0,0,0,.14)")
            note = f"{pct}%"
            if preview_cc.value is not None:
                note += f"  ·  preview {preview_cc.value:.1f}%"
            gui.text(note, size="sm", muted=True, key="cc-load-note")
        return
    # Recomputing while an earlier value exists.
    if busy_full.value and f is not None:
        gui.badge(f"{f['cover']:.2f}% canopy cover", variant="success",
                  style="font-size:16px;padding:6px 14px", key="cc-badge")
        gui.progress(int(full_prog.value * 100), key="cc-progress",
                     style="height:8px;background:rgba(0,0,0,.14)")
        gui.text("Recomputing…", size="sm", muted=True, key="cc-updating")
        return
    # Exact value, up to date.
    if f is not None and not stale.value:
        gui.badge(f"{f['cover']:.2f}% canopy cover", variant="success",
                  style="font-size:16px;padding:6px 14px", key="cc-badge")
        return
    # Thresholds changed (or no exact value yet): quick preview + Recompute.
    if preview_cc.value is not None:
        gui.badge(f"{preview_cc.value:.1f}% (preview)", variant="neutral",
                  style="font-size:16px;padding:6px 14px", key="cc-badge")
    gui.button("Recompute", variant="primary", size="sm", on_click=recompute,
               key="cc-recompute")
    gui.text("Recompute runs a full-resolution analysis for the exact value; "
             "moving the sliders shows a quick preview approximation.",
             size="sm", muted=True, key="cc-note")


def metadata_block(s):
    with gui.card(gap=3, padding=10, style="background:var(--surface-2)"):
        gui.text(s.name, size="sm", bold=True, style="word-break:break-all")
        gui.text(f"{s.width} × {s.height} px · {s.bands} band(s) · {s.dtype} · "
                 f"{s.file_mb:.1f} MB", size="sm", muted=True)
        r, g, b = (i + 1 for i in s.rgb_idx)
        gui.text(f"RGB from bands {r}/{g}/{b}", size="sm", muted=True)
        gui.text(f"CRS {s.crs} · GSD {s.gsd}", size="sm", muted=True)
        f = full.value
        if f is not None:
            area = f" · {f['valid_m2'] / 1e4:.2f} ha" if "valid_m2" in f else ""
            gui.text(f"Valid pixels {f['valid_px']:,}{area}", size="sm", muted=True)


def analyze_tab():
    s = sess.value
    with gui.row(justify="space-between", align="center"):
        gui.text("Canopeo thresholds", bold=True, size="sm", muted=True,
                 style=SECTION_CSS)
        gui.button("Reset", variant="ghost", size="sm", on_click=reset_defaults,
                   key="reset-thr")
    gui.slider("R/G threshold", min=0.85, max=1.15, step=0.01, value=rg,
               on_change=lambda v: set_class_param(rg, v), key="rg")
    gui.slider("B/G threshold", min=0.85, max=1.15, step=0.01, value=bg,
               on_change=lambda v: set_class_param(bg, v), key="bg")
    gui.slider("Excess green (2G−R−B)", min=0, max=50, step=1, value=exg,
               on_change=lambda v: set_class_param(exg, v), key="exg")
    gui.text("Lower the ratios to be stricter about what counts as canopy. Raise "
             "excess green to reject soil, shadows, or JPEG green tint.",
             size="sm", muted=True)

    gui.divider()
    gui.text("Display", bold=True, size="sm", muted=True, style=SECTION_CSS)
    gui.slider("Mask blend", min=0, max=100, step=5, value=blend,
               on_change=lambda v: set_display_param(blend, v), key="blend")
    gui.checkbox("Show overlay", value=show_ovl, on_change=show_ovl.set,
                 key="show-ovl")
    gui.select(COLORS, "Mask color", value=color,
               on_change=lambda v: set_display_param(color, v), key="color")
    if s is not None and s.georeferenced:
        gui.select(TILES, "Basemap", value=tiles, on_change=tiles.set, key="tiles")


def areas_tab():
    s = sess.value
    geo = s is not None and s.georeferenced
    gui.text("Draw rectangles, polygons, or circles with the map toolbar, or "
             "load a GeoJSON of plot boundaries. Each area gets its own canopy "
             "cover. Edit or delete areas with the toolbar.",
             size="sm", muted=True)
    gui.file_picker("Load plots (GeoJSON)…", value=plots_pick,
                    file_types=("geojson", "json"), disabled=not geo,
                    on_change=load_plots, key="plots-load", style="width:100%")
    lst = areas.value
    if not lst:
        return
    if busy_areas.value:
        gui.text("Computing area cover…", size="sm", muted=True)
    elif stale.value:
        gui.text("Thresholds changed — press Recompute for exact area values.",
                 size="sm", muted=True)

    sel = next((a for a in lst if a["id"] == sel_area.value), None)
    if sel is not None:
        with gui.card(gap=4, padding=10, style="background:var(--surface-2)"):
            with gui.row(justify="space-between", align="center"):
                gui.text(sel.get("name", sel["id"]), bold=True, size="sm")
                with gui.row(gap=2):
                    gui.button("Delete", variant="ghost", size="sm",
                               on_click=lambda: delete_area(sel["id"]),
                               key="sel-del")
                    gui.button("✕", variant="ghost", size="sm",
                               on_click=lambda: sel_area.set(None), key="sel-close")
            cc = sel.get("cover_pct")
            gui.badge("n/a" if cc is None else f"{cc:.2f}% canopy cover",
                      variant="success" if cc is not None else "neutral")
            bits = [f"{sel['type']} · {sel.get('source', 'drawn')}"]
            if sel.get("area_m2"):
                bits.append(f"{sel['area_m2'] / 1e4:.3f} ha")
            if sel.get("valid_px"):
                bits.append(f"{sel['valid_px']:,} px")
            gui.text(" · ".join(bits), size="sm", muted=True)

    vals = [a["cover_pct"] for a in lst if a.get("cover_pct") is not None]
    with gui.row(justify="space-between", align="center"):
        if vals:
            gui.text(f"{len(lst)} areas · mean {sum(vals) / len(vals):.1f}% · "
                     f"min {min(vals):.1f}% · max {max(vals):.1f}%",
                     size="sm", muted=True)
        else:
            gui.text(f"{len(lst)} areas", size="sm", muted=True)
        gui.button("Clear all", size="sm", variant="ghost", on_click=clear_areas,
                   key="areas-clear")
    rows = [{"area": a.get("name"),
             "cover %": "…" if a.get("cover_pct") is None else a["cover_pct"],
             "ha": round(a["area_m2"] / 1e4, 3) if a.get("area_m2") else ""}
            for a in lst]
    with gui.scroll(max_height=380, key="areas-scroll"):
        gui.table(rows, key="areas-table")


def export_tab():
    s = sess.value
    have = s is not None
    gui.text("Canopy mask", bold=True, size="sm")
    gui.text("GeoTIFF mask (1 = canopy, 0 = other, 255 = nodata) at the current "
             "thresholds.", size="sm", muted=True)
    gui.file_picker("Save mask…", save=True, file_types=("GeoTIFF (*.tif)",),
                    disabled=not have,
                    on_change=export_mask, key="mask-save", style="width:100%")
    gui.divider()
    gui.text("Areas", bold=True, size="sm")
    have_areas = bool(areas.value)
    gui.file_picker("Save areas CSV…", save=True, file_types=("csv",),
                    disabled=not have_areas, on_change=export_csv,
                    key="csv-save", style="width:100%")
    gui.file_picker("Save areas GeoJSON…", save=True, file_types=("geojson",),
                    disabled=not have_areas, on_change=export_geojson,
                    key="gj-save", style="width:100%")


def toolbar():
    with gui.row(align="center", justify="space-between", padding="0 16px",
                 style=TOOLBAR_CSS):
        with gui.row(gap=10, align="center"):
            if LOGO_SRC:
                gui.html(f'<img src="{LOGO_SRC}" alt="" '
                         'style="height:28px;width:28px;display:block">',
                         key="logo")
            gui.title("Canopeo Drone", size="md")
            gui.text("Green canopy cover for drone & satellite imagery",
                     muted=True, size="sm")
        with gui.row(gap=2, align="center"):
            gui.button("About", variant="ghost", size="sm",
                       on_click=lambda: show_about.set(True), key="about-btn")
            gui.button("Guidelines", variant="ghost", size="sm",
                       on_click=lambda: show_guide.set(True), key="guide-btn")
            gui.button("License", variant="ghost", size="sm",
                       on_click=lambda: show_license.set(True), key="license-btn")


def modals():
    with gui.modal("About Canopeo Drone", visible=show_about.value,
                   on_close=lambda: show_about.set(False), width=460,
                   key="about-modal"):
        gui.text(f"Canopeo Drone v{VERSION}", bold=True)
        gui.text("Green canopy cover from drone and satellite imagery, using "
                 "the Canopeo algorithm.", size="sm")
        gui.text("Canopeo classifies a pixel as green canopy when R/G and B/G "
                 "are below their thresholds and the excess green index "
                 "(2G − R − B) is above its threshold.", size="sm", muted=True)
        gui.divider()
        gui.text("Reference", bold=True, size="sm")
        gui.text("Patrignani, A., & Ochsner, T. E. (2015). Canopeo: A powerful "
                 "new tool for measuring fractional green canopy cover. "
                 "Agronomy Journal, 107(6), 2312–2320.", size="sm", muted=True)
        gui.divider()
        gui.text("© 2026 Andres Patrignani and Tyson E. Ochsner", size="sm",
                 muted=True)
        gui.text(HOMEPAGE, size="sm", muted=True, mono=True)
        if KSU_SRC or OSU_SRC:
            gui.divider()
            gui.text("A partnership between Kansas State University and "
                     "Oklahoma State University.", size="sm", muted=True,
                     style="text-align:center")
            with gui.row(gap=28, justify="center", align="center",
                         key="about-logos"):
                if KSU_SRC:
                    gui.html(f'<img src="{KSU_SRC}" alt="Kansas State University"'
                             ' style="height:44px;width:auto;object-fit:contain">',
                             key="ksu-logo")
                if OSU_SRC:
                    gui.html(f'<img src="{OSU_SRC}" alt="Oklahoma State University"'
                             ' style="height:44px;width:auto;object-fit:contain">',
                             key="osu-logo")

    with gui.modal("Guidelines", visible=show_guide.value,
                   on_close=lambda: show_guide.set(False), width=560,
                   key="guide-modal"):
        with gui.scroll(max_height=520):
            with gui.col(gap=10):
                gui.text("Workflow", bold=True, size="sm")
                gui.text("1. Load a georeferenced GeoTIFF orthomosaic (RGB or "
                         "multispectral). For plain photos, use Canopeo Drag&Drop.",
                         size="sm")
                gui.text("2. The map shows the classified image over satellite "
                         "imagery. Whole-image cover shows a quick preview first, "
                         "then the exact full-resolution value.", size="sm")
                gui.text("3. Adjust the thresholds until the overlay matches the "
                         "canopy you see. While you drag, the map overlay and the "
                         "cover number update as a quick preview from a downscaled "
                         "image. Press Recompute for the exact full-resolution "
                         "value — on large orthomosaics that pass takes a few "
                         "seconds, so it is a deliberate step, not automatic.",
                         size="sm")
                gui.text("4. Draw areas on the map or load a GeoJSON of plot "
                         "boundaries (Areas tab). Each area shows its cover on the "
                         "map and in the table. Save results from the Export tab.", size="sm")
                gui.divider()
                gui.text("Thresholds", bold=True, size="sm")
                gui.text("R/G and B/G (default 0.95): a pixel counts as canopy only if "
                         "red and blue are both below this fraction of green. Lower "
                         "values are stricter.", size="sm")
                gui.text("Excess green (default 20, on a 0–255 scale): how much "
                         "greener than red and blue combined a pixel must be. "
                         "Raise it if soil or shadows are picked up, or if JPEG "
                         "compression adds a green tint.", size="sm")
                gui.divider()
                gui.text("What is counted", bold=True, size="sm")
                gui.text("Transparent, nodata, and all-black pixels (stitching "
                         "borders) are excluded from the calculation. Multispectral "
                         "bands are mapped to RGB automatically (PlanetScope "
                         "8-band, MicaSense 5-band, or the color tags in the file).", size="sm")
                gui.text("One brightness stretch is applied to the whole image so "
                         "thresholds behave the same across orthomosaic seams. "
                         "Stitching artifacts and compression can still bias "
                         "results, so always check the overlay visually.",
                         size="sm")
                gui.divider()
                gui.text("Large files", bold=True, size="sm")
                gui.text("Orthomosaics are read in strips and map tiles are "
                         "rendered on demand, so multi-gigabyte files work without "
                         "loading them into memory. The full-resolution pass takes "
                         "seconds to about a minute depending on file size.",
                         size="sm")

    with gui.modal("License", visible=show_license.value,
                   on_close=lambda: show_license.set(False), width=640,
                   key="license-modal"):
        with gui.scroll(max_height=520):
            gui.text(_license_text(), size="sm", mono=True,
                     style="white-space:pre-wrap")


def sidebar():
    with gui.col(padding=16, gap=12, style=SIDEBAR_CSS):
        gui.file_picker("Loading…" if busy_load.value else "Load image…",
                        value=img_pick,
                        file_types=("GeoTIFF (*.tif;*.tiff)",),
                        disabled=busy_load.value, on_change=load_image,
                        key="img-load", style="width:100%")
        if sess.value is not None:
            metadata_block(sess.value)
            cover_readout()
        gui.divider()
        tab = gui.tabs(["Analyze", "Areas", "Export"], key="side-tabs")
        if tab == "Analyze":
            analyze_tab()
        elif tab == "Areas":
            areas_tab()
        else:
            export_tab()


def _overlay_layer(s):
    """Classified overlay: on-demand tiles when available, else the preview."""
    if tile_url.value:
        (sw, ne) = s.bounds
        # Plain dict (same shape gui.TileOverlay emits) so we can lift the
        # zoom ceiling above TileOverlay's fixed 24.
        return {"type": "tiles", "url": tile_url.value,
                "options": {"opacity": 1.0, "tms": False, "minZoom": 0,
                            "maxNativeZoom": MAX_ZOOM, "maxZoom": MAX_ZOOM,
                            "attribution": "",
                            "bounds": [list(sw), list(ne)]}}
    if preview_png.value:
        return gui.ImageOverlay(preview_png.value, bounds=s.bounds, opacity=1.0)
    return None


def _drawn_entries():
    """areas -> guile drawn= list: neon outline, selection highlight, cover pill."""
    out = []
    for a in areas.value:
        cc = a.get("cover_pct")
        label = f"{cc:.1f}%" if cc is not None else ("…" if busy_areas.value else "")
        d = {"id": a["id"], "type": a["type"], "coords": a["coords"], "label": label}
        if a["id"] == sel_area.value:
            d["style"] = AREA_SELECTED
        out.append(d)
    return out


def main_view():
    s = sess.value
    with gui.col(fill=True, style=f"height:{BODY_H}"):
        if s is None:
            with gui.col(align="center", justify="center", fill=True):
                with gui.card(padding=40):
                    with gui.col(align="center", gap=8):
                        gui.text("No image loaded", muted=True)
                        gui.text("Load a georeferenced GeoTIFF orthomosaic to begin.",
                                 muted=True, size="sm")
            return

        layers = []
        if show_ovl.value:
            ov = _overlay_layer(s)
            if ov is not None:
                layers.append(ov)
        v = view.value
        gui.leaflet(center=v["center"], zoom=v["zoom"], height=800,
                    tiles=TILE_LAYERS[tiles.value], layers=layers,
                    draw=["rectangle", "polygon", "circle"],
                    drawn=_drawn_entries(), draw_style=AREA_STYLE,
                    on_shape=on_shape, on_shape_edit=on_shape_edit,
                    on_shape_delete=on_shape_delete,
                    on_shape_click=on_shape_click,
                    on_move=on_move,
                    on_click=lambda lat, lon: sel_area.set(None),
                    style="height:100%", key="map")


@gui.app("Canopeo Drone", width=1320, height=880, resizable=True)
def ui():
    with gui.col(gap=0, style="height:100vh;overflow:hidden"):
        gui.html(PAGE_CSS, key="page-css")
        toolbar()
        with gui.row(gap=0, align="stretch", fill=True,
                     style=f"width:100%;height:{BODY_H};overflow:hidden"):
            sidebar()
            main_view()
    modals()


def _smoke(log_path: str) -> int:
    """Headless self-test for a packaged build: exercise GDAL/PROJ/shapely
    through the engine and write the outcome to log_path. Exit 0 on success."""
    import traceback
    lines = [f"Canopeo Drone {VERSION} smoke test", f"frozen={FROZEN} root={ROOT_DIR}",
             f"GDAL_DATA={os.environ.get('GDAL_DATA')} PROJ_LIB={os.environ.get('PROJ_LIB')}"]
    ok = True
    try:
        import rasterio, pyproj, shapely, geopandas  # noqa: F401
        lines.append(f"rasterio {rasterio.__version__} (GDAL {rasterio.__gdal_version__}), "
                     f"pyproj {pyproj.__version__} (PROJ {pyproj.proj_version_str}), "
                     f"shapely {shapely.__version__}, geopandas {geopandas.__version__}")
        # synthesize a small georeferenced GeoTIFF — no fixture ships with the app
        import json as _json, tempfile
        import numpy as _np
        from rasterio.transform import from_origin
        _tif = os.path.join(tempfile.gettempdir(), "canopeo_smoke.tif")
        _h, _w = 240, 320
        _rgb = _np.zeros((3, _h, _w), _np.uint8)
        _rgb[0] = 120; _rgb[1] = 110; _rgb[2] = 115            # soil
        _rgb[0, :120] = 40; _rgb[1, :120] = 200; _rgb[2, :120] = 45  # top half green
        with rasterio.open(_tif, "w", driver="GTiff", height=_h, width=_w, count=3,
                           dtype="uint8", crs="EPSG:32614",
                           transform=from_origin(563000, 4252000, 3, 3),
                           photometric="RGB") as _ds:
            _ds.write(_rgb)
        s = E.open_session(_tif)
        r = E.full_cover(s, E.Params())
        lines.append(f"GeoTIFF cover {r['cover']:.1f}% (expect ~50), CRS {s.crs}")
        ok &= abs(r["cover"] - 50.0) < 2.0 and s.georeferenced
        # a non-GeoTIFF input must be refused
        try:
            E.open_session(_tif[:-4] + ".jpg")
            lines.append("reject non-GeoTIFF: NOT rejected"); ok = False
        except RuntimeError:
            lines.append("reject non-GeoTIFF: ok")
        # projected CRS round-trip through PROJ (needs proj.db)
        from pyproj import Transformer
        x, y = Transformer.from_crs("EPSG:4326", "EPSG:32614", always_xy=True).transform(-98.0, 38.3)
        lines.append(f"PROJ transform ok: {x:.1f}, {y:.1f}")
        ok &= 500000 < x < 600000
        geom = E.shape_geometry_wgs84({"type": "circle", "coords": {"lat": 38.3, "lng": -98.0, "radius": 100}})
        lines.append(f"circle geometry ok: {geom.geom_type}, {len(geom.exterior.coords)} pts")
        # verify pyogrio can read a GeoJSON (the frozen-build failure mode)
        _fc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"plot_id": f"p{_i}"},
             "geometry": {"type": "Polygon", "coordinates": [[
                 [-98.00, 38.30], [-97.99, 38.30], [-97.99, 38.31],
                 [-98.00, 38.31], [-98.00, 38.30]]]}} for _i in range(2)]}
        _gj = os.path.join(tempfile.gettempdir(), "canopeo_smoke.geojson")
        with open(_gj, "w", encoding="utf-8") as _f:
            _json.dump(_fc, _f)
        shapes = E.import_geojson_shapes(_gj)
        lines.append(f"GeoJSON read (pyogrio): {len(shapes)} polygons")
        ok &= len(shapes) == 2
        for _f in (_tif, _gj):
            try:
                os.remove(_f)
            except OSError:
                pass
        lines.append("tile server: " + ("started" if TILESERVER else "unavailable"))
        ok &= TILESERVER is not None
        lines.append("logo: " + ("found" if LOGO_SRC else "MISSING"))
        lines.append("license: " + ("found" if "PolyForm" in _license_text() else "MISSING"))
        ok &= bool(LOGO_SRC) and "PolyForm" in _license_text()
    except Exception:
        ok = False
        lines.append(traceback.format_exc())
    lines.append("RESULT: " + ("OK" if ok else "FAILED"))
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        sys.exit(_smoke(sys.argv[sys.argv.index("--smoke") + 1]))
    gui.run(dev="--dev" in sys.argv)
