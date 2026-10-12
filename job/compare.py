"""Head-to-head: KNMI's nowcast vs Buienradar's, scored against what fell.

Every run (at moment F, the newest radar time) both sources' forecasts as they
stand at F are stored for a fixed set of places across the Netherlands:
Buienradar's point forecast, and KNMI's latest run read at the same places. When
the observation for time t arrives, both are scored with what they said at
F = t - 5, 10, … 120 min ("had you looked at F, who was right?"), only where
both made a forecast, with daily tallies per lead time:

  hits, misses, false alarms, correct negatives   (rain = WET_MMH or more)
  sum of absolute errors (mm/h) and number of pairs

Lead 0 is Buienradar's own recent observations (its response starts with its
last few observed steps) vs KNMI's for the same times: how much the two disagree
about rain that has already fallen. KNMI's forecast starts from the very
observations it is scored against, so it has a head start that lead 0 helps put
in proportion.

DWD's nowcast is scored too, but it only covers the eastern part of the
Netherlands, so it gets its own set ("east"): KNMI, Buienradar and DWD on the
places where all three made a forecast. Two consensus forecasts are scored in
the same set: the mean of KNMI and DWD ("mean2") and of all three ("mean3"),
to see whether combining sources beats each of them.

State in OUT/state/compare.json; summary for the app in OUT/compare.json.
"""

import json
import math
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

BUIENRADAR = "https://graphdata.buienradar.nl/2.0/forecast/geo/RainHistoryForecast"
# A 0.35° × 0.45° grid of places, kept where it falls inside the Netherlands.
POINTS = [
    (50.8, 5.75), (51.5, 3.5), (51.5, 3.95), (51.5, 4.4), (51.5, 4.85), (51.5, 5.3), (51.5, 5.75),
    (51.85, 4.4), (51.85, 4.85), (51.85, 5.3), (51.85, 5.75), (52.2, 4.4), (52.2, 4.85), (52.2, 5.3),
    (52.2, 5.75), (52.2, 6.2), (52.2, 6.65), (52.55, 4.85), (52.55, 5.3), (52.55, 5.75), (52.55, 6.2),
    (52.55, 6.65), (52.9, 4.85), (52.9, 5.3), (52.9, 5.75), (52.9, 6.2), (52.9, 6.65), (52.9, 7.1),
    (53.25, 5.75), (53.25, 6.2), (53.25, 6.65), (53.25, 7.1),
]
LEADS = 24            # compared lead times: 5 … 120 min (KNMI's range)
WET_MMH = 0.3
DAYS = 7              # summary window
KEEP_DAYS = 14
STEP = timedelta(minutes=5)

# Must match app.js / build.py.
X0, Y0, X1, Y1 = 0, 6257115.396264, 1208534.924976, 7553162.131589
W, H = 700, 751


def pixel(lat, lon):
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 6378137
    return int((Y1 - y) / (Y1 - Y0) * H), int((x - X0) / (X1 - X0) * W)


PIXELS = [pixel(la, lo) for la, lo in POINTS]


def mmh(grid):
    """Byte-encoded grid → mm/h at each of the places."""
    b = np.array([grid[r, c] for r, c in PIXELS], dtype=float)
    return np.where(b == 0, 0.0, 10 ** (-1 + (b - 1) / 254 * math.log10(400))).round(2).tolist()


def key(t):
    return t.strftime("%Y-%m-%dT%H:%MZ")


class Compare:
    def __init__(self, out):
        self.path = out / "state" / "compare.json"
        self.out = out
        s = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.br = s.get("br", {})        # F → {"obs": {t: [per place]}, "fc": {t: [per place]}}
        self.knmi = s.get("knmi", {})    # F → {t: [per place]}
        self.latest = s.get("latest", {})  # KNMI's newest run: {t: [per place]}
        self.days = s.get("days", {})    # day → {"br"|"knmi": [[h, m, f, cn, abs, n] per lead 0..24], "east": {...}}
        self.dwd = s.get("dwd", {})      # F → {t: [per place]}
        self.latest_dwd = s.get("latest_dwd", {})
        self.dwd_obs = s.get("dwd_obs", {})  # t → DWD's own analysis at t (its +0 step)
        # Drop state from the earlier, differently keyed format.
        if any(not isinstance(v, dict) or "fc" not in v for v in self.br.values()):
            self.br, self.knmi, self.days = {}, {}, {}

    def add_knmi(self, ref, grids):
        """KNMI forecast run (byte grids for leads 1..24) at the places."""
        self.latest = {key(ref + (i + 1) * STEP): mmh(g) for i, g in enumerate(grids)}

    def add_dwd(self, ref, grids):
        """DWD run (mm/h grids, NaN = no data, for +0 … +120 min) at the places."""
        vals = [[None if np.isnan(g[r, c]) else round(float(g[r, c]), 2) for r, c in PIXELS] for g in grids]
        self.dwd_obs[key(ref)] = vals[0]
        self.latest_dwd = {key(ref + i * STEP): v for i, v in enumerate(vals) if i}

    def snapshot(self, f):
        """Store all sources' forecasts as they stand at moment f."""
        self.knmi[key(f)] = self.latest
        self.dwd[key(f)] = self.latest_dwd
        obs, fc = {}, {}
        now = datetime.now(timezone.utc)
        for i, (la, lo) in enumerate(POINTS):
            try:
                with urllib.request.urlopen(f"{BUIENRADAR}?lat={la}&lon={lo}", timeout=10) as r:
                    steps = json.load(r)["forecasts"]
            except Exception as e:  # one place failing shouldn't stop the rest
                print("buienradar", la, lo, e)
                continue
            for st in steps:
                t = datetime.fromisoformat(st["utcdatetime"]).replace(tzinfo=timezone.utc)
                # Steps up to now are Buienradar's observations, later ones its forecast.
                (obs if t <= now else fc).setdefault(key(t), [None] * len(POINTS))[i] = st["precipitation"]
            time.sleep(0.1)
        self.br[key(f)] = {"obs": obs, "fc": fc}

    def score(self, t, grid):
        """Score both sources against the observation at t (byte grid)."""
        obs = mmh(grid)
        tk = key(t)
        blank = lambda: [[0] * 6 for _ in range(LEADS + 1)]  # noqa: E731
        day = self.days.setdefault(t.strftime("%Y-%m-%d"), {"br": blank(), "knmi": blank()})
        east = day.setdefault("east", {})
        for src in ("knmi", "br", "dwd", "mean2", "mean3"):
            east.setdefault(src, blank())

        def tally(row, f, o):
            fw, ow = f >= WET_MMH, o >= WET_MMH
            row[0 if fw and ow else 1 if ow else 2 if fw else 3] += 1
            row[4] += abs(f - o)
            row[5] += 1

        # Lead 0: Buienradar's own observation of t (from the newest snapshot that has it).
        for f in sorted(self.br, reverse=True):
            seen = self.br[f]["obs"].get(tk)
            if seen:
                for p, v in enumerate(seen):
                    if v is not None:
                        tally(day["br"][0], v, obs[p])
                break
        d0 = self.dwd_obs.get(tk)
        if d0:
            for p, v in enumerate(d0):
                if v is not None:
                    tally(east["dwd"][0], v, obs[p])
        for k in range(1, LEADS + 1):
            f = key(t - k * STEP)
            b = self.br.get(f, {}).get("fc", {}).get(tk)
            kn = self.knmi.get(f, {}).get(tk)
            d = self.dwd.get(f, {}).get(tk)
            if not b or not kn:
                continue
            for p in range(len(POINTS)):
                if b[p] is None or kn[p] is None:
                    continue      # only compare where both made a forecast
                tally(day["br"][k], b[p], obs[p])
                tally(day["knmi"][k], kn[p], obs[p])
                if d and d[p] is not None:
                    tally(east["knmi"][k], kn[p], obs[p])
                    tally(east["br"][k], b[p], obs[p])
                    tally(east["dwd"][k], d[p], obs[p])
                    tally(east["mean2"][k], (kn[p] + d[p]) / 2, obs[p])
                    tally(east["mean3"][k], (kn[p] + b[p] + d[p]) / 3, obs[p])

    def save(self, t0):
        old = key(t0 - timedelta(hours=2, minutes=10))
        self.br = {k: v for k, v in self.br.items() if k >= old}
        self.knmi = {k: v for k, v in self.knmi.items() if k >= old}
        self.dwd = {k: v for k, v in self.dwd.items() if k >= old}
        self.dwd_obs = {k: v for k, v in self.dwd_obs.items() if k >= old}
        cutoff = (t0 - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        self.days = {d: v for d, v in self.days.items() if d >= cutoff}
        self.path.write_text(json.dumps(
            {"br": self.br, "knmi": self.knmi, "latest": self.latest, "days": self.days,
             "dwd": self.dwd, "latest_dwd": self.latest_dwd, "dwd_obs": self.dwd_obs}, separators=(",", ":")))

        # Summary over the last DAYS days, per source and lead time.
        since = (t0 - timedelta(days=DAYS)).strftime("%Y-%m-%d")
        window = [v for d, v in self.days.items() if d > since]
        summary = {"t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": len(window), "wet": WET_MMH, "east": {}}

        def rows_for(get):
            rows = []
            for k in range(LEADS + 1):
                h, m, f, cn, ab, n = (sum(get(v)[k][i] for v in window) for i in range(6))
                events = h + m + f
                rows.append({
                    "csi": round(h / events, 3) if events else None,    # rain correctly forecast
                    "bias": round((h + f) / (h + m), 2) if h + m else None,  # >1 too wet, <1 too dry
                    "mae": round(ab / n, 3) if n else None,
                    "events": events,
                    "n": n,
                })
            return rows

        for src in ("knmi", "br"):
            summary[src] = rows_for(lambda v: v[src])
        blank = [[0] * 6 for _ in range(LEADS + 1)]
        for src in ("knmi", "br", "dwd", "mean2", "mean3"):
            summary["east"][src] = rows_for(lambda v: v.get("east", {}).get(src, blank))
        (self.out / "compare.json").write_text(json.dumps(summary, separators=(",", ":")))
