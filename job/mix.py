"""KNMI and DWD merged: the app's default forecast ("Both").

Forecast frames: the mean rain rate (mm/h) of KNMI's and DWD's nowcasts where
both cover a place, one source alone where only it covers. At each source's
coverage edge its weight ramps up over FEATHER pixels (~20 km), so there is no
seam where the averaging starts. DWD is used only when its run is recent.

Observed frames (the past hour) are not averaged: KNMI's gauge-corrected
observation wherever KNMI covers, DWD's analysis only outside that.

All frames are on KNMI's grid (the app's main timeline) with the usual byte
encoding. DWD comes from its map frames (job/dwd.py, own Mercator grid, 255 =
no data), resampled by nearest pixel. Also written: KNMI's forecast for the
Netherlands window stacked in one image (like DWD's), so the app can show
both components while scrubbing.
"""

import math
from datetime import datetime, timedelta

import cv2
import numpy as np
from PIL import Image

import dwd

# Must match app.js / build.py.
X0, Y0, X1, Y1 = 0, 6257115.396264, 1208534.924976, 7553162.131589
W, H = 700, 751
STEP = timedelta(minutes=5)
FEATHER = 20                     # pixels (~1.06 km each)
STALE = timedelta(minutes=15)    # a DWD run older than KNMI's by more is left out


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def stamp(t):
    return f"{t:%Y%m%d%H%M}"


def mmh(b):
    b = b.astype(np.float64)
    return np.where(b == 0, 0.0, 10 ** (-1 + (b - 1) / 254 * math.log10(400)))


def encode(v):
    out = np.zeros(v.shape, np.uint8)
    wet = v >= 0.1
    out[wet] = np.clip(1 + np.round((np.log10(v[wet]) + 1) / math.log10(400) * 254), 1, 255)
    return out


def dwd_lookup(m):
    """For each KNMI grid pixel, the DWD map pixel at its centre (both Web Mercator)."""
    xs = X0 + (np.arange(W) + 0.5) * (X1 - X0) / W
    ys = Y1 - (np.arange(H) + 0.5) * (Y1 - Y0) / H
    c = np.floor((xs - m["x0"]) / (m["x1"] - m["x0"]) * m["w"]).astype(int)
    r = np.floor((m["y1"] - ys) / (m["y1"] - m["y0"]) * m["h"]).astype(int)
    rr, cc = np.meshgrid(r, c, indexing="ij")
    inside = (rr >= 0) & (rr < m["h"]) & (cc >= 0) & (cc < m["w"])
    return np.where(inside, rr, 0), np.where(inside, cc, 0), inside


def ramp(mask):
    """0 at the edge of the mask → 1 at FEATHER pixels inside (0 outside)."""
    d = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    return np.minimum(1.0, d / FEATHER)


def build(out, obs, fc, ref, dwd_meta, kvalid):
    """Merged frames for the timeline. obs/fc: KNMI's entries ({"t", "file"});
    ref: KNMI's run; kvalid: KNMI's coverage on the grid. Returns (index
    block, merged forecast grids in fc order)."""
    m = (dwd_meta or {}).get("map")
    dref = parse(dwd_meta["ref"]) if m else None
    use = bool(m) and dref >= ref - STALE
    load = lambda name: np.array(Image.open(out / name))  # noqa: E731

    dvalid = np.zeros((H, W), bool)
    if use:
        rows, cols, inside = dwd_lookup(m)
        dobs = {e["t"]: e["file"] for e in m["obs"]}
        base = dobs.get(dwd_meta["ref"])
        if base and (out / base).exists():
            dvalid = inside & (load(base)[rows, cols] != 255)
        else:
            use = False

    def on_grid(name):
        """A DWD map frame on KNMI's grid (255 = no data), or None."""
        if not use or not name or not (out / name).exists():
            return None
        g = load(name)[rows, cols]
        g[~inside] = 255
        return g

    wk, wd = ramp(kvalid), ramp(dvalid)

    # Past hour: KNMI where it covers, DWD outside.
    mix_obs = []
    for e in obs:
        name = f"mix-obs-{e['t'][:16].replace('-', '').replace(':', '').replace('T', '')}.png"
        if not (out / name).exists():
            k = load(e["file"])
            d = on_grid(dobs.get(e["t"])) if use else None
            if d is not None:
                fill = ~kvalid & (d != 255)
                k = k.copy()
                k[fill] = d[fill]
            Image.fromarray(k, "L").save(out / name, optimize=True)
        mix_obs.append({"t": e["t"], "file": name})

    # Forecast: weighted mean where both cover.
    mix_fc, grids = [], []
    tag = stamp(dref) if use else "none"
    for e in fc:
        t = parse(e["t"])
        name = f"mix-fc-{stamp(ref)}-{tag}-{stamp(t)}.png"
        if (out / name).exists():
            g = load(name)
        else:
            k = load(e["file"])
            d = None
            if use:
                j = round((t - dref) / STEP)
                d = on_grid(m["fc"][j - 1] if 1 <= j <= len(m["fc"]) else dobs.get(e["t"]) if j == 0 else None)
            if d is None:
                g = k
            else:
                dv = d != 255
                a, b = wk, np.where(dv, wd, 0.0)
                tot = a + b
                with np.errstate(invalid="ignore", divide="ignore"):
                    v = np.where(tot > 0, (a * mmh(k) + b * mmh(np.where(dv, d, 0))) / tot, 0.0)
                g = encode(v)
            Image.fromarray(g, "L").save(out / name, optimize=True)
        mix_fc.append({"t": e["t"], "file": name})
        grids.append(g)

    # KNMI's own forecast in the Netherlands window, for the readout.
    win = f"knmi-win-{stamp(ref)}.png"
    if not (out / win).exists():
        y0, x0 = dwd.WINDOW["y0"], dwd.WINDOW["x0"]
        frames = []
        for e in fc:
            if parse(e["t"]) <= ref:
                continue
            k = np.minimum(load(e["file"])[y0:y0 + dwd.WINDOW["h"], x0:x0 + dwd.WINDOW["w"]], 254)   # 255 = no data
            k[~kvalid[y0:y0 + dwd.WINDOW["h"], x0:x0 + dwd.WINDOW["w"]]] = 255
            frames.append(k)
        Image.fromarray(np.vstack(frames), "L").save(out / win, optimize=True)

    # Where either source sees: the hatching's outline.
    frame = np.where(kvalid | dvalid, 0, 255).astype(np.uint8)
    rings = dwd.map_coverage(frame, {"x0": X0, "y0": Y0, "x1": X1, "y1": Y1, "w": W, "h": H})
    block = {"obs": mix_obs, "fc": mix_fc, "dwd": dwd_meta["ref"] if use else None,
             "coverage": rings, "knmi_win": win, "win": dwd.WINDOW, "knmi_ref": f"{ref:%Y-%m-%dT%H:%M:%SZ}"}
    return block, grids
