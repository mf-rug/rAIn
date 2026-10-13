"""The sky between the rain: clouds from satellite, and measured sunshine.

Radar only sees falling rain. A grey drizzly day and a sunny day with passing
showers can look the same on it; the difference is in the clouds and the sun.

Clouds: a cloud field (0 = clear … 1 = dense cloud) every 15 minutes for a wide
area (~25°W–40°E, 34–66°N, one grid), which the app draws itself over the
basemap, the same way day and night, and moves like the rain. Drawing the
satellite picture (GeoColour) instead meant moving clouds and ground together:
city lights and coasts got dragged, low clouds stuck, tiers showed seams.
The field combines EUMETSAT's cloud mask (Meteosat 0°, a derived product: it
catches low, warm clouds and fog that infrared alone misses) with Meteosat
Third Generation's infrared (10.5 µm; colder = higher, denser cloud) for
shading. Checked against KNMI stations' cloud cover, 2026-10-13 afternoon and
evening (357 station-times): the mask caught 137 of 184 overcast moments,
infrared alone 75; by night all methods are weaker (mask 35 of 67). Core data,
CC BY 4.0, no key ("Contains modified EUMETSAT Meteosat data"). Requests need
an explicit time (the service snaps any time to its nearest image and returns
an old default without one); images appear ~25–45 minutes late.

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

import io
import json
import math
import urllib.error
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
CLOUD_STEP = timedelta(minutes=15)   # the mask's rhythm; the nearest infrared is ≤ 5 min off
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


def cloud_field(t):
    """→ (field 0..1 as float array, the mask image's bytes) for time t."""
    mask_png = wms(MASK_LAYER, t, "image/png")
    ir_t = t - timedelta(minutes=5) if t.minute % 10 == 5 else t      # infrared every 10 min
    ir = np.asarray(Image.open(io.BytesIO(wms(IR_LAYER, ir_t, "image/jpeg"))).convert("L"), np.float32)
    c = np.asarray(Image.open(io.BytesIO(mask_png)).convert("RGB")).astype(np.int16)
    mask = ((c[..., 0] > 200) & (c[..., 1] > 200) & (c[..., 2] > 200)).astype(np.float32)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.0)                        # the mask is blocky (~5 km here)
    dense = np.clip((ir - IR_CLEAR) / (IR_CLOUD - IR_CLEAR), 0, 1)
    return np.clip(mask * (CLOUD_LOW + (1 - CLOUD_LOW) * dense), 0, 1), mask_png


def clouds(out, old, oldest):
    """Cloud fields from oldest to the newest out, one per 15 minutes.
    Returns [{"t", "file", "m"}] (m: a hash of the mask image); entries with
    "gap" mark times without a mask of their own. The service answers any time
    with its nearest image, so a copy of a neighbour's mask is a gap (kept, so
    it isn't asked for again, except at the newest end: retried next run)."""
    import hashlib
    have = {e["t"]: e for e in old if e["t"] >= iso(oldest) and (e.get("gap") or (out / e.get("file", "")).exists())}
    real = [t for t, e in have.items() if not e.get("gap")]
    have = {t: e for t, e in have.items() if not e.get("gap") or (real and t < max(real))}
    try:
        newest = min(layer_latest(MASK_LAYER), layer_latest(IR_LAYER) + timedelta(minutes=5))
    except Exception as e:
        print("clouds unavailable:", e)
        return sorted(have.values(), key=lambda e: e["t"])
    t = floor(newest, CLOUD_STEP)
    start = datetime.now(timezone.utc)
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
        t -= CLOUD_STEP
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

    def save(self, now):
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
