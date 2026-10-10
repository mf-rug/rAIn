"""Head-to-head: KNMI's nowcast vs Buienradar's, scored against what fell.

Every run, Buienradar's point forecast is fetched for a fixed set of places
across the Netherlands, and KNMI's forecast is read at the same places from the
run the job already downloads. When an observation arrives, both are compared
with it at the same places and lead times (only pairs where both exist), and
daily tallies are kept per lead time:

  hits, misses, false alarms, correct negatives   (rain = WET_MMH or more)
  sum of absolute errors (mm/h) and number of pairs

Lead 0 is Buienradar's own "now" vs KNMI's observation: how much the two
disagree about the present, independent of forecasting. KNMI's forecast starts
from the very observation it is scored against, so it has a head start that
lead 0 helps put in proportion.

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
        self.br = s.get("br", {})        # issue time → per place, values for leads 0..36
        self.knmi = s.get("knmi", {})    # run time → per place, values for leads 1..24
        self.days = s.get("days", {})    # day → {"br"|"knmi": [[h, m, f, cn, abs, n] per lead 0..24]}

    def fetch_buienradar(self):
        """Buienradar's forecast for every place; issue time = its first timestep."""
        got = {}
        for i, (la, lo) in enumerate(POINTS):
            try:
                with urllib.request.urlopen(f"{BUIENRADAR}?lat={la}&lon={lo}", timeout=10) as r:
                    fs = json.load(r)["forecasts"]
            except Exception as e:  # one place failing shouldn't stop the rest
                print("buienradar", la, lo, e)
                continue
            t_first = datetime.fromisoformat(fs[0]["utcdatetime"]).replace(tzinfo=timezone.utc)
            series = got.setdefault(key(t_first), [None] * len(POINTS))
            series[i] = [f["precipitation"] for f in fs]
            time.sleep(0.1)
        self.br.update(got)

    def add_knmi(self, ref, grids):
        """KNMI forecast run (byte grids for leads 1..24) at the places."""
        self.knmi[key(ref)] = [list(v) for v in zip(*[mmh(g) for g in grids])]

    def score(self, t, grid):
        """Score both sources against the observation at t (byte grid)."""
        obs = mmh(grid)
        day = self.days.setdefault(t.strftime("%Y-%m-%d"), {
            "br": [[0] * 6 for _ in range(LEADS + 1)],
            "knmi": [[0] * 6 for _ in range(LEADS + 1)],
        })

        def tally(row, f, o):
            fw, ow = f >= WET_MMH, o >= WET_MMH
            row[0 if fw and ow else 1 if ow else 2 if fw else 3] += 1
            row[4] += abs(f - o)
            row[5] += 1

        for k in range(LEADS + 1):
            b = self.br.get(key(t - k * STEP))
            kn = self.knmi.get(key(t - k * STEP)) if k else None
            for p in range(len(POINTS)):
                bv = b[p][k] if b and b[p] and k < len(b[p]) else None
                if k == 0:
                    if bv is not None:
                        tally(day["br"][0], bv, obs[p])
                    continue
                kv = kn[p][k - 1] if kn else None
                if bv is None or kv is None:
                    continue      # only compare where both made a forecast
                tally(day["br"][k], bv, obs[p])
                tally(day["knmi"][k], kv, obs[p])

    def save(self, t0):
        old = key(t0 - timedelta(hours=3))
        self.br = {k: v for k, v in self.br.items() if k >= old}
        self.knmi = {k: v for k, v in self.knmi.items() if k >= old}
        cutoff = (t0 - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        self.days = {d: v for d, v in self.days.items() if d >= cutoff}
        self.path.write_text(json.dumps({"br": self.br, "knmi": self.knmi, "days": self.days}, separators=(",", ":")))

        # Summary over the last DAYS days, per source and lead time.
        since = (t0 - timedelta(days=DAYS)).strftime("%Y-%m-%d")
        window = [v for d, v in self.days.items() if d > since]
        summary = {"t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": len(window), "wet": WET_MMH}
        for src in ("knmi", "br"):
            rows = []
            for k in range(LEADS + 1):
                h, m, f, cn, ab, n = (sum(v[src][k][i] for v in window) for i in range(6))
                events = h + m + f
                rows.append({
                    "csi": round(h / events, 3) if events else None,    # rain correctly forecast
                    "bias": round((h + f) / (h + m), 2) if h + m else None,  # >1 too wet, <1 too dry
                    "mae": round(ab / n, 3) if n else None,
                    "events": events,
                    "n": n,
                })
            summary[src] = rows
        (self.out / "compare.json").write_text(json.dumps(summary, separators=(",", ":")))
