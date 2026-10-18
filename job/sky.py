"""The sky between the rain: clouds from satellite, and measured sunshine.

Radar only sees falling rain. A grey drizzly day and a sunny day with passing
showers can look the same on it; the difference is in the clouds and the sun.

Clouds: a cloud field (0 = clear … 1 = dense cloud) every 10 minutes for a wide
area (~25°W–40°E, 34–66°N, one grid), which the app draws itself over the
basemap, the same way day and night, and moves like the rain. Drawing the
satellite picture (GeoColour) instead meant moving clouds and ground together:
city lights and coasts got dragged, low clouds stuck, tiers showed seams.
The field combines a cloud mask (a derived product: it catches low, warm
clouds and fog that infrared alone misses) with Meteosat Third Generation's
infrared (10.5 µm; colder = higher, denser cloud) for shading.

The mask is MTG's own (FCI-2-CLM, every 10 minutes, ~2 km at nadir), from
EUMETSAT's Data Store (needs a free account: EUMETSAT_KEY / EUMETSAT_SECRET),
published ~10 minutes after its slot. Checked against KNMI stations' cloud
cover on 2026-10-13, 08:30–21:00 (679 station-times): correlation 0.82, vs
0.73 for Meteosat 0°'s mask (msg_fes:clm) and 0.69 for its Rapid Scan mask;
by night it caught 42 of 73 overcast moments and called 8 of 132 clear ones
cloudy (MSG: 38 and 16). Without the key, or when the Data Store fails, the
job falls back to MSG's mask from the public WMS (every 15 minutes, images
~25–45 minutes late: the WMS lags the Data Store by ~20 minutes). The WMS
needs an explicit time (it snaps any time to its nearest image and returns an
old default without one). Infrared comes from the WMS only, so the newest
fields are first shaded with the newest infrared available and shaded again
once their own arrives. Core data, CC BY 4.0 ("Contains modified EUMETSAT
Meteosat data").

Stations: KNMI's 10-minute observations (sunshine minutes, cloud cover, weather
code) at ~32 Dutch stations, for the measured past hour on the app's sun band.

Models: whether any weather model forecasts sun well enough to show is not
known yet (on 2026-10-13 KNMI's HARMONIE missed a sunny afternoon in the south
entirely). So every hour the job stores several models' sun forecasts for the
stations' locations, and scores them against the stations once the hour is
measured: sun fraction per hour (0 = no sun, 1 = sunny throughout), by lead
time, plus "persistence" (the last measured hour, carried forward) as the
baseline a model has to beat.

State in OUT/state/sky.json; for the app OUT/sun.json (stations, past hour)
and the cloud fields; the scores in OUT/sky.json.
"""

import base64
import hashlib
import io
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
from PIL import Image

WMS = "https://view.eumetsat.int/geoserver/wms"
MASK_LAYER = "msg_fes:clm"           # cloud mask: white = cloud, green/blue = clear land/sea
IR_LAYER = "mtg_fd:ir105_hrfi"
IR_CLEAR, IR_CLOUD = 52, 70          # grey levels of the service's IR rendering (clear 37–52, cloud ≥ ~50)
CLOUD_BBOX = (-2782987, 4028802, 4452780, 9876846)   # Web Mercator, ~25°W–40°E, 34–66°N
CLOUD_W, CLOUD_H = 1200, 969         # ~6 Mercator km per pixel (~3.7 km on the ground here)
CLOUD_STEP = timedelta(minutes=10)   # MTG's rhythm (the WMS fallback: 15 minutes)
WMS_STEP = timedelta(minutes=15)
DS = "https://api.eumetsat.int"
MTG_CLM = "EO:EUM:DAT:0678"          # FCI-2-CLM, netCDF, full disc
# cloud_state classes (enum in the file): 0 not processed, 1 cloud free,
# 2 cloud contaminated (partial or thin), 3 cloud filled (opaque), 4..9 dust,
# ash, snow/ice, undefined. Partial cloud counts as cloud, a bit thinner:
# against the stations, opaque alone caught only 219 of 475 overcast moments.
MTG_DENSITY = np.array([0, 0, 0.8, 1, 0, 0, 0, 0, 0, 0], np.float32)
MTG_N = 5568                         # columns and rows of the 2 km grid
CLOUD_LOW = 0.55                     # a cloudy pixel's least density (warm, low cloud)
SAT_BUDGET = 60                      # seconds per run for satellite requests
# Cloud motion between consecutive fields: optical flow (OpenCV Farnebäck) on
# the field with local contrast enhanced (CLAHE; a uniform deck otherwise has
# nothing to follow, so only its edges move), reduced to a FLOW_GX × FLOW_GY
# grid (block medians; blocks with little texture take the motion of textured
# ones around them; median filter, speed cap, light blur) in field pixels per
# 10 minutes. Tested on 2026-10-13 evening fields: drifting along the mean of
# the last two beat a frozen field at 30–90 min (wrong cloud/clear at 60 min
# 10.2% vs 10.9%; much of the change was low cloud forming and dissolving).
FLOW_GX, FLOW_GY = 48, 39
FLOW_CAP_KM = 50                     # Mercator km per 10 minutes
FLOW_SCALE = 4                       # PNG byte steps per pixel (128 = no motion)
FLOW_FILL = 6                        # blocks (Gaussian sigma) for filling textureless blocks

STN = "10-minute-in-situ-meteorological-observations/versions/1.0"
STN_STEP = timedelta(minutes=10)
STN_PER_RUN = 3                  # downloads per run (catches up after gaps)
NL = (50.6, 53.7, 3.2, 7.3)      # lat0, lat1, lon0, lon1

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
MODELS = ["knmi_seamless", "icon_d2", "ukmo_seamless", "ecmwf_ifs025", "meteofrance_seamless"]
LEAD_BINS = [(1, 3), (4, 6), (7, 12)]   # hours ahead
SUNNY = 0.5                      # an hour with at least half its time in sunshine
MIN_ELEV = 10                    # degrees; lower sun is left out of scoring
DAYS = 7
KEEP_DAYS = 14

# Must match app.js / build.py.
X0, Y0, X1, Y1 = 0, 6257115.396264, 1208534.924976, 7553162.131589
W, H = 700, 751

# Drizzle and rain the radar misses: drizzle falls from cloud a few hundred
# metres up, below the radar beam, in drops too small to echo. KNMI's stations
# report it (WaWa weather code ww). Checked on 2026-10-18, a drizzly day (32
# stations, 02:30-15:20, 821 station-times with the MTG mask): drizzle came
# from opaque cloud 94% of the time, and clear sky had drizzle 1 in 81, so
# clearing cloud ends it; but opaque cloud drizzled only 18% of the time and
# infrared doesn't tell which, so drizzle can't be read from clouds. It is
# patchy (the nearest station, ~33 km, also drizzled 20% of the time) and it
# lasts (still wet 60 min later 58%, vs 23% for a dry station). So the app
# shows stations' reports (not a filled-in map) and, while a station near the
# point drizzles, "drizzle possible" for as long as the drifted cloud field
# keeps it under cloud. wet-log.json keeps ww, radar and cloud at each station
# for checking this over more days.
CLEAR_BELOW = 0.5               # cloud field value under which drizzle is unlikely (opaque cloud ≥ CLOUD_LOW)
DRIFT_STEPS = 13                # +0..+120 min, every 10 min
WET_KEEP_DAYS = 14


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def floor(t, step):
    s = int(step.total_seconds())
    return datetime.fromtimestamp(int(t.timestamp()) // s * s, timezone.utc)


# ---------- clouds ----------

def layer_latest(layer):
    """Newest time of a WMS layer, from the layer's own (small) capabilities."""
    ws, name = layer.split(":")
    url = f"https://view.eumetsat.int/geoserver/{ws}/{name}/ows?service=WMS&version=1.3.0&request=GetCapabilities"
    with urllib.request.urlopen(url, timeout=20) as r:
        caps = r.read().decode()
    dim = caps.split('<Dimension name="time"', 1)[1].split(">", 1)[1].split("<", 1)[0]
    return parse(dim.split("/")[1].replace(".000Z", "Z"))


def wms(layer, t, fmt):
    q = (f"service=WMS&version=1.3.0&request=GetMap&layers={layer}&styles=&crs=EPSG:3857"
         f"&bbox={','.join(str(v) for v in CLOUD_BBOX)}&width={CLOUD_W}&height={CLOUD_H}&format={fmt}&time={iso(t)}")
    with urllib.request.urlopen(f"{WMS}?{q}", timeout=25) as r:
        body = r.read()
        if not r.headers.get("Content-Type", "").startswith("image/"):
            raise ValueError(r.headers.get("Content-Type"))
    return body


def shade(cloud, ir):
    """Cloudiness 0..1 × infrared grey levels (None: no infrared) → field 0..1."""
    dense = 0.5 * cloud if ir is None else np.clip((ir - IR_CLEAR) / (IR_CLOUD - IR_CLEAR), 0, 1)
    return np.clip(cloud * (CLOUD_LOW + (1 - CLOUD_LOW) * dense), 0, 1)


def infrared(t):
    return np.asarray(Image.open(io.BytesIO(wms(IR_LAYER, t, "image/jpeg"))).convert("L"), np.float32)


def cloud_field(t):
    """MSG mask from the WMS → (field 0..1 as float array, the mask image's bytes) for time t."""
    mask_png = wms(MASK_LAYER, t, "image/png")
    ir_t = t - timedelta(minutes=5) if t.minute % 10 == 5 else t      # infrared every 10 min
    c = np.asarray(Image.open(io.BytesIO(mask_png)).convert("RGB")).astype(np.int16)
    mask = ((c[..., 0] > 200) & (c[..., 1] > 200) & (c[..., 2] > 200)).astype(np.float32)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.0)                        # the mask is blocky (~5 km here)
    return shade(mask, infrared(ir_t)), mask_png


# ----- MTG's mask from the Data Store -----

_token = {"value": None, "until": 0.0}


def ds_token():
    import time
    if time.time() > _token["until"]:
        auth = base64.b64encode(f"{os.environ['EUMETSAT_KEY']}:{os.environ['EUMETSAT_SECRET']}".encode()).decode()
        req = urllib.request.Request(f"{DS}/token", data=b"grant_type=client_credentials",
                                     headers={"Authorization": "Basic " + auth})
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
        _token.update(value=tok["access_token"], until=time.time() + tok["expires_in"] - 60)
    return _token["value"]


def mtg_slots(oldest):
    """{field time (the slot's end; the Netherlands is scanned near the end of
    the slot): netCDF download URL} for MTG masks since oldest."""
    q = urllib.parse.urlencode({"format": "json", "pi": MTG_CLM, "dtstart": iso(oldest - CLOUD_STEP), "c": 100})
    with urllib.request.urlopen(f"{DS}/data/search-products/1.0.0/os?{q}", timeout=20) as r:
        feats = json.load(r)["features"]
    slots = {}
    for f in feats:
        end = parse(f["properties"]["date"].split("/")[1])
        nc = [e["href"] for e in f["properties"]["links"]["sip-entries"] if e["title"].endswith(".nc")]
        if nc and end >= oldest:
            slots[iso(end)] = nc[0]
    return slots


_lut = {}


def mtg_lut():
    """Cloud grid → MTG grid: (window rows, window cols, map x, map y) for cv2.remap."""
    if not _lut:
        import pyproj
        h = 35786400.0
        tr = pyproj.Transformer.from_crs("EPSG:3857", pyproj.CRS.from_proj4(
            f"+proj=geos +h={h} +lon_0=0 +sweep=y +a=6378137 +b=6356752 +units=m"), always_xy=True)
        x0, y0, x1, y1 = CLOUD_BBOX
        mx = x0 + (np.arange(CLOUD_W) + 0.5) * (x1 - x0) / CLOUD_W
        my = y1 - (np.arange(CLOUD_H) + 0.5) * (y1 - y0) / CLOUD_H
        gx, gy = np.meshgrid(mx, my)
        X, Y = tr.transform(gx, gy)
        # The file's x attributes (scale -5.5888e-5 rad, offset 0.15562) would
        # put the columns east → west; they are stored west → east (checked on
        # the day/night boundary). Rows run south → north.
        step = 5.5887776e-05
        col = (np.asarray(X) / h + 0.15561952) / step - 1     # index v = 1..N at v * step - 0.15562
        row = (np.asarray(Y) / h + 0.15561952) / step - 1
        col, row = col.astype(np.float32), row.astype(np.float32)
        ok = np.isfinite(col) & np.isfinite(row)
        r0, r1 = int(np.nanmin(row[ok])) - 2, int(np.nanmax(row[ok])) + 3
        c0, c1 = int(np.nanmin(col[ok])) - 2, int(np.nanmax(col[ok])) + 3
        r0, c0, r1, c1 = max(r0, 0), max(c0, 0), min(r1, MTG_N), min(c1, MTG_N)
        col[~ok] = -10; row[~ok] = -10
        _lut.update(rows=slice(r0, r1), cols=slice(c0, c1), mx=col - c0, my=row - r0)
    return _lut


def mtg_cloud(url):
    """Cloudiness 0..1 on the cloud grid from one MTG mask file."""
    import h5py
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + ds_token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    lut = mtg_lut()
    with h5py.File(io.BytesIO(body)) as f:
        cls = f["cloud_state"][lut["rows"], lut["cols"]].astype(np.int16)
    d = MTG_DENSITY[np.clip(cls, 0, 9)]
    d = cv2.GaussianBlur(d, (0, 0), 0.8)                              # 2 km pixels → ~3.7 km cells
    return cv2.remap(d, lut["mx"], lut["my"], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def clouds_mtg(out, have, oldest, start):
    """Adds MTG fields to have (dict by time). The newest are shaded with the
    newest infrared there is ("ir" < their slot's start) and redone once
    their own is out. Returns True if any field changed."""
    slots = mtg_slots(oldest)
    try:
        ir_newest = layer_latest(IR_LAYER)
    except Exception as e:
        print("infrared unavailable:", e)
        ir_newest = None
    changed = False
    for ts in sorted(slots, reverse=True):
        if (datetime.now(timezone.utc) - start).total_seconds() > SAT_BUDGET:
            break
        t = parse(ts)
        want = t - CLOUD_STEP                                         # infrared of the same slot
        ir_t = None if ir_newest is None else min(want, floor(ir_newest, CLOUD_STEP))
        if ir_t is not None and want - ir_t > timedelta(minutes=30):
            ir_t = None
        e = have.get(ts)
        if e and e.get("src") == "mtg" and e.get("ir") in (iso(want), iso(ir_t) if ir_t else None):
            continue                                                  # done, or no better infrared yet
        try:
            field = shade(mtg_cloud(slots[ts]), infrared(ir_t) if ir_t else None)
            name = f"cloud-{t:%Y%m%d%H%M}.webp"
            Image.fromarray((field * 255).astype(np.uint8), "L").save(out / name, quality=60)
            have[ts] = {"t": ts, "file": name, "m": hashlib.md5(slots[ts].encode()).hexdigest()[:12], "src": "mtg",
                        "ir": iso(ir_t) if ir_t else None}
            before = [k for k in have if k < ts]
            if e and before:
                have[max(before)].pop("flow", None)                   # its motion towards this field
            changed = True
            print("clouds", ts, "infrared", iso(ir_t) if ir_t else "none")
        except Exception as ex:   # retried next run
            print("clouds failed", ts, getattr(ex, "code", ex))
    return changed


def clouds(out, old, oldest):
    """Cloud fields from oldest to the newest out: MTG's mask from the Data
    Store when there's a key, else (or when it fails) MSG's from the WMS.
    Returns [{"t", "file", "m"}] (m: identifies the mask image); entries with
    "gap" mark WMS times without a mask of their own. The WMS answers any time
    with its nearest image, so a copy of a neighbour's mask is a gap (kept, so
    it isn't asked for again, except at the newest end: retried next run)."""
    have = {e["t"]: e for e in old if e["t"] >= iso(oldest) and (e.get("gap") or (out / e.get("file", "")).exists())}
    real = [t for t, e in have.items() if not e.get("gap")]
    have = {t: e for t, e in have.items() if not e.get("gap") or (real and t < max(real))}
    start = datetime.now(timezone.utc)
    if os.environ.get("EUMETSAT_KEY") and os.environ.get("EUMETSAT_SECRET"):
        try:
            if clouds_mtg(out, have, oldest, start):
                for f in out.glob("cloud-motion-*.png"):              # a field changed: redo the drift motion
                    f.unlink()
            entries = sorted(have.values(), key=lambda e: e["t"])
            if any(e.get("src") == "mtg" for e in entries):
                return entries
        except Exception as e:
            print("MTG clouds unavailable, falling back to the WMS:", e)
    try:
        newest = min(layer_latest(MASK_LAYER), layer_latest(IR_LAYER) + timedelta(minutes=5))
    except Exception as e:
        print("clouds unavailable:", e)
        return sorted(have.values(), key=lambda e: e["t"])
    t = floor(newest, WMS_STEP)
    while t >= oldest and (datetime.now(timezone.utc) - start).total_seconds() < SAT_BUDGET:
        if iso(t) not in have:
            try:
                field, mask_png = cloud_field(t)
                name = f"cloud-{t:%Y%m%d%H%M}.webp"
                Image.fromarray((field * 255).astype(np.uint8), "L").save(out / name, quality=60)
                have[iso(t)] = {"t": iso(t), "file": name, "m": hashlib.md5(mask_png).hexdigest()[:12]}
                print("clouds", iso(t))
            except Exception as ex:   # retried next run
                print("clouds failed", iso(t), getattr(ex, "code", ex))
        t -= WMS_STEP
    entries = sorted(have.values(), key=lambda e: e["t"])
    prev = None
    for k, e in enumerate(entries):
        if e.get("gap"):
            continue
        if prev is not None and e.get("m") == prev:
            (out / e.pop("file")).unlink(missing_ok=True)
            e.pop("flow", None)
            e["gap"] = True
            if all(x.get("gap") for x in entries[k:]):
                return entries[:k]
        prev = e.get("m")
    return entries


def flow_field(pa, pb, minutes):
    """Cloud motion from field a to field b: FLOW_GY × FLOW_GX grid of (dx, dy)
    in field pixels per 10 minutes."""
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    a = clahe.apply(np.asarray(Image.open(pa).convert("L")))
    b = clahe.apply(np.asarray(Image.open(pb).convert("L")))
    f = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 4, 21, 3, 7, 1.5, 0) * 10 / minutes
    af = a.astype(np.float32)
    texture = np.hypot(cv2.Sobel(af, cv2.CV_32F, 1, 0, ksize=5), cv2.Sobel(af, cv2.CV_32F, 0, 1, ksize=5))
    h, w = f.shape[:2]
    g = np.zeros((FLOW_GY, FLOW_GX, 2), np.float32)
    conf = np.zeros((FLOW_GY, FLOW_GX), np.float32)
    for j in range(FLOW_GY):
        for i in range(FLOW_GX):
            sl = (slice(j * h // FLOW_GY, (j + 1) * h // FLOW_GY), slice(i * w // FLOW_GX, (i + 1) * w // FLOW_GX))
            g[j, i] = np.median(f[sl].reshape(-1, 2), axis=0)
            conf[j, i] = texture[sl].mean()
    conf = np.clip(conf / max(np.percentile(conf, 75), 1e-6), 0, 1)
    num = np.stack([cv2.GaussianBlur(g[..., c] * conf, (0, 0), FLOW_FILL) for c in range(2)], -1)
    around = num / np.maximum(cv2.GaussianBlur(conf, (0, 0), FLOW_FILL), 1e-6)[..., None]
    g = conf[..., None] * g + (1 - conf[..., None]) * around
    g = np.stack([cv2.medianBlur(np.ascontiguousarray(g[..., c]), 3) for c in range(2)], -1)
    cap = FLOW_CAP_KM / ((CLOUD_BBOX[2] - CLOUD_BBOX[0]) / CLOUD_W / 1000)
    speed = np.hypot(g[..., 0], g[..., 1])
    g *= np.minimum(1, cap / np.maximum(speed, 1e-6))[..., None]
    return cv2.GaussianBlur(g, (3, 3), 0)


def save_flow(g, path):
    rgba = np.zeros((*g.shape[:2], 4), np.uint8)
    rgba[..., :2] = np.clip(np.round(128 + g * FLOW_SCALE), 0, 255)
    rgba[..., 3] = 255
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def load_flow(path):
    return (np.asarray(Image.open(path), np.float32)[..., :2] - 128) / FLOW_SCALE


def wet_kind(ww):
    """WaWa code → "drizzle", "recent" (drizzle in the past hour, not now: 22;
    the sensor's gaps when drops get too fine), "rain" or None."""
    if ww is None:
        return None
    ww = int(ww)
    if 50 <= ww <= 58:
        return "drizzle"
    if ww == 22:
        return "recent"
    if 40 <= ww <= 42 or 60 <= ww <= 69 or 80 <= ww <= 99:
        return "rain"
    return None


def merc(lat, lon):
    return lon * 20037508.34 / 180, math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137


def cloud_drift(out, entries, motion, points):
    """The newest cloud field drifted along the motion, like the app draws it,
    sampled at points ({key: (lat, lon)}): → (field time, {key: [0..255 per
    10 minutes, +0..+120]})."""
    real = [e for e in entries if e.get("file")]
    if not real or not motion or not points:
        return None, {}
    f = np.asarray(Image.open(out / real[-1]["file"]).convert("L"), np.float32)
    g = load_flow(out / motion)
    fh, fw = f.shape
    gh, gw = g.shape[:2]

    def flow(x, y):   # bilinear, field pixels per 10 minutes
        gx = min(max(x / fw * gw - 0.5, 0), gw - 1.001)
        gy = min(max(y / fh * gh - 0.5, 0), gh - 1.001)
        i, j = int(gx), int(gy)
        a, b = gx - i, gy - j
        return ((1 - a) * (1 - b) * g[j, i] + a * (1 - b) * g[j, i + 1] + (1 - a) * b * g[j + 1, i] + a * b * g[j + 1, i + 1])

    res = {}
    for key, (lat, lon) in points.items():
        mx, my = merc(lat, lon)
        x = (mx - CLOUD_BBOX[0]) / (CLOUD_BBOX[2] - CLOUD_BBOX[0]) * fw
        y = (CLOUD_BBOX[3] - my) / (CLOUD_BBOX[3] - CLOUD_BBOX[1]) * fh
        vals, qx, qy = [], x, y
        for k in range(DRIFT_STEPS):
            if k:   # trace one more 10-minute step back along the motion
                dx, dy = flow(qx, qy)
                qx, qy = qx - dx, qy - dy
            i, j = int(round(qx)), int(round(qy))
            vals.append(int(f[j - 1:j + 2, i - 1:i + 2].mean()) if 1 <= i < fw - 1 and 1 <= j < fh - 1 else 0)
        res[key] = vals
    return real[-1]["t"], res


def cloud_motion(out, entries):
    """Adds each field's motion towards the next one ("flow") and returns the
    motion for drifting the newest field forward: the mean of the last two
    (file name, or None)."""
    real = [e for e in entries if e.get("file")]
    fields = []
    for a, b in zip(real, real[1:]):
        minutes = (parse(b["t"]) - parse(a["t"])).total_seconds() / 60
        if minutes > 45:
            continue
        name = f"cloud-flow-{a['t'][:16].replace('-', '').replace(':', '').replace('T', '')}-{b['t'][11:16].replace(':', '')}.png"
        if a.get("flow") != name or not (out / name).exists():
            try:
                save_flow(flow_field(out / a["file"], out / b["file"], minutes), out / name)
            except Exception as ex:
                print("cloud motion failed", a["t"], ex)
                continue
            a["flow"] = name
        fields.append(name)
    if not fields:
        return None
    name = f"cloud-motion-{real[-1]['t'][:16].replace('-', '').replace(':', '').replace('T', '')}.png"
    if not (out / name).exists():
        save_flow(np.mean([load_flow(out / f) for f in fields[-2:]], axis=0), out / name)
    return name


# ---------- sun position ----------

def elevation(t, lat, lon):
    """Solar elevation in degrees (good to ~1°, plenty here)."""
    d = t.timetuple().tm_yday
    h = t.hour + t.minute / 60 + t.second / 3600
    decl = math.radians(23.44) * math.sin(2 * math.pi / 365 * (d - 81))
    ha = math.radians((h + lon / 15 - 12) * 15)
    la = math.radians(lat)
    s = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1, min(1, s))))


def clear_dni(elev):
    """Direct normal irradiance under a clear sky (W/m², Meinel)."""
    if elev <= 0:
        return 0.0
    am = 1 / math.sin(math.radians(max(elev, 2)))
    return 1353 * 0.7 ** (am ** 0.678)


# ---------- stations, model forecasts, scores ----------

class Sky:
    def __init__(self, out, download):
        self.out = out
        self.download = download
        self.path = out / "state" / "sky.json"
        s = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.stations = s.get("stations", {})   # WMO id → [name, lat, lon]
        self.obs = s.get("obs", {})             # 10-min time → {id: [ss min, cloud okta, ww code]}
        self.missing = s.get("missing", [])     # 10-min times KNMI never published
        self.fc = s.get("fc", {})               # issue hour → {model: {hour: [sun fraction per station or None]}}
        self.days = s.get("days", {})           # day → {model: [[abs err, n, hit, miss, fa, cn] per lead bin]}
        self.scored = s.get("scored", "")       # last hour scored

    # A station file every 10 minutes, ~3 minutes after its time.
    def update_stations(self, now):
        oldest = floor(now - timedelta(hours=13), STN_STEP)   # enough for scoring persistence and late hours
        t = floor(now, STN_STEP)
        todo = []
        while t > oldest:
            if iso(t) not in self.obs and iso(t) not in self.missing:
                todo.append(t)
            t -= STN_STEP
        for t in todo[:STN_PER_RUN]:      # newest first
            try:
                f = self.download(STN, f"KMDS__OPER_P___10M_OBS_L2_{t:%Y%m%d%H%M}.nc")
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    if now - t > timedelta(minutes=30):
                        self.missing.append(iso(t))
                    continue
                raise
            ids = [str(int(x)) if not isinstance(x, (bytes, str)) else (x.decode() if isinstance(x, bytes) else x).strip()
                   for x in f["station"][:]]
            names = [(x.decode() if isinstance(x, bytes) else str(x)).strip() for x in f["stationname"][:]]
            lat, lon = f["lat"][:], f["lon"][:]
            ss, n, ww = f["ss"][:, 0], f["n"][:, 0], f["ww"][:, 0]
            row = {}
            for i, sid in enumerate(ids):
                if not (NL[0] < lat[i] < NL[1] and NL[2] < lon[i] < NL[3]):
                    continue
                val = lambda a: None if not np.isfinite(a[i]) else round(float(a[i]), 1)  # noqa: E731
                if val(ss) is None and val(n) is None:
                    continue
                self.stations.setdefault(sid, [names[i], round(float(lat[i]), 3), round(float(lon[i]), 3)])
                row[sid] = [val(ss), val(n), val(ww)]
            self.obs[iso(t)] = row
            print("stations", iso(t), len(row))
        self.obs = {k: v for k, v in self.obs.items() if k > iso(oldest)}
        self.missing = [k for k in self.missing if k > iso(oldest)]

    def log_wet(self, mix_obs, clouds):
        """Adds each station's weather code, radar (merged observation, max
        over ~3 km) and cloud field value to state/wet-log.json, for the 10-minute
        times that have all three; kept WET_KEEP_DAYS."""
        path = self.out / "state" / "wet-log.json"
        log = json.loads(path.read_text()) if path.exists() else {}
        radar = {e["t"]: e["file"] for e in mix_obs or []}
        fields = [e for e in clouds or [] if e.get("file")]
        for t, row in sorted(self.obs.items()):
            if t in log or t not in radar or not (self.out / radar[t]).exists():
                continue
            near = [e for e in fields if e["t"] <= t and parse(t) - parse(e["t"]) <= timedelta(minutes=20)]
            if not near:
                continue
            a = np.asarray(Image.open(self.out / radar[t]))
            c = np.asarray(Image.open(self.out / near[-1]["file"]).convert("L"))
            entry = {}
            for sid, r in row.items():
                if r[2] is None or sid not in self.stations:
                    continue
                _, lat, lon = self.stations[sid]
                mx, my = merc(lat, lon)
                x, y = int((mx - X0) / (X1 - X0) * W), int((Y1 - my) / (Y1 - Y0) * H)
                cx = int((mx - CLOUD_BBOX[0]) / (CLOUD_BBOX[2] - CLOUD_BBOX[0]) * c.shape[1])
                cy = int((CLOUD_BBOX[3] - my) / (CLOUD_BBOX[3] - CLOUD_BBOX[1]) * c.shape[0])
                entry[sid] = [int(r[2]), int(a[y - 1:y + 2, x - 1:x + 2].max()), int(c[cy - 1:cy + 2, cx - 1:cx + 2].mean())]
            log[t] = entry
        cutoff = iso(datetime.now(timezone.utc) - timedelta(days=WET_KEEP_DAYS))
        log = {t: v for t, v in log.items() if t >= cutoff}
        path.write_text(json.dumps(log, separators=(",", ":")))

    def sun_ids(self):
        return sorted(sid for sid in self.stations if any((r.get(sid) or [None])[0] is not None for r in self.obs.values()))

    def hour_obs(self, hour):
        """Measured sun fraction per station for the hour ending at `hour`
        (needs all six 10-minute values)."""
        times = [iso(hour - k * STN_STEP) for k in range(6)]
        if not all(t in self.obs for t in times):
            return None
        out = {}
        for sid in self.sun_ids():
            v = [self.obs[t].get(sid, [None])[0] for t in times]
            if all(x is not None for x in v):
                out[sid] = min(1.0, sum(v) / 60)
        return out

    # Every hour: each model's sun forecast for the next 12 hours at the stations.
    def update_forecasts(self, now):
        issue = floor(now, timedelta(hours=1))
        if iso(issue) in self.fc:
            return
        ids = self.sun_ids()
        if not ids:
            return
        lat = ",".join(str(self.stations[s][1]) for s in ids)
        lon = ",".join(str(self.stations[s][2]) for s in ids)
        url = (f"{OPEN_METEO}?latitude={lat}&longitude={lon}&hourly=direct_normal_irradiance"
               f"&models={','.join(MODELS)}&forecast_hours=13&timezone=UTC")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                res = json.load(r)
        except Exception as e:
            print("open-meteo", e)
            return
        res = res if isinstance(res, list) else [res]
        per = {m: {} for m in MODELS}
        for k, sid in enumerate(ids):
            h = res[k]["hourly"]
            for j, ts in enumerate(h["time"]):
                end = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                # Hourly radiation is the mean over the hour ending at `end`.
                if end <= issue:
                    continue
                el = elevation(end - timedelta(minutes=30), self.stations[sid][1], self.stations[sid][2])
                for m in MODELS:
                    v = h.get(f"direct_normal_irradiance_{m}", [None] * len(h["time"]))[j]
                    row = per[m].setdefault(iso(end), [None] * len(ids))
                    if v is not None and el >= MIN_ELEV:
                        row[k] = round(min(1.0, v / clear_dni(el)), 3)
        self.fc[iso(issue)] = {"ids": ids, **per}
        print("sun forecasts", iso(issue))

    def score(self, now):
        """Score every newly measured hour against what each model (and
        persistence) said 1 … 12 hours before."""
        hour = floor(now, timedelta(hours=1))
        start = parse(self.scored) + timedelta(hours=1) if self.scored else hour - timedelta(hours=1)
        h = start
        while h <= hour:
            obs = self.hour_obs(h)
            if obs is None:
                if now - h > timedelta(hours=1):   # never complete: skip it
                    self.scored = iso(h)
                    h += timedelta(hours=1)
                    continue
                break
            day = self.days.setdefault(h.strftime("%Y-%m-%d"), {})
            for b, (a, z) in enumerate(LEAD_BINS):
                for lead in range(a, z + 1):
                    issue = h - timedelta(hours=lead)
                    fc = self.fc.get(iso(issue))
                    prev = self.hour_obs(issue)        # persistence: the hour before the issue
                    if not fc:
                        continue
                    for k, sid in enumerate(fc["ids"]):
                        o = obs.get(sid)
                        if o is None:
                            continue
                        el = elevation(h - timedelta(minutes=30), self.stations[sid][1], self.stations[sid][2])
                        if el < MIN_ELEV:
                            continue
                        guesses = {m: (fc.get(m, {}).get(iso(h)) or [None] * len(fc["ids"]))[k] for m in MODELS}
                        if prev is not None and elevation(issue - timedelta(minutes=30), self.stations[sid][1], self.stations[sid][2]) >= MIN_ELEV:
                            guesses["persistence"] = prev.get(sid)
                        for m, f in guesses.items():
                            if f is None:
                                continue
                            row = day.setdefault(m, [[0] * 6 for _ in LEAD_BINS])[b]
                            row[0] += round(abs(f - o), 3)
                            row[1] += 1
                            fs, os_ = f >= SUNNY, o >= SUNNY
                            row[2 if fs and os_ else 3 if os_ else 4 if fs else 5] += 1
            self.scored = iso(h)
            print("sun scored", iso(h))
            h += timedelta(hours=1)

    def save(self, now, clouds=None, motion=None):
        self.fc = {k: v for k, v in self.fc.items() if k > iso(now - timedelta(hours=14))}
        cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        self.days = {d: v for d, v in self.days.items() if d >= cutoff}
        self.path.write_text(json.dumps(
            {"stations": self.stations, "obs": self.obs, "missing": self.missing, "fc": self.fc,
             "days": self.days, "scored": self.scored}, separators=(",", ":")))

        # The app: stations' measured sun, cloud cover and weather over the last hour.
        times = sorted(self.obs)[-7:]
        ids = sorted(self.stations)
        sun = {
            "t": times,
            "stations": [[sid, *self.stations[sid]] for sid in ids],
            "ss": [[(self.obs[t].get(sid) or [None])[0] for t in times] for sid in ids],
            "n": [[(self.obs[t].get(sid) or [None, None])[1] for t in times] for sid in ids],
            "ww": [[(self.obs[t].get(sid) or [None, None, None])[2] for t in times] for sid in ids],
        }
        # Stations reporting drizzle or rain in the last 20 minutes: the cloud
        # field at them over the next two hours (drizzle ends when it clears).
        wet = {sid: self.stations[sid][1:] for sid in ids
               if any(wet_kind((self.obs[t].get(sid) or [None] * 3)[2]) for t in times[-2:])}
        try:
            t0, drift = cloud_drift(self.out, clouds or [], motion, wet)
        except Exception as e:
            print("cloud drift unavailable:", e)
            t0, drift = None, {}
        if drift:
            sun["cloud"] = {"t0": t0, "step": 10, "clear": round(CLEAR_BELOW * 255), "v": drift}
        (self.out / "sun.json").write_text(json.dumps(sun, separators=(",", ":")))

        # Scores over the last DAYS days, per model and lead bin.
        since = (now - timedelta(days=DAYS)).strftime("%Y-%m-%d")
        window = [v for d, v in self.days.items() if d > since]
        summary = {"t": iso(now), "days": len(window), "sunny": SUNNY, "leads": LEAD_BINS, "models": {}}
        for m in MODELS + ["persistence"]:
            rows = []
            for b in range(len(LEAD_BINS)):
                ab, n, hit, miss, fa, cn = (sum(v.get(m, [[0] * 6] * len(LEAD_BINS))[b][i] for v in window) for i in range(6))
                ev = hit + miss + fa
                rows.append({
                    "mae": round(ab / n, 3) if n else None,       # mean error in sun fraction (0..1)
                    "csi": round(hit / ev, 3) if ev else None,    # sunny hours correctly forecast
                    "bias": round((hit + fa) / (hit + miss), 2) if hit + miss else None,
                    "sunny": hit + miss, "n": n,
                })
            summary["models"][m] = rows
        (self.out / "sky.json").write_text(json.dumps(summary, separators=(",", ":")))
