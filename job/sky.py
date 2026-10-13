"""The sky between the rain: satellite cloud images and measured sunshine.

Radar only sees falling rain. A grey drizzly day and a sunny day with passing
showers can look the same on it; the difference is in the clouds and the sun.

Satellite: EUMETSAT's GeoColour image from Meteosat Third Generation (true
colour by day, infrared clouds over city lights at night), every 10 minutes,
for the radar map's area. An "Advanced Image Product": Core data, CC BY 4.0, no
key ("Contains modified EUMETSAT Meteosat data"). A request needs an explicit
time: without one the service returns an old default image. Images appear about
12 minutes after their time; times not out yet return an error or hang, so
probes use short timeouts.

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
and the satellite images; the scores in OUT/sky.json.
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

SAT_WMS = "https://view.eumetsat.int/geoserver/wms"
SAT_LAYER = "mtg_fd:rgb_geocolour"
SAT_STEP = timedelta(minutes=10)
SAT_W, SAT_H = 1000, 1072        # the radar grid's area (≈1.2 Mercator km per pixel)
# Coarser images of wider areas under the detailed one, for the map zoomed
# out (the app loads them only when the view reaches beyond the finer one):
# key in the index entries → (Mercator bbox, width, height). Re-encoded, as
# the service's JPEGs are large for a phone.
TIERS = {
    "wide": ((-1113195, 5012341, 2782987, 8399738), 1040, 905),   # ≈10°W–25°E, 41–60°N, 3.7 km/px
    "outer": ((-2782987, 4028802, 4452780, 9876846), 1000, 808),  # ≈25°W–40°E, 34–66°N, 7.2 km/px
}
SAT_BUDGET = 60                  # seconds per run for satellite requests
# Cloud motion between consecutive images, per tier: optical flow (OpenCV
# Farnebäck), reduced to a FLOW_GRID × FLOW_GRID field (block medians, 3×3
# median filter, speed cap, light blur) in image pixels per 10 minutes. Tested
# on 2026-10-13: drifting the newest image along the mean of the last two
# fields beat a frozen image at 10–60 minutes (wrong cloud/clear pixels at
# 60 min: 11.5% vs 16.9%).
FLOW_GRID = 32
FLOW_CAP_KM = 30                 # Mercator km per 10 minutes (~110 km/h on the ground here)
FLOW_SCALE = 4                   # PNG byte steps per pixel (128 = no motion)

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


# ---------- satellite ----------

def sat_latest():
    """Newest image time, from the layer's own (small) capabilities document."""
    ws, layer = SAT_LAYER.split(":")
    url = f"https://view.eumetsat.int/geoserver/{ws}/{layer}/ows?service=WMS&version=1.3.0&request=GetCapabilities"
    with urllib.request.urlopen(url, timeout=20) as r:
        caps = r.read().decode()
    dim = caps.split('<Dimension name="time"', 1)[1].split(">", 1)[1].split("<", 1)[0]
    return parse(dim.split("/")[1].replace(".000Z", "Z"))


def sat_image(t, bbox, w, h):
    q = (f"service=WMS&version=1.3.0&request=GetMap&layers={SAT_LAYER}&styles=&crs=EPSG:3857"
         f"&bbox={','.join(str(v) for v in bbox)}&width={w}&height={h}&format=image/jpeg&time={iso(t)}")
    with urllib.request.urlopen(f"{SAT_WMS}?{q}", timeout=20) as r:
        body = r.read()
        if not r.headers.get("Content-Type", "").startswith("image/"):
            raise ValueError(r.headers.get("Content-Type"))
    return body


def satellite(out, old, oldest):
    """Satellite images from oldest to the newest out, one per 10 minutes.
    Returns [{"t", "file", "wide", "outer"}]; entries with "gap" mark times without an image.

    The service answers any time with the nearest image it has, so a time
    with no image of its own comes back as a copy of its neighbour: such
    copies are kept as gaps (and not requested again)."""
    have = {e["t"]: e for e in old if e["t"] >= iso(oldest) and (e.get("gap") or (out / e["file"]).exists())}
    real = [t for t, e in have.items() if not e.get("gap")]
    have = {t: e for t, e in have.items() if not e.get("gap") or (real and t < max(real))}
    try:
        t = floor(sat_latest(), SAT_STEP)
    except Exception as e:
        print("satellite unavailable:", e)
        return sorted(have.values(), key=lambda e: e["t"])
    start = datetime.now(timezone.utc)
    while t >= oldest and (datetime.now(timezone.utc) - start).total_seconds() < SAT_BUDGET:
        e = have.get(iso(t))
        try:
            if e is None:
                name = f"sat-{t:%Y%m%d%H%M}.jpg"
                (out / name).write_bytes(sat_image(t, (X0, Y0, X1, Y1), SAT_W, SAT_H))
                e = have[iso(t)] = {"t": iso(t), "file": name}
                print("satellite", iso(t))
            for key, (bbox, w, h) in TIERS.items():
                if "file" in e and not (e.get(key) and (out / e[key]).exists()):
                    name = f"sat-{key}-{t:%Y%m%d%H%M}.jpg"
                    Image.open(io.BytesIO(sat_image(t, bbox, w, h))).save(out / name, quality=65, optimize=True)
                    e[key] = name
        except Exception as ex:   # retried next run
            print("satellite failed", iso(t), getattr(ex, "code", ex))
        t -= SAT_STEP
    # Copies of a neighbour: keep the earlier one. A copy at the newest end is
    # dropped and asked for again next run (the capabilities can list a time
    # before its image is served).
    entries = sorted(have.values(), key=lambda e: e["t"])
    prev = None
    for k, e in enumerate(entries):
        if e.get("gap"):
            continue
        body = (out / e["file"]).read_bytes()
        if prev is not None and body == prev:
            (out / e.pop("file")).unlink()
            for key in TIERS:
                if e.get(key):
                    (out / e.pop(key)).unlink(missing_ok=True)
            e["gap"] = True
            if all(x.get("gap") for x in entries[k:]):
                return [x for x in entries[:k]]
        prev = body
    return entries


def tier_geometry():
    """key → (bbox, width, height) for every tier, the detailed one as "file"."""
    return {"file": ((X0, Y0, X1, Y1), SAT_W, SAT_H), **TIERS}


def flow_field(pa, pb, minutes, km_per_px):
    """Cloud motion from image a to image b as a grid of (dx, dy) in pixels per 10 min."""
    def grey(p, s):
        im = Image.open(p).convert("L")
        return np.asarray(im.resize((im.width // s, im.height // s), Image.BILINEAR))
    s = 2 if km_per_px < 2 else 1          # the detailed tier is fine enough at half size
    f = cv2.calcOpticalFlowFarneback(grey(pa, s), grey(pb, s), None, 0.5, 4, 21, 3, 7, 1.5, 0) * s * 10 / minutes
    h, w = f.shape[:2]
    g = np.zeros((FLOW_GRID, FLOW_GRID, 2), np.float32)
    for j in range(FLOW_GRID):
        for i in range(FLOW_GRID):
            blk = f[j * h // FLOW_GRID:(j + 1) * h // FLOW_GRID, i * w // FLOW_GRID:(i + 1) * w // FLOW_GRID]
            g[j, i] = np.median(blk.reshape(-1, 2), axis=0)
    g = np.stack([cv2.medianBlur(np.ascontiguousarray(g[..., c]), 3) for c in range(2)], -1)
    cap = FLOW_CAP_KM / km_per_px
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


def sat_motion(out, entries):
    """Adds each image's motion towards the next one ("flow": {tier: file})
    and returns the motion for drifting the newest image forward: the mean of
    the last two fields ({tier: file}, or {})."""
    real = [e for e in entries if e.get("file")]
    motion = {}
    for key, (bbox, w, h) in tier_geometry().items():
        km = (bbox[2] - bbox[0]) / w / 1000
        fields = []
        for a, b in zip(real, real[1:]):
            minutes = (parse(b["t"]) - parse(a["t"])).total_seconds() / 60
            if not a.get(key) or not b.get(key) or minutes > 30:
                continue
            name = f"sat-flow-{key}-{a['t'][:16].replace('-', '').replace(':', '').replace('T', '')}.png"
            if not (a.get("flow") or {}).get(key) or not (out / a["flow"][key]).exists():
                try:
                    save_flow(flow_field(out / a[key], out / b[key], minutes, km), out / name)
                except Exception as ex:
                    print("cloud motion failed", key, a["t"], ex)
                    continue
                a.setdefault("flow", {})[key] = name
            fields.append(a["flow"][key])
        if fields:
            name = f"sat-motion-{key}-{real[-1]['t'][:16].replace('-', '').replace(':', '').replace('T', '')}.png"
            if not (out / name).exists():
                save_flow(np.mean([load_flow(out / f) for f in fields[-2:]], axis=0), out / name)
            motion[key] = name
    return motion


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
