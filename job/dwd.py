"""DWD's radar nowcast (RADVOR "RV"): 2 hours ahead in 5-minute steps, 1 km.

Open data, no key: https://opendata.dwd.de/weather/radar/composite/rv/ (CC BY
4.0, Deutscher Wetterdienst). Only German radars feed it, so it covers roughly
the eastern three quarters of the Netherlands; elsewhere it has no data.

Frames are reprojected to the app's Web Mercator grid as mm/h (NaN = no data).
For the app, the Netherlands area is cut out at half resolution (~2 km) and all
25 steps are stacked into one greyscale PNG: 0 = dry, 1..254 = 0.1..~38 mm/h
(log scale, as elsewhere), 255 = no data.
"""

import io
import math
import tarfile
import urllib.request
import warnings
from datetime import datetime, timezone

import h5py
import numpy as np
from PIL import Image
from pyproj import Transformer

URL = "https://opendata.dwd.de/weather/radar/composite/rv/composite_rv_LATEST.tar"

# Must match app.js / build.py.
X0, Y0, X1, Y1 = 0, 6257115.396264, 1208534.924976, 7553162.131589
W, H = 700, 751

# Atlas window: the Netherlands plus a margin (lat 50.5–53.8, lon 3.0–7.6).
ATLAS_STEP = 2


def _px(lat, lon):
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137
    return int((x - X0) / (X1 - X0) * W), int((Y1 - y) / (Y1 - Y0) * H)


_AX0, _AY0 = _px(53.8, 3.0)
_AX1, _AY1 = _px(50.5, 7.6)
ATLAS = {"x0": _AX0, "y0": _AY0, "w": (_AX1 - _AX0) // ATLAS_STEP, "h": (_AY1 - _AY0) // ATLAS_STEP, "step": ATLAS_STEP}

_lookup = {}


def _grid_lookup(projdef):
    """Nearest DWD pixel for each Mercator pixel centre (DWD grid: 1100 × 1200 km,
    1 km cells, pixel (0, 0) centred at x = 0, y = 0, y decreasing downwards)."""
    if projdef not in _lookup:
        t = Transformer.from_crs("EPSG:3857", projdef, always_xy=True)
        ys, xs = np.mgrid[0:H, 0:W]
        sx, sy = t.transform(X0 + (xs + 0.5) * (X1 - X0) / W, Y1 - (ys + 0.5) * (Y1 - Y0) / H)
        c, r = np.round(sx / 1000).astype(int), np.round(-sy / 1000).astype(int)
        inside = (r >= 0) & (r < 1200) & (c >= 0) & (c < 1100)
        _lookup[projdef] = (np.where(inside, r, 0), np.where(inside, c, 0), inside)
    return _lookup[projdef]


def fetch():
    """→ (run time, list of 25 mm/h grids on the app's grid for +0 … +120 min)."""
    with urllib.request.urlopen(URL, timeout=60) as r:
        tar = tarfile.open(fileobj=io.BytesIO(r.read()))
    members = sorted((m for m in tar.getmembers() if m.name.endswith("-hd5")), key=lambda m: m.name)
    ref = datetime.strptime("".join(members[0].name.split("_")[2:4]), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    grids = []
    for m in members[:25]:
        f = h5py.File(io.BytesIO(tar.extractfile(m).read()))
        what = f["dataset1/data1/what"].attrs
        proj = f["where"].attrs["projdef"]
        proj = proj.decode() if isinstance(proj, bytes) else proj
        rows, cols, inside = _grid_lookup(proj)
        raw = f["dataset1/data1/data"][:]
        v = raw[rows, cols].astype(np.float64)
        mm5 = v * what["gain"] + what["offset"]
        grids.append(np.where(inside & (v != what["nodata"]), np.maximum(mm5, 0) * 12, np.nan))
    return ref, grids


def atlas(grids):
    """Stack the Netherlands window of each grid (half resolution) into one PNG image."""
    tiles = []
    s = ATLAS_STEP
    for g in grids:
        win = g[_AY0:_AY0 + ATLAS["h"] * s, _AX0:_AX0 + ATLAS["w"] * s]
        win = win.reshape(ATLAS["h"], s, ATLAS["w"], s)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)    # all-NaN blocks: no data there
            v = np.nanmean(win, axis=(1, 3))
        b = np.zeros(v.shape, np.uint8)
        wet = v >= 0.1
        b[wet] = np.clip(1 + np.round((np.log10(v[wet]) + 1) / math.log10(400) * 254), 1, 254)
        b[np.isnan(v)] = 255
        tiles.append(b)
    return Image.fromarray(np.vstack(tiles), "L")
