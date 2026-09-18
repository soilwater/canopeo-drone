"""
build.py — package Canopeo Drone as a Windows executable with guile.

    python build.py            # folder build in dist/CanopeoDrone/  (default)
    python build.py --onefile  # single CanopeoDrone.exe
    python build.py --console  # keep a console window (debugging a build)

guile.package() (>= 0.8.6) already handles the generic hard parts: it bundles
only the native pywebview backend (no Qt), raises PyInstaller's recursion
limit, excludes the tools that crash its analysis, and puts conda's
Library\\bin on PATH so extension DLLs resolve. This script adds only what a
geospatial app needs on top:

  * the GDAL and PROJ data directories (proj.db etc.),
  * a full collect of rasterio / pyproj / pyogrio, whose compiled submodules
    and data PyInstaller's static analysis misses,
  * the icon and license (the self-test synthesizes its own GeoTIFF),
  * excludes for the heavy optional stacks (TensorFlow, dask, numba, OpenCV,
    ...) that pandas/geopandas/rasterio extras would otherwise pull in from a
    full environment. Building from a clean venv makes these unnecessary; they
    are kept so a build from the Anaconda base environment still stays small.
"""

import os
import subprocess
import sys

import guile as gui
from guile._package import _subprocess_env   # conda Library\bin on PATH

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
NAME = "CanopeoDrone"

# Optional stacks reachable through pandas/geopandas/rasterio extras
# that the app never uses. (guile already excludes IPython/sphinx/docutils/…)
HEAVY_EXCLUDES = [
    "tensorflow", "tensorboard", "keras", "huggingface_hub", "torch",
    "dask", "dask_expr", "distributed", "fsspec", "numba", "llvmlite",
    "cv2", "skimage", "imageio", "astropy", "bokeh", "boto3", "botocore",
    "pyarrow", "netCDF4", "h5py", "grpc", "mapclassify", "fiona", "xarray",
    "googleapiclient", "ipywidgets", "comm", "sympy", "numexpr", "bottleneck",
    "lxml", "sqlalchemy", "openpyxl", "xlrd", "tables", "matplotlib",
    "tkinter", "pytest", "scipy", "sklearn", "pandas.tests",
]


def gdal_proj_data():
    """(gdal_data_dir, proj_data_dir) for this environment, wheel or conda."""
    from rasterio._env import get_gdal_data
    import pyproj
    gdal = get_gdal_data()
    proj = pyproj.datadir.get_data_dir()
    for cand in (gdal, os.path.join(sys.prefix, "Library", "share", "gdal")):
        if cand and os.path.isdir(cand):
            gdal = cand
            break
    for cand in (proj, os.path.join(sys.prefix, "Library", "share", "proj")):
        if cand and os.path.isdir(cand):
            proj = cand
            break
    return gdal, proj


def main():
    onefile = "--onefile" in sys.argv
    console = "--console" in sys.argv
    gdal, proj = gdal_proj_data()
    print(f"[build] GDAL data: {gdal}\n[build] PROJ data: {proj}")

    add_data = [
        (os.path.join(ROOT, "icons", "icon-256.png"), "icons"),
        (os.path.join(ROOT, "assets", "kstate_logo.jpg"), "assets"),
        (os.path.join(ROOT, "assets", "osu_logo.png"), "assets"),
        (os.path.join(ROOT, "LICENSE.txt"), "."),
        (gdal, "gdal_data"),
        (proj, "proj_data"),
    ]
    cmd = gui.package(
        os.path.join(APP, "main.py"), name=NAME,
        package_mode="onefile" if onefile else "onedir",
        windowed=not console,
        icon=os.path.join(ROOT, "icons", "canopeo.ico"),
        add_data=add_data,
        hidden_imports=["engine", "tileserver", "pyogrio"],
        exclude_modules=HEAVY_EXCLUDES,
        output_dir=os.path.join(ROOT, "dist"),
        run=False,
    )
    # Compiled submodules / data PyInstaller can't see by static analysis.
    for pkg in ("rasterio", "pyproj", "pyogrio"):
        cmd += ["--collect-all", pkg]
    cmd += ["--paths", APP, "--workpath", os.path.join(ROOT, "build")]

    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True, env=_subprocess_env())
    exe = os.path.join(ROOT, "dist", NAME + ".exe") if onefile else \
        os.path.join(ROOT, "dist", NAME, NAME + ".exe")
    print(f"[build] done: {exe}")


if __name__ == "__main__":
    main()
